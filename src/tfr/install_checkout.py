from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tfr.installations import (
    InstallationError,
    InstallationLayout,
    ReleaseMetadata,
    checkout_release_id,
    release_python,
    stable_release_id,
)
from tfr.updates import ReleaseManifest, UpdateError, fetch_release_manifest

_OFFICIAL_MANIFEST_URL = (
    "https://github.com/Blaag/tfr/releases/latest/download/update-manifest.json"
)
_OFFICIAL_REPOSITORY_URL = "https://github.com/Blaag/tfr.git"
_STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

_GIT_OPTIONS = (
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "--no-replace-objects",
)


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    root: Path
    version: str
    commit: str
    release_id: str


def repository_identity(root: Path) -> RepositoryIdentity:
    root = root.resolve()
    if not (root / ".git").exists() or not (root / "pyproject.toml").is_file():
        raise InstallationError(f"not a TFR Git checkout: {root}")
    commit = _run(
        ["git", *_GIT_OPTIONS, "-C", str(root), "rev-parse", "HEAD"],
        capture=True,
    ).stdout.strip()
    status = _run(
        [
            "git",
            *_GIT_OPTIONS,
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        ],
        capture=True,
    ).stdout
    if status.strip():
        raise InstallationError("checkout has uncommitted or untracked changes; commit them first")
    confirmed_commit = _run(
        ["git", *_GIT_OPTIONS, "-C", str(root), "rev-parse", "HEAD"],
        capture=True,
    ).stdout.strip()
    if confirmed_commit != commit:
        raise InstallationError("checkout HEAD changed while its identity was being verified")
    git_common_directory = Path(
        _run(
            [
                "git",
                *_GIT_OPTIONS,
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture=True,
        ).stdout.strip()
    )
    local_git_overrides = (
        git_common_directory / "info/attributes",
        git_common_directory / "info/grafts",
    )
    for override in local_git_overrides:
        if os.path.lexists(override):
            raise InstallationError(f"checkout contains unsupported local Git metadata: {override}")
    try:
        pyproject = _run(
            [
                "git",
                *_GIT_OPTIONS,
                "-C",
                str(root),
                "show",
                f"{commit}:pyproject.toml",
            ],
            capture=True,
        ).stdout
        project = tomllib.loads(pyproject)["project"]
        if project["name"] != "tfr":
            raise InstallationError("checkout project name must be tfr")
        version = project["version"]
    except (KeyError, tomllib.TOMLDecodeError) as exc:
        raise InstallationError("cannot read the project version") from exc
    if not isinstance(version, str):
        raise InstallationError("project version must be a string")
    return RepositoryIdentity(
        root=root,
        version=version,
        commit=commit,
        release_id=checkout_release_id(version, commit),
    )


def install_checkout(
    repository: Path,
    layout: InstallationLayout,
    *,
    python: str = "3.12",
    activate: bool = True,
    provenance: str = "git-checkout",
) -> ReleaseMetadata:
    if provenance not in {"git-checkout", "stable-release"}:
        raise InstallationError("release provenance is invalid")
    identity = repository_identity(repository)
    release_id = (
        stable_release_id(identity.version, identity.commit)
        if provenance == "stable-release"
        else identity.release_id
    )
    layout.prepare()
    with layout.lock():
        if provenance == "stable-release":
            _reject_stable_downgrade_locked(layout, identity.version, identity.commit)
        if os.path.lexists(layout.releases / release_id):
            existing = layout.validate_release(release_id)
            _verify_installed_release(existing.path, existing.metadata)
            if not _python_request_matches(python, existing.metadata.python_version):
                raise InstallationError(
                    f"release already uses Python {existing.metadata.python_version}, "
                    f"which does not match --python {python}"
                )
            if activate:
                layout._activate_locked(release_id)
            return existing.metadata

    staging: Path | None = None
    with tempfile.TemporaryDirectory(prefix="tfr-checkout-build-") as temporary:
        work = Path(temporary)
        source_tree = work / "source"
        source_tree.mkdir()
        archive = work / "source.tar"
        _run(
            [
                "git",
                *_GIT_OPTIONS,
                "-C",
                str(identity.root),
                "archive",
                "--format=tar",
                "--output",
                str(archive),
                identity.commit,
            ]
        )
        with tarfile.open(archive) as source_archive:
            source_archive.extractall(source_tree, filter="data")
        lock_file = source_tree / "uv.lock"
        if not lock_file.is_file():
            raise InstallationError("committed checkout does not contain uv.lock")
        (source_tree / "src" / "tfr" / "_build.py").write_text(
            f'"""Build metadata generated by the checkout installer."""\n\n'
            f'BUILD_COMMIT: str | None = "{identity.commit}"\n',
            encoding="utf-8",
        )

        requirements = work / "requirements.txt"
        _run(
            [
                "uv",
                "export",
                "--project",
                str(source_tree),
                "--locked",
                "--no-default-groups",
                "--no-emit-project",
                "--no-config",
                "--quiet",
                "--output-file",
                str(requirements),
            ]
        )
        build_constraints = work / "build-constraints.txt"
        _run(
            [
                "uv",
                "export",
                "--project",
                str(source_tree),
                "--locked",
                "--only-group",
                "build",
                "--no-emit-project",
                "--no-config",
                "--quiet",
                "--output-file",
                str(build_constraints),
            ]
        )
        distributions = work / "dist"
        _run(
            [
                "uv",
                "build",
                "--no-config",
                "--build-constraints",
                str(build_constraints),
                "--require-hashes",
                "--wheel",
                "--out-dir",
                str(distributions),
                str(source_tree),
            ]
        )
        wheels = tuple(distributions.glob("*.whl"))
        if len(wheels) != 1:
            raise InstallationError("checkout build did not produce exactly one wheel")
        wheel = wheels[0]

        staging = layout.create_staging_directory(release_id)
        try:
            _run(
                [
                    "uv",
                    "venv",
                    "--no-config",
                    "--python",
                    python,
                    "--relocatable",
                    "--no-project",
                    str(staging),
                ]
            )
            interpreter = release_python(staging)
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--no-config",
                    "--python",
                    str(interpreter),
                    "--require-hashes",
                    "--only-binary",
                    ":all:",
                    "--link-mode",
                    "copy",
                    "--requirements",
                    str(requirements),
                ]
            )
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--no-config",
                    "--python",
                    str(interpreter),
                    "--no-cache",
                    "--no-deps",
                    "--link-mode",
                    "copy",
                    str(wheel),
                ]
            )
            build = _installed_build(interpreter)
            if build["version"] != identity.version or build["commit"] != identity.commit:
                raise InstallationError(
                    "installed checkout build identity does not match Git HEAD: "
                    f"expected {identity.version} ({identity.commit}), "
                    f"got {build['version']} ({build['commit']})"
                )
            metadata = ReleaseMetadata(
                release_id=release_id,
                version=identity.version,
                commit=identity.commit,
                source=provenance,
                installed_at=datetime.now(UTC).isoformat(),
                python_version=str(build["python_version"]),
                wheel_sha256=_sha256(wheel),
                lock_sha256=_sha256(lock_file),
            )
            with layout.lock():
                if provenance == "stable-release":
                    _reject_stable_downgrade_locked(
                        layout, identity.version, identity.commit
                    )
                installed_path = layout.commit_staged_release(staging, metadata)
                staging = None
                installed = layout.validate_release(release_id)
                _verify_installed_release(installed_path, installed.metadata)
                if activate:
                    layout._activate_locked(release_id)
            return installed.metadata
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)


@contextlib.contextmanager
def stable_release_checkout(
    manifest: ReleaseManifest,
    *,
    source_url: str = _OFFICIAL_REPOSITORY_URL,
) -> Iterator[RepositoryIdentity]:
    """Yield a temporary checkout of the exact annotated tag named by a manifest."""
    with tempfile.TemporaryDirectory(prefix="tfr-stable-source-") as temporary:
        work = Path(temporary)
        repository = work / "source"
        template = work / "empty-template"
        template.mkdir()
        _run(
            [
                "git",
                *_GIT_OPTIONS,
                "init",
                "--quiet",
                "--initial-branch=tfr-stable",
                f"--template={template}",
                str(repository),
            ]
        )
        tag_ref = f"refs/tags/{manifest.tag}"
        _run(
            [
                "git",
                *_GIT_OPTIONS,
                "-C",
                str(repository),
                "fetch",
                "--quiet",
                "--no-tags",
                "--depth=1",
                source_url,
                f"+{tag_ref}:{tag_ref}",
            ]
        )
        tag_type = _run(
            ["git", *_GIT_OPTIONS, "-C", str(repository), "cat-file", "-t", tag_ref],
            capture=True,
        ).stdout.strip()
        if tag_type != "tag":
            raise InstallationError(f"stable release tag is not annotated: {manifest.tag}")
        tag = _parse_tag_object(
            _run(
                ["git", *_GIT_OPTIONS, "-C", str(repository), "cat-file", "tag", tag_ref],
                capture=True,
            ).stdout
        )
        if tag.get("tag") != manifest.tag:
            raise InstallationError("stable release tag object has an unexpected name")
        if tag.get("type") != "commit":
            raise InstallationError("stable release tag does not point directly to a commit")
        if tag.get("object") != manifest.commit:
            raise InstallationError(
                "stable release tag commit does not match the release manifest"
            )
        _run(
            [
                "git",
                *_GIT_OPTIONS,
                "-C",
                str(repository),
                "checkout",
                "--quiet",
                "--detach",
                manifest.commit,
            ]
        )
        identity = repository_identity(repository)
        if identity.commit != manifest.commit:
            raise InstallationError("stable checkout commit does not match the release manifest")
        if identity.version != manifest.version:
            raise InstallationError("stable checkout version does not match the release manifest")
        yield identity


def install_latest_stable(
    layout: InstallationLayout,
    *,
    python: str = "3.12",
    activate: bool = True,
    manifest_url: str = _OFFICIAL_MANIFEST_URL,
    source_url: str = _OFFICIAL_REPOSITORY_URL,
    fetch_manifest: Callable[..., ReleaseManifest] = fetch_release_manifest,
) -> ReleaseMetadata:
    """Install the exact source commit identified by the live stable manifest."""
    try:
        manifest = fetch_manifest(manifest_url, timeout=10.0)
    except (OSError, UpdateError, ValueError) as exc:
        raise InstallationError(f"cannot resolve the latest stable release: {exc}") from exc
    with stable_release_checkout(manifest, source_url=source_url) as identity:
        return install_checkout(
            identity.root,
            layout,
            python=python,
            activate=activate,
            provenance="stable-release",
        )


def _parse_tag_object(content: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in content.splitlines():
        if not line:
            break
        name, separator, value = line.partition(" ")
        if not separator or name in headers:
            raise InstallationError("stable release tag object is malformed")
        headers[name] = value
    return headers


def _reject_stable_downgrade_locked(
    layout: InstallationLayout,
    version: str,
    commit: str,
) -> None:
    stable_releases = tuple(
        release
        for release in layout._list_releases_locked()
        if release.metadata.source == "stable-release"
    )
    if not stable_releases:
        return
    candidate = tuple(int(part) for part in version.split("."))
    installed_versions: list[tuple[int, int, int]] = []
    for release in stable_releases:
        installed_match = _STABLE_VERSION.fullmatch(release.metadata.version)
        if installed_match is None:
            raise InstallationError("installed stable release has an invalid version")
        installed = tuple(int(part) for part in installed_match.groups())
        installed_versions.append(installed)
        if candidate == installed and commit != release.metadata.commit:
            raise InstallationError(
                "latest stable release reuses an installed version with a different commit"
            )
    highest = max(installed_versions)
    if candidate < highest:
        highest_version = ".".join(str(part) for part in highest)
        raise InstallationError(
            f"latest stable release {version} is older than installed stable "
            f"release {highest_version}"
        )


def _verify_installed_release(path: Path, metadata: ReleaseMetadata) -> None:
    build = _installed_build(release_python(path))
    if build["version"] != metadata.version or build["commit"] != metadata.commit:
        raise InstallationError(f"installed release identity does not match metadata: {path}")


def _python_request_matches(request: str, actual: str) -> bool:
    numeric = re.fullmatch(r"(?:cpython-)?(\d+(?:\.\d+){0,2})", request)
    if numeric is not None:
        requested_parts = numeric.group(1).split(".")
        return actual.split(".")[: len(requested_parts)] == requested_parts
    resolved = _run(
        ["uv", "python", "find", "--no-config", "--python", request], capture=True
    ).stdout.strip()
    result = _run(
        [resolved, "-I", "-c", "import platform; print(platform.python_version())"],
        capture=True,
    )
    return result.stdout.strip() == actual


def _installed_build(python: Path) -> dict[str, Any]:
    command = (
        "import json, platform; "
        "from tfr.updates import current_build; "
        "build = current_build(); "
        "print(json.dumps({'version': build.version, 'commit': build.commit, "
        "'protocol': build.protocol, 'python_version': platform.python_version()}))"
    )
    result = _run([str(python), "-I", "-c", command], capture=True)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InstallationError("installed release returned invalid build identity") from exc
    if not isinstance(value, dict):
        raise InstallationError("installed release returned invalid build identity")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(arguments: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name)
    if arguments[0] == "git":
        environment["GIT_ATTR_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GCM_INTERACTIVE"] = "Never"
    try:
        return subprocess.run(
            arguments,
            check=True,
            env=environment,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError as exc:
        raise InstallationError(f"required command is unavailable: {arguments[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else f"exit status {exc.returncode}"
        raise InstallationError(f"command failed ({arguments[0]}): {detail}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install-from-checkout",
        description="Build and manage a versioned TFR installation from this Git checkout.",
    )
    parser.add_argument("--root", type=Path, help="managed installation root")
    parser.add_argument("--bin-dir", type=Path, help="launcher directory")
    parser.add_argument("--python", default="3.12", help="Python request passed to uv")
    parser.add_argument("--no-activate", action="store_true", help="install without activating")
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--list", action="store_true", help="list installed releases")
    operations.add_argument(
        "--activate",
        metavar="RELEASE_ID",
        help="activate an installed release",
    )
    operations.add_argument("--rollback", action="store_true", help="swap current and previous")
    operations.add_argument(
        "--latest-stable",
        action="store_true",
        help="install the exact source tag identified by the live stable manifest",
    )
    return parser


def run(argv: list[str] | None = None, *, repository: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    layout = InstallationLayout.defaults(root=args.root, bin_directory=args.bin_dir)
    try:
        if args.list:
            for release in layout.list_releases():
                markers = " ".join(
                    marker
                    for enabled, marker in (
                        (release.current, "current"),
                        (release.previous, "previous"),
                    )
                    if enabled
                )
                suffix = f" [{markers}]" if markers else ""
                print(f"{release.metadata.release_id}{suffix}")
            return 0
        if args.activate:
            release = layout.activate(args.activate)
            print(f"Activated TFR {release.metadata.release_id}")
            return 0
        if args.rollback:
            release = layout.rollback()
            print(f"Rolled back to TFR {release.metadata.release_id}")
            return 0
        if args.latest_stable:
            metadata = install_latest_stable(
                layout,
                python=args.python,
                activate=not args.no_activate,
            )
        else:
            root = repository or Path(__file__).resolve().parents[2]
            metadata = install_checkout(
                root,
                layout,
                python=args.python,
                activate=not args.no_activate,
            )
        operation = "Installed and activated" if not args.no_activate else "Installed"
        print(f"{operation} TFR {metadata.release_id}")
        if not args.no_activate:
            print(f"Launcher: {layout.launcher}")
            path_entries = os.environ.get("PATH", "").split(os.pathsep)
            if str(layout.bin_directory) not in path_entries:
                print(f"Add {layout.bin_directory} to PATH to invoke `tfr`.", file=sys.stderr)
        return 0
    except InstallationError as exc:
        print(f"install-from-checkout: {exc}", file=sys.stderr)
        return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
