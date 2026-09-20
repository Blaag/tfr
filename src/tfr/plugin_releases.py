from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from tfr.plugins import PLUGIN_API_VERSION, PLUGIN_ENTRY_POINT_GROUP
from tfr.updates import current_build

try:
    import fcntl
except ImportError:  # pragma: no cover - managed plugin releases target POSIX hosts
    fcntl = None  # type: ignore[assignment]

_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_INSTALLED_VERSION = re.compile(r"^(?:0!)?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROJECT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_RELEASE_ID = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\+stable\.[0-9a-f]{40}$"
)
_MAX_MANIFEST_BYTES = 128 * 1024
_GIT_TIMEOUT_SECONDS = 60
_GIT_OPTIONS = (
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "--no-replace-objects",
)


class PluginReleaseError(ValueError):
    """Raised when a stable plugin release is unsafe or invalid."""


class PluginReleaseNetworkError(PluginReleaseError):
    """Raised when a stable manifest cannot be retrieved."""


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise PluginReleaseError(f"{field} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise PluginReleaseError(f"{field} contains unexpected or missing fields")


def _semver(value: object, field: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or (match := _SEMVER.fullmatch(value)) is None:
        raise PluginReleaseError(f"{field} must be a stable semantic version")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _https_url(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise PluginReleaseError(f"{field} must be an HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or any(
            character.isspace()
            or ord(character) < 32
            or 127 <= ord(character) <= 159
            or character == "\\"
            or unicodedata.category(character) == "Cf"
            for character in value
        )
    ):
        raise PluginReleaseError(f"{field} must be a safe HTTPS URL without credentials")
    return value


@dataclass(frozen=True, slots=True)
class PluginReleaseManifest:
    project: str
    version: str
    tag: str
    commit: str
    tfr_minimum: str
    tfr_maximum_exclusive: str
    plugin_api_minimum: int
    plugin_api_maximum: int
    plugins: tuple[str, ...]
    release_url: str
    artifact_url: str
    artifact_size: int
    artifact_sha256: str

    @property
    def release_id(self) -> str:
        return f"{self.version}+stable.{self.commit}"

    @property
    def semantic_version(self) -> tuple[int, int, int]:
        return _semver(self.version, "manifest.version")

    @classmethod
    def from_json(cls, content: bytes) -> PluginReleaseManifest:
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginReleaseError("plugin release manifest is not valid UTF-8 JSON") from exc
        data = _object(value, "manifest")
        _exact_fields(
            data,
            {
                "schema_version",
                "project",
                "channel",
                "version",
                "tag",
                "commit",
                "compatibility",
                "plugins",
                "release_url",
                "artifact",
            },
            "manifest",
        )
        if (
            not isinstance(data["schema_version"], int)
            or isinstance(data["schema_version"], bool)
            or data["schema_version"] != 1
        ):
            raise PluginReleaseError("unsupported plugin release manifest schema")
        if data["channel"] != "stable":
            raise PluginReleaseError("plugin release manifest is not for the stable channel")
        project = data["project"]
        if not isinstance(project, str) or _PROJECT.fullmatch(project) is None:
            raise PluginReleaseError("manifest.project is invalid")
        version = data["version"]
        _semver(version, "manifest.version")
        if data["tag"] != f"v{version}":
            raise PluginReleaseError("manifest.tag must match manifest.version")
        commit = data["commit"]
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            raise PluginReleaseError("manifest.commit must be a full lowercase Git commit")

        compatibility = _object(data["compatibility"], "manifest.compatibility")
        _exact_fields(
            compatibility,
            {
                "tfr_minimum",
                "tfr_maximum_exclusive",
                "plugin_api_minimum",
                "plugin_api_maximum",
            },
            "manifest.compatibility",
        )
        tfr_minimum = compatibility["tfr_minimum"]
        tfr_maximum = compatibility["tfr_maximum_exclusive"]
        if _semver(tfr_minimum, "manifest.compatibility.tfr_minimum") >= _semver(
            tfr_maximum, "manifest.compatibility.tfr_maximum_exclusive"
        ):
            raise PluginReleaseError("manifest TFR compatibility range is invalid")
        api_minimum = compatibility["plugin_api_minimum"]
        api_maximum = compatibility["plugin_api_maximum"]
        if (
            not isinstance(api_minimum, int)
            or isinstance(api_minimum, bool)
            or not isinstance(api_maximum, int)
            or isinstance(api_maximum, bool)
            or api_minimum < 1
            or api_maximum < api_minimum
        ):
            raise PluginReleaseError("manifest plugin API compatibility range is invalid")

        plugins = data["plugins"]
        if (
            not isinstance(plugins, list)
            or not plugins
            or not all(isinstance(name, str) and name for name in plugins)
            or plugins != sorted(set(plugins))
        ):
            raise PluginReleaseError(
                "manifest.plugins must be a nonempty sorted unique string list"
            )
        artifact = _object(data["artifact"], "manifest.artifact")
        _exact_fields(artifact, {"url", "size", "sha256"}, "manifest.artifact")
        size = artifact["size"]
        digest = artifact["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            raise PluginReleaseError("manifest.artifact.size must be positive")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise PluginReleaseError("manifest.artifact.sha256 must be a lowercase SHA-256 digest")
        return cls(
            project=project,
            version=version,
            tag=data["tag"],
            commit=commit,
            tfr_minimum=tfr_minimum,
            tfr_maximum_exclusive=tfr_maximum,
            plugin_api_minimum=api_minimum,
            plugin_api_maximum=api_maximum,
            plugins=tuple(plugins),
            release_url=_https_url(data["release_url"], "manifest.release_url"),
            artifact_url=_https_url(artifact["url"], "manifest.artifact.url"),
            artifact_size=size,
            artifact_sha256=digest,
        )

    def assert_compatible(self) -> None:
        installed = current_build().version
        match = _INSTALLED_VERSION.match(installed)
        if match is None:
            raise PluginReleaseError(f"installed TFR version is not comparable: {installed}")
        tfr_version = tuple(int(part) for part in match.groups())
        if not (
            _semver(self.tfr_minimum, "manifest.compatibility.tfr_minimum")
            <= tfr_version
            < _semver(
                self.tfr_maximum_exclusive,
                "manifest.compatibility.tfr_maximum_exclusive",
            )
        ):
            raise PluginReleaseError(
                f"plugin release {self.version} does not support TFR {installed}"
            )
        if not self.plugin_api_minimum <= PLUGIN_API_VERSION <= self.plugin_api_maximum:
            raise PluginReleaseError(
                f"plugin release {self.version} does not support plugin API {PLUGIN_API_VERSION}"
            )


class _HttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> urllib.request.Request | None:
        _https_url(new_url, "plugin release redirect URL")
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def fetch_plugin_release_manifest(url: str, *, timeout: float = 10.0) -> PluginReleaseManifest:
    _https_url(url, "plugin release manifest URL")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "tfr-plugin-release-checker",
        },
    )
    opener = urllib.request.build_opener(_HttpsRedirectHandler())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise PluginReleaseNetworkError(f"plugin release server returned HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise PluginReleaseNetworkError(f"cannot reach plugin release server: {exc}") from exc
    with response:
        _https_url(response.geturl(), "plugin release response URL")
        content = response.read(_MAX_MANIFEST_BYTES + 1)
    if len(content) > _MAX_MANIFEST_BYTES:
        raise PluginReleaseError("plugin release manifest exceeds the size limit")
    return PluginReleaseManifest.from_json(content)


@dataclass(frozen=True, slots=True)
class PluginReleaseMetadata:
    project: str
    version: str
    tag: str
    commit: str
    repo_url: str
    manifest_url: str
    plugins: tuple[str, ...]
    tfr_minimum: str
    tfr_maximum_exclusive: str
    plugin_api_minimum: int
    plugin_api_maximum: int
    installed_at: str

    @property
    def release_id(self) -> str:
        return f"{self.version}+stable.{self.commit}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project": self.project,
            "version": self.version,
            "tag": self.tag,
            "commit": self.commit,
            "repo_url": self.repo_url,
            "manifest_url": self.manifest_url,
            "plugins": list(self.plugins),
            "compatibility": {
                "tfr_minimum": self.tfr_minimum,
                "tfr_maximum_exclusive": self.tfr_maximum_exclusive,
                "plugin_api_minimum": self.plugin_api_minimum,
                "plugin_api_maximum": self.plugin_api_maximum,
            },
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_mapping(cls, value: object) -> PluginReleaseMetadata:
        data = _object(value, "plugin release metadata")
        _exact_fields(
            data,
            {
                "schema_version",
                "project",
                "version",
                "tag",
                "commit",
                "repo_url",
                "manifest_url",
                "plugins",
                "compatibility",
                "installed_at",
            },
            "plugin release metadata",
        )
        if (
            not isinstance(data["schema_version"], int)
            or isinstance(data["schema_version"], bool)
            or data["schema_version"] != 1
        ):
            raise PluginReleaseError("unsupported installed plugin release metadata")
        compatibility = _object(data["compatibility"], "plugin release compatibility")
        _exact_fields(
            compatibility,
            {
                "tfr_minimum",
                "tfr_maximum_exclusive",
                "plugin_api_minimum",
                "plugin_api_maximum",
            },
            "plugin release compatibility",
        )
        plugins = data["plugins"]
        if not isinstance(plugins, list):
            raise PluginReleaseError("installed plugin names must be a list")
        try:
            installed_at = datetime.fromisoformat(data["installed_at"])
        except (TypeError, ValueError) as exc:
            raise PluginReleaseError("plugin release installation time is invalid") from exc
        if installed_at.tzinfo is None:
            raise PluginReleaseError("plugin release installation time must include a timezone")
        manifest = PluginReleaseManifest(
            project=data["project"],
            version=data["version"],
            tag=data["tag"],
            commit=data["commit"],
            tfr_minimum=compatibility["tfr_minimum"],
            tfr_maximum_exclusive=compatibility["tfr_maximum_exclusive"],
            plugin_api_minimum=compatibility["plugin_api_minimum"],
            plugin_api_maximum=compatibility["plugin_api_maximum"],
            plugins=tuple(plugins),
            release_url="https://metadata.invalid/release",
            artifact_url="https://metadata.invalid/artifact",
            artifact_size=1,
            artifact_sha256="0" * 64,
        )
        # Reuse the manifest validators for all release identity fields.
        PluginReleaseManifest.from_json(
            json.dumps(
                {
                    "schema_version": 1,
                    "project": manifest.project,
                    "channel": "stable",
                    "version": manifest.version,
                    "tag": manifest.tag,
                    "commit": manifest.commit,
                    "compatibility": {
                        "tfr_minimum": manifest.tfr_minimum,
                        "tfr_maximum_exclusive": manifest.tfr_maximum_exclusive,
                        "plugin_api_minimum": manifest.plugin_api_minimum,
                        "plugin_api_maximum": manifest.plugin_api_maximum,
                    },
                    "plugins": list(manifest.plugins),
                    "release_url": manifest.release_url,
                    "artifact": {
                        "url": manifest.artifact_url,
                        "size": 1,
                        "sha256": "0" * 64,
                    },
                }
            ).encode()
        )
        repo_url = data["repo_url"]
        manifest_url = data["manifest_url"]
        if not isinstance(repo_url, str) or not repo_url:
            raise PluginReleaseError("installed plugin repository URL is invalid")
        _https_url(manifest_url, "installed plugin manifest URL")
        return cls(
            project=manifest.project,
            version=manifest.version,
            tag=manifest.tag,
            commit=manifest.commit,
            repo_url=repo_url,
            manifest_url=manifest_url,
            plugins=manifest.plugins,
            tfr_minimum=manifest.tfr_minimum,
            tfr_maximum_exclusive=manifest.tfr_maximum_exclusive,
            plugin_api_minimum=manifest.plugin_api_minimum,
            plugin_api_maximum=manifest.plugin_api_maximum,
            installed_at=data["installed_at"],
        )


def _run_git(*arguments: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *_GIT_OPTIONS, *arguments],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PluginReleaseError(f"could not run git: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PluginReleaseError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def remove_plugin_bytecode(checkout: Path) -> None:
    for root, directories, files in os.walk(checkout, followlinks=False):
        root_path = Path(root)
        for name in tuple(directories):
            if name != "__pycache__":
                continue
            bytecode = root_path / name
            if bytecode.is_symlink():
                bytecode.unlink()
            else:
                shutil.rmtree(bytecode)
            directories.remove(name)
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                (root_path / name).unlink()


def verify_plugin_checkout_paths(checkout: Path) -> None:
    checkout_root = checkout.resolve()
    for root, directories, files in os.walk(checkout_root, followlinks=False):
        root_path = Path(root)
        if root_path == checkout_root and ".git" in directories:
            directories.remove(".git")
        for name in (*directories, *files):
            path = root_path / name
            if not path.is_symlink():
                continue
            try:
                target = path.resolve(strict=True)
            except OSError as exc:
                raise PluginReleaseError(
                    f"plugin checkout contains a broken symlink: {path}"
                ) from exc
            if target != checkout_root and checkout_root not in target.parents:
                raise PluginReleaseError(f"plugin checkout symlink escapes its release: {path}")


@dataclass(frozen=True, slots=True)
class PluginReleaseLayout:
    root: Path

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def previous(self) -> Path:
        return self.root / "previous"

    @property
    def lock_path(self) -> Path:
        return self.root / "install.lock"

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.releases.mkdir(exist_ok=True, mode=0o700)
        for directory in (self.root, self.releases):
            info = directory.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise PluginReleaseError(f"managed plugin path is unsafe: {directory}")
            directory.chmod(0o700)

    @contextlib.contextmanager
    def lock(self, *, timeout_seconds: float | None = None) -> Iterator[None]:
        self.prepare()
        if fcntl is None:
            raise PluginReleaseError("managed plugin locking requires a POSIX host")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise PluginReleaseError("managed plugin lock is unsafe")
            if timeout_seconds is None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + timeout_seconds
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise PluginReleaseError(
                                "timed out waiting for the managed plugin lock"
                            ) from None
                        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _pointer_release_id(self, pointer: Path) -> str | None:
        if not os.path.lexists(pointer):
            return None
        if not pointer.is_symlink():
            raise PluginReleaseError(f"managed plugin pointer is not a symlink: {pointer}")
        target = os.readlink(pointer)
        expected_prefix = "releases/"
        if not target.startswith(expected_prefix) or "/" in target[len(expected_prefix) :]:
            raise PluginReleaseError(f"managed plugin pointer has an unsafe target: {pointer}")
        release_id = target[len(expected_prefix) :]
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise PluginReleaseError(f"managed plugin pointer has an invalid release: {pointer}")
        return release_id

    def _replace_pointer(self, pointer: Path, release_id: str) -> None:
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise PluginReleaseError("plugin release ID is invalid")
        temporary = self.root / f".{pointer.name}-{uuid4().hex}"
        try:
            temporary.symlink_to(f"releases/{release_id}")
            os.replace(temporary, pointer)
        finally:
            temporary.unlink(missing_ok=True)

    def validate_release(
        self,
        release_id: str,
        *,
        repo_url: str | None = None,
        manifest_url: str | None = None,
        source_path: str = ".",
    ) -> tuple[Path, PluginReleaseMetadata]:
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise PluginReleaseError("plugin release ID is invalid")
        release = self.releases / release_id
        try:
            info = release.stat(follow_symlinks=False)
        except OSError as exc:
            raise PluginReleaseError(f"installed plugin release is missing: {release}") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PluginReleaseError(f"installed plugin release is unsafe: {release}")
        metadata_path = release / "release.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise PluginReleaseError("installed plugin release metadata is missing or unsafe")
        try:
            metadata_value = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata = PluginReleaseMetadata.from_mapping(metadata_value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginReleaseError("cannot read installed plugin release metadata") from exc
        if metadata.release_id != release_id:
            raise PluginReleaseError("installed plugin metadata does not match its directory")
        if repo_url is not None and metadata.repo_url != repo_url:
            raise PluginReleaseError("installed plugin repository does not match configuration")
        if manifest_url is not None and metadata.manifest_url != manifest_url:
            raise PluginReleaseError("installed plugin manifest does not match configuration")
        checkout = release / "checkout"
        if not (checkout / ".git").is_dir():
            raise PluginReleaseError("installed plugin checkout is missing")
        verify_plugin_checkout_paths(checkout)
        remove_plugin_bytecode(checkout)
        if _run_git("-C", str(checkout), "rev-parse", "HEAD") != metadata.commit:
            raise PluginReleaseError("installed plugin checkout commit does not match metadata")
        if _run_git("-C", str(checkout), "remote", "get-url", "origin") != metadata.repo_url:
            raise PluginReleaseError("installed plugin checkout origin does not match metadata")
        if _run_git("-C", str(checkout), "status", "--porcelain=v1", "--untracked-files=all"):
            raise PluginReleaseError("installed plugin checkout has local changes")
        _verify_annotated_tag(checkout, metadata.tag, metadata.commit)
        _verify_project(checkout, source_path, metadata)
        return checkout, metadata

    def current_release(
        self, *, repo_url: str, manifest_url: str | None = None, source_path: str
    ) -> tuple[Path, PluginReleaseMetadata] | None:
        release_id = self._pointer_release_id(self.current)
        if release_id is None:
            return None
        return self.validate_release(
            release_id,
            repo_url=repo_url,
            manifest_url=manifest_url,
            source_path=source_path,
        )

    def activate(
        self,
        release_id: str,
        *,
        repo_url: str,
        manifest_url: str | None = None,
        source_path: str,
    ) -> Path:
        checkout, _metadata = self.validate_release(
            release_id,
            repo_url=repo_url,
            manifest_url=manifest_url,
            source_path=source_path,
        )
        current = self._pointer_release_id(self.current)
        if current != release_id:
            previous = self._pointer_release_id(self.previous)
            if current is not None:
                self.validate_release(
                    current,
                    repo_url=repo_url,
                    manifest_url=manifest_url,
                    source_path=source_path,
                )
                self._replace_pointer(self.previous, current)
            try:
                self._replace_pointer(self.current, release_id)
            except (OSError, PluginReleaseError):
                if current is not None:
                    if previous is None:
                        self.previous.unlink(missing_ok=True)
                    else:
                        self._replace_pointer(self.previous, previous)
                raise
        return checkout

    def rollback(
        self, *, repo_url: str, manifest_url: str | None = None, source_path: str
    ) -> Path:
        previous = self._pointer_release_id(self.previous)
        if previous is None:
            raise PluginReleaseError("no previous plugin release is available")
        _checkout, metadata = self.validate_release(
            previous,
            repo_url=repo_url,
            manifest_url=manifest_url,
            source_path=source_path,
        )
        _manifest_from_metadata(metadata).assert_compatible()
        return self.activate(
            previous,
            repo_url=repo_url,
            manifest_url=manifest_url,
            source_path=source_path,
        )


def _verify_annotated_tag(checkout: Path, tag: str, commit: str) -> None:
    tag_object = _run_git("-C", str(checkout), "rev-parse", f"refs/tags/{tag}")
    if _run_git("-C", str(checkout), "cat-file", "-t", tag_object) != "tag":
        raise PluginReleaseError(f"plugin release tag is not annotated: {tag}")
    tag_content = _run_git("-C", str(checkout), "cat-file", "tag", tag_object)
    first_line = tag_content.splitlines()[0].split(" ", 1)
    if len(first_line) != 2 or first_line[0] != "object":
        raise PluginReleaseError(f"cannot inspect plugin release tag: {tag}")
    target = first_line[1]
    if _run_git("-C", str(checkout), "cat-file", "-t", target) != "commit":
        raise PluginReleaseError("plugin release tag does not point directly to a commit")
    if target != commit:
        raise PluginReleaseError("plugin release tag does not match the manifest commit")


def _project_data(checkout: Path, source_path: str) -> Mapping[str, Any]:
    path = Path(source_path)
    if path.is_absolute() or ".." in path.parts:
        raise PluginReleaseError("plugin source path is unsafe")
    relative = (path / "pyproject.toml").as_posix()
    try:
        content = _run_git("-C", str(checkout), "show", f"HEAD:{relative}")
        return tomllib.loads(content)
    except (tomllib.TOMLDecodeError, PluginReleaseError) as exc:
        raise PluginReleaseError("cannot read released plugin pyproject.toml") from exc


def _verify_project(
    checkout: Path, source_path: str, metadata: PluginReleaseMetadata
) -> Mapping[str, Any]:
    data = _project_data(checkout, source_path)
    project = _object(data.get("project"), "released project")
    if project.get("name") != metadata.project or project.get("version") != metadata.version:
        raise PluginReleaseError("released plugin project identity does not match the manifest")
    entry_points = _object(
        _object(project.get("entry-points", {}), "released entry points").get(
            PLUGIN_ENTRY_POINT_GROUP, {}
        ),
        "released TFR plugin entry points",
    )
    if tuple(sorted(entry_points)) != metadata.plugins:
        raise PluginReleaseError("released plugin entry points do not match the manifest")
    return data


def _metadata_from_manifest(
    manifest: PluginReleaseManifest, *, repo_url: str, manifest_url: str
) -> PluginReleaseMetadata:
    return PluginReleaseMetadata(
        project=manifest.project,
        version=manifest.version,
        tag=manifest.tag,
        commit=manifest.commit,
        repo_url=repo_url,
        manifest_url=manifest_url,
        plugins=manifest.plugins,
        tfr_minimum=manifest.tfr_minimum,
        tfr_maximum_exclusive=manifest.tfr_maximum_exclusive,
        plugin_api_minimum=manifest.plugin_api_minimum,
        plugin_api_maximum=manifest.plugin_api_maximum,
        installed_at=datetime.now(UTC).isoformat(),
    )


def install_plugin_release(
    layout: PluginReleaseLayout,
    manifest: PluginReleaseManifest,
    *,
    repo_url: str,
    manifest_url: str,
    source_path: str,
) -> Path:
    manifest.assert_compatible()
    metadata = _metadata_from_manifest(manifest, repo_url=repo_url, manifest_url=manifest_url)
    with layout.lock():
        for release in layout.releases.iterdir():
            if release.name.startswith("."):
                continue
            _checkout, existing = layout.validate_release(
                release.name,
                repo_url=repo_url,
                manifest_url=manifest_url,
                source_path=source_path,
            )
            if existing.version == manifest.version and existing.commit != manifest.commit:
                raise PluginReleaseError(
                    "plugin stable version is already installed from a different commit"
                )
            if _semver(existing.version, "installed plugin version") > manifest.semantic_version:
                raise PluginReleaseError("plugin stable release downgrade refused")

        target = layout.releases / manifest.release_id
        if os.path.lexists(target):
            return layout.activate(
                manifest.release_id,
                repo_url=repo_url,
                manifest_url=manifest_url,
                source_path=source_path,
            )
        staging = Path(
            tempfile.mkdtemp(prefix=f".staging-{manifest.release_id}-", dir=layout.releases)
        )
        try:
            checkout = staging / "checkout"
            _run_git("init", "--quiet", str(checkout))
            _run_git("-C", str(checkout), "remote", "add", "origin", repo_url)
            _run_git(
                "-C",
                str(checkout),
                "fetch",
                "--quiet",
                "--depth",
                "1",
                "--no-tags",
                "origin",
                f"refs/tags/{manifest.tag}:refs/tags/{manifest.tag}",
            )
            _verify_annotated_tag(checkout, manifest.tag, manifest.commit)
            _run_git("-C", str(checkout), "checkout", "--quiet", "--detach", manifest.commit)
            verify_plugin_checkout_paths(checkout)
            _verify_project(checkout, source_path, metadata)
            (staging / "release.json").write_text(
                json.dumps(metadata.as_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return layout.activate(
            manifest.release_id,
            repo_url=repo_url,
            manifest_url=manifest_url,
            source_path=source_path,
        )


def current_plugin_release(
    layout: PluginReleaseLayout,
    *,
    repo_url: str,
    manifest_url: str | None = None,
    source_path: str,
    lock_timeout_seconds: float | None = None,
    check_compatibility: bool = True,
) -> tuple[Path, PluginReleaseMetadata] | None:
    with layout.lock(timeout_seconds=lock_timeout_seconds):
        current = layout.current_release(
            repo_url=repo_url,
            manifest_url=manifest_url,
            source_path=source_path,
        )
        if current is not None and check_compatibility:
            _manifest_from_metadata(current[1]).assert_compatible()
        return current


def assert_plugin_release_compatible(metadata: PluginReleaseMetadata) -> None:
    _manifest_from_metadata(metadata).assert_compatible()


def _manifest_from_metadata(metadata: PluginReleaseMetadata) -> PluginReleaseManifest:
    return PluginReleaseManifest(
        project=metadata.project,
        version=metadata.version,
        tag=metadata.tag,
        commit=metadata.commit,
        tfr_minimum=metadata.tfr_minimum,
        tfr_maximum_exclusive=metadata.tfr_maximum_exclusive,
        plugin_api_minimum=metadata.plugin_api_minimum,
        plugin_api_maximum=metadata.plugin_api_maximum,
        plugins=metadata.plugins,
        release_url="https://metadata.invalid/release",
        artifact_url="https://metadata.invalid/artifact",
        artifact_size=1,
        artifact_sha256="0" * 64,
    )


def rollback_plugin_release(
    layout: PluginReleaseLayout,
    *,
    repo_url: str,
    manifest_url: str | None = None,
    source_path: str,
) -> Path:
    with layout.lock():
        return layout.rollback(
            repo_url=repo_url,
            manifest_url=manifest_url,
            source_path=source_path,
        )
