from __future__ import annotations

import argparse
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
)

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
) -> ReleaseMetadata:
    identity = repository_identity(repository)
    layout.prepare()
    with layout.lock():
        if os.path.lexists(layout.releases / identity.release_id):
            existing = layout.validate_release(identity.release_id)
            _verify_installed_release(existing.path, existing.metadata)
            if not _python_request_matches(python, existing.metadata.python_version):
                raise InstallationError(
                    f"release already uses Python {existing.metadata.python_version}, "
                    f"which does not match --python {python}"
                )
            if activate:
                layout._activate_locked(identity.release_id)
            return existing.metadata

    staging: Path | None = None
    with tempfile.TemporaryDirectory(prefix="tfr-checkout-build-") as temporary:
        work = Path(temporary)
        source = work / "source"
        source.mkdir()
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
            source_archive.extractall(source, filter="data")
        lock_file = source / "uv.lock"
        if not lock_file.is_file():
            raise InstallationError("committed checkout does not contain uv.lock")
        (source / "src" / "tfr" / "_build.py").write_text(
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
                str(source),
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
                str(source),
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
                str(source),
            ]
        )
        wheels = tuple(distributions.glob("*.whl"))
        if len(wheels) != 1:
            raise InstallationError("checkout build did not produce exactly one wheel")
        wheel = wheels[0]

        staging = layout.create_staging_directory(identity.release_id)
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
                release_id=identity.release_id,
                version=identity.version,
                commit=identity.commit,
                source="git-checkout",
                installed_at=datetime.now(UTC).isoformat(),
                python_version=str(build["python_version"]),
                wheel_sha256=_sha256(wheel),
                lock_sha256=_sha256(lock_file),
            )
            with layout.lock():
                installed_path = layout.commit_staged_release(staging, metadata)
                staging = None
                installed = layout.validate_release(identity.release_id)
                _verify_installed_release(installed_path, installed.metadata)
                if activate:
                    layout._activate_locked(identity.release_id)
            return installed.metadata
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)


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
