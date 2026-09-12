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
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import TYPE_CHECKING

from tfr.plugins import PLUGIN_ENTRY_POINT_GROUP

if TYPE_CHECKING:
    from tfr.config import PluginSource

GIT_TIMEOUT_SECONDS = 30
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_SHORTHAND = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class PluginSourceError(RuntimeError):
    """Raised when a configured plugin source cannot be fetched or read."""


@dataclass(frozen=True, slots=True)
class PluginSourceFailure:
    repo: str
    error: str


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
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
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
    checkout = plugins_directory / source_slug(url)
    pinned = _is_pinned_commit(source.ref)

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
                raise PluginSourceError(
                    f"could not check out {source.ref} in {url}: {output.strip()}"
                )
        return checkout

    if pinned or not source.auto_update:
        return checkout

    code, _output = await _run_git("fetch", "--quiet", "--depth", "1", "origin", cwd=checkout)
    if code != 0:
        return checkout
    code, _output = await _run_git("checkout", "--quiet", "--force", "FETCH_HEAD", cwd=checkout)
    return checkout


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
    import_root = str(src_dir if src_dir.is_dir() else project_root)
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
) -> tuple[tuple[EntryPoint, ...], tuple[PluginSourceFailure, ...]]:
    """Fetch and discover every configured plugin source.

    Each source is handled independently: a failure fetching or reading one
    source is returned in the failure list rather than raised, so a single
    unreachable or misconfigured repository cannot prevent the rest of the
    application from starting.
    """
    discovered: list[EntryPoint] = []
    failures: list[PluginSourceFailure] = []
    for source in sources:
        try:
            checkout = await sync_plugin_source(source, plugins_directory)
            discovered.extend(discover_source_entry_points(checkout, source))
        except PluginSourceError as exc:
            failures.append(PluginSourceFailure(repo=source.repo, error=str(exc)))
    return tuple(discovered), tuple(failures)
