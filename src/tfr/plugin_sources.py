"""Fetch and discover TFR plugins directly from Git repositories.

This module lets an operator point ``plugins.sources`` in the main
configuration at a GitHub (or other Git host) repository and have TFR fetch
it locally, without a separate packaging or installation step. It is a
convenience feature for small, trusted groups: plugin code fetched this way
runs with TFR's full process privileges, exactly like any other plugin. See
``PLUGIN_SOURCES.md``/the README "Installing Plugins" section for the
associated trust model.

TFR never installs fetched plugin code into the running Python environment
and never executes anything from the repository other than importing the
Python module named by its declared ``tfr.plugins.v1`` entry points (the same
loading step used for normally installed plugin distributions). It only
invokes the local ``git`` binary with argument lists (never a shell string),
and only for ``clone``, ``fetch``, and ``checkout``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import TYPE_CHECKING

from tfr.plugin_releases import (
    PluginReleaseError,
    PluginReleaseLayout,
    PluginReleaseNetworkError,
    current_plugin_release,
    fetch_plugin_release_manifest,
    install_plugin_release,
    remove_plugin_bytecode,
    verify_plugin_checkout_paths,
)
from tfr.plugins import PLUGIN_ENTRY_POINT_GROUP

if TYPE_CHECKING:
    from tfr.config import PluginSource

GIT_TIMEOUT_SECONDS = 30
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_SHORTHAND = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_GIT_OPTIONS = (
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "--no-replace-objects",
)


class PluginSourceError(RuntimeError):
    """Raised when a configured plugin source cannot be fetched or read."""


@dataclass(frozen=True, slots=True)
class PluginSourceFailure:
    repo: str
    error: str


@dataclass(frozen=True, slots=True)
class PluginSourceNotice:
    repo: str
    message: str


def normalize_repo_url(repo: str) -> str:
    """Expand an ``OWNER/REPO`` GitHub shorthand to a full clone URL.

    Any other value (a full ``https://``, ``ssh://``, or ``git@`` URL) is
    returned unchanged; ``git`` itself validates it when cloning.
    """
    if _GITHUB_SHORTHAND.fullmatch(repo):
        return f"https://github.com/{repo}.git"
    return repo


def source_slug(repo: str) -> str:
    """A deterministic, filesystem-safe checkout directory name for ``repo``."""
    digest = sha256(repo.encode("utf-8")).hexdigest()[:12]
    tail = repo.rstrip("/").rsplit("/", 1)[-1] or "repo"
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    safe = _SLUG_UNSAFE.sub("_", tail).strip("_.") or "repo"
    return f"{safe}-{digest}"


def _is_pinned_commit(ref: str | None) -> bool:
    return ref is not None and bool(_COMMIT_SHA.fullmatch(ref))


async def _run_git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *_GIT_OPTIONS,
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError as exc:
        raise PluginSourceError(f"could not run git: {exc}") from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=GIT_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise PluginSourceError(
            f"git {' '.join(args)} timed out after {GIT_TIMEOUT_SECONDS}s"
        ) from None
    return process.returncode or 0, output.decode("utf-8", errors="replace")


async def sync_plugin_source(source: PluginSource, plugins_directory: Path) -> Path:
    """Ensure a local checkout of ``source`` exists and return its path.

    - A source pinned to a full 40-character commit SHA is fetched once and
      never modified afterward, regardless of ``auto_update``.
    - A source with no pin, or pinned to a branch/tag name, is fetched once
      and left alone on later launches unless ``auto_update`` is set.
    - When ``auto_update`` is set and fetching fails (for example, the
      operator is offline), the existing local checkout is kept as-is rather
      than treated as an error.
    """
    url = normalize_repo_url(source.repo)
    pinned = _is_pinned_commit(source.ref)
    checkout_key = f"{url}@{source.ref}" if pinned else url
    checkout = plugins_directory / source_slug(checkout_key)

    if not (checkout / ".git").exists():
        plugins_directory.mkdir(parents=True, exist_ok=True)
        clone_args = ["clone", "--quiet"]
        if source.ref and not pinned:
            clone_args += ["--branch", source.ref]
        clone_args += ["--depth", "1", "--", url, str(checkout)]
        code, output = await _run_git(*clone_args)
        if code != 0:
            raise PluginSourceError(f"could not clone {url}: {output.strip()}")
        if pinned:
            code, output = await _run_git(
                "fetch", "--quiet", "--depth", "1", "origin", source.ref, cwd=checkout
            )
            if code == 0:
                code, output = await _run_git(
                    "checkout", "--quiet", "--force", source.ref, cwd=checkout
                )
            if code != 0:
                shutil.rmtree(checkout, ignore_errors=True)
                raise PluginSourceError(
                    f"could not check out {source.ref} in {url}: {output.strip()}"
                )
            await _verify_pinned_checkout(checkout, url, source.ref)
        return checkout

    if pinned:
        await _verify_pinned_checkout(checkout, url, source.ref)
        return checkout
    if not source.auto_update:
        return checkout

    code, _output = await _run_git("fetch", "--quiet", "--depth", "1", "origin", cwd=checkout)
    if code != 0:
        return checkout
    code, _output = await _run_git("checkout", "--quiet", "--force", "FETCH_HEAD", cwd=checkout)
    return checkout


async def _verify_pinned_checkout(checkout: Path, url: str, commit: str) -> None:
    code, origins = await _run_git("remote", "get-url", "--all", "origin", cwd=checkout)
    if code != 0 or origins.splitlines() != [url]:
        raise PluginSourceError("pinned plugin checkout origin does not match configuration")
    code, head = await _run_git("rev-parse", "HEAD", cwd=checkout)
    if code != 0 or head.strip() != commit:
        raise PluginSourceError("pinned plugin checkout does not match its configured commit")
    try:
        verify_plugin_checkout_paths(checkout)
        remove_plugin_bytecode(checkout)
    except PluginReleaseError as exc:
        raise PluginSourceError(str(exc)) from exc
    code, status = await _run_git(
        "status", "--porcelain=v1", "--untracked-files=all", cwd=checkout
    )
    if code != 0 or status.strip():
        raise PluginSourceError("pinned plugin checkout has local changes")


async def _resolve_stable_source(
    source: PluginSource, plugins_directory: Path
) -> tuple[Path, str | None]:
    url = normalize_repo_url(source.repo)
    manifest_url = str(source.manifest_url)
    layout = PluginReleaseLayout(
        (plugins_directory.expanduser() / "managed" / source_slug(url)).resolve()
    )
    try:
        current = await asyncio.to_thread(
            current_plugin_release,
            layout,
            repo_url=url,
            source_path=source.path,
        )
        try:
            manifest = await asyncio.to_thread(fetch_plugin_release_manifest, manifest_url)
        except PluginReleaseNetworkError as exc:
            if current is None:
                raise
            return current[0], f"using verified {current[1].version}; update check failed: {exc}"
        manifest.assert_compatible()
        if current is not None:
            installed = current[1]
            if installed.version == manifest.version:
                if installed.commit != manifest.commit:
                    raise PluginReleaseError(
                        "stable manifest changed the commit for an installed plugin version"
                    )
                return current[0], None
            if _version_tuple(manifest.version) < _version_tuple(installed.version):
                raise PluginReleaseError("stable plugin release downgrade refused")
            if source.policy == "stable-notify":
                return (
                    current[0],
                    f"stable plugin release {manifest.version} is available "
                    f"(current {installed.version})",
                )
        try:
            checkout = await asyncio.to_thread(
                install_plugin_release,
                layout,
                manifest,
                repo_url=url,
                manifest_url=manifest_url,
                source_path=source.path,
            )
        except PluginReleaseError as exc:
            if current is None:
                raise
            return current[0], f"using verified {current[1].version}; update failed: {exc}"
        return checkout, None
    except PluginReleaseError as exc:
        raise PluginSourceError(str(exc)) from exc


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
    if match is None:
        raise PluginSourceError(f"invalid stable plugin version: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def discover_source_entry_points(checkout: Path, source: PluginSource) -> tuple[EntryPoint, ...]:
    """Parse ``pyproject.toml`` in ``checkout`` and return its declared plugins.

    The repository's ``src`` directory (or its root, if there is no ``src``
    directory) is added to ``sys.path`` so the declared entry-point modules
    can be imported. Nothing in the repository is executed by this function;
    ``pyproject.toml`` is parsed as data only.
    """
    checkout_root = checkout.resolve()
    project_root = (checkout_root / source.path).resolve()
    if project_root != checkout_root and checkout_root not in project_root.parents:
        raise PluginSourceError(
            f"plugin source path {source.path!r} escapes its repository checkout"
        )

    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.is_file():
        raise PluginSourceError(f"{pyproject_path} does not exist")

    try:
        with pyproject_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise PluginSourceError(f"could not parse {pyproject_path}: {exc}") from exc

    entry_point_table = (
        data.get("project", {}).get("entry-points", {}).get(PLUGIN_ENTRY_POINT_GROUP, {})
    )
    if not entry_point_table:
        return ()

    src_dir = project_root / "src"
    import_path = (src_dir if src_dir.is_dir() else project_root).resolve()
    if import_path != checkout_root and checkout_root not in import_path.parents:
        raise PluginSourceError("plugin import path escapes its repository checkout")
    import_root = str(import_path)
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

    return tuple(
        EntryPoint(name=name, value=str(value), group=PLUGIN_ENTRY_POINT_GROUP)
        for name, value in entry_point_table.items()
    )


async def load_plugin_sources(
    sources: Sequence[PluginSource],
    *,
    plugins_directory: Path,
) -> tuple[
    tuple[EntryPoint, ...],
    tuple[PluginSourceFailure, ...],
    tuple[PluginSourceNotice, ...],
]:
    """Fetch and discover every configured plugin source.

    Each source is handled independently: a failure fetching or reading one
    source is returned in the failure list rather than raised, so a single
    unreachable or misconfigured repository cannot prevent the rest of the
    application from starting.
    """
    discovered: list[EntryPoint] = []
    failures: list[PluginSourceFailure] = []
    notices: list[PluginSourceNotice] = []
    for source in sources:
        try:
            if source.policy in {"stable-auto", "stable-notify"}:
                checkout, notice = await _resolve_stable_source(source, plugins_directory)
                if notice is not None:
                    notices.append(PluginSourceNotice(repo=source.repo, message=notice))
            else:
                checkout = await sync_plugin_source(source, plugins_directory)
            discovered.extend(discover_source_entry_points(checkout, source))
        except PluginSourceError as exc:
            failures.append(PluginSourceFailure(repo=source.repo, error=str(exc)))
    return tuple(discovered), tuple(failures), tuple(notices)
