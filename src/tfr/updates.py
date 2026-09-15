from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import re
import stat
import tempfile
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tfr._build import BUILD_COMMIT
from tfr.config import UpdateConfig
from tfr.gateway_protocol import PROTOCOL_VERSION

_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_BUILD_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}$")
_BUILD_RELEASE = re.compile(
    r"^(?:(0|[1-9][0-9]*)!)?"
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:\.(0|[1-9][0-9]*))?(.*)$"
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_MANIFEST_BYTES = 128 * 1024


class UpdateError(ValueError):
    """Raised when release metadata cannot be fetched or validated."""


def _semver(value: object, field: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or (match := _SEMVER.fullmatch(value)) is None:
        raise UpdateError(f"{field} must be a stable semantic version")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _https_url(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise UpdateError(f"{field} must be an HTTPS URL")
    if any(
        character.isspace()
        or ord(character) < 32
        or 127 <= ord(character) <= 159
        or character == "\\"
        or unicodedata.category(character) == "Cf"
        for character in value
    ):
        raise UpdateError(f"{field} contains unsafe characters")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise UpdateError(f"{field} must be an HTTPS URL without credentials")
    return value


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise UpdateError(f"{field} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise UpdateError(f"{field} contains unexpected or missing fields")


@dataclass(frozen=True, slots=True)
class BuildIdentity:
    version: str
    commit: str | None
    protocol: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if _BUILD_VERSION.fullmatch(self.version) is None:
            raise UpdateError("build.version is invalid")
        if self.commit is not None and _COMMIT.fullmatch(self.commit) is None:
            raise UpdateError("build.commit must be a 40-character lowercase hexadecimal commit")
        if (
            not isinstance(self.protocol, int)
            or isinstance(self.protocol, bool)
            or self.protocol < 1
        ):
            raise UpdateError("build.protocol must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "commit": self.commit, "protocol": self.protocol}

    @classmethod
    def from_mapping(cls, value: object) -> BuildIdentity:
        data = _object(value, "build")
        _exact_fields(data, {"version", "commit", "protocol"}, "build")
        commit = data["commit"]
        protocol = data["protocol"]
        if commit is not None and not isinstance(commit, str):
            raise UpdateError("build.commit must be a string or null")
        if not isinstance(protocol, int) or isinstance(protocol, bool):
            raise UpdateError("build.protocol must be an integer")
        return cls(version=data["version"], commit=commit, protocol=protocol)


def current_build() -> BuildIdentity:
    try:
        installed_version = version("tfr")
    except PackageNotFoundError:
        installed_version = "0.0.0"
    commit = BUILD_COMMIT.casefold() if BUILD_COMMIT else None
    if commit is not None and _COMMIT.fullmatch(commit) is None:
        commit = None
    return BuildIdentity(version=installed_version, commit=commit)


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    url: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    version: str
    tag: str
    commit: str
    protocol_minimum: int
    protocol_maximum: int
    release_url: str
    artifact: ReleaseArtifact

    @classmethod
    def from_json(cls, content: bytes) -> ReleaseManifest:
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError("release manifest is not valid UTF-8 JSON") from exc
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
                "protocol",
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
            raise UpdateError("unsupported release manifest schema")
        if data["project"] != "tfr" or data["channel"] != "stable":
            raise UpdateError("release manifest is not for TFR's stable channel")
        semantic_version = data["version"]
        _semver(semantic_version, "manifest.version")
        if data["tag"] != f"v{semantic_version}":
            raise UpdateError("manifest.tag must match manifest.version")
        commit = data["commit"]
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            raise UpdateError("manifest.commit must be a full lowercase hexadecimal commit")
        protocol = _object(data["protocol"], "manifest.protocol")
        _exact_fields(protocol, {"minimum", "maximum"}, "manifest.protocol")
        minimum = protocol["minimum"]
        maximum = protocol["maximum"]
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or minimum < 1
            or maximum < minimum
        ):
            raise UpdateError("manifest protocol range is invalid")
        artifact_data = _object(data["artifact"], "manifest.artifact")
        _exact_fields(artifact_data, {"url", "size", "sha256"}, "manifest.artifact")
        size = artifact_data["size"]
        digest = artifact_data["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            raise UpdateError("manifest.artifact.size must be positive")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise UpdateError("manifest.artifact.sha256 must be a lowercase SHA-256 digest")
        return cls(
            version=semantic_version,
            tag=data["tag"],
            commit=commit,
            protocol_minimum=minimum,
            protocol_maximum=maximum,
            release_url=_https_url(data["release_url"], "manifest.release_url"),
            artifact=ReleaseArtifact(
                url=_https_url(artifact_data["url"], "manifest.artifact.url"),
                size=size,
                sha256=digest,
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project": "tfr",
            "channel": "stable",
            "version": self.version,
            "tag": self.tag,
            "commit": self.commit,
            "protocol": {
                "minimum": self.protocol_minimum,
                "maximum": self.protocol_maximum,
            },
            "release_url": self.release_url,
            "artifact": {
                "url": self.artifact.url,
                "size": self.artifact.size,
                "sha256": self.artifact.sha256,
            },
        }

    def supports(self, build: BuildIdentity) -> bool:
        return self.protocol_minimum <= build.protocol <= self.protocol_maximum


def update_available(build: BuildIdentity, manifest: ReleaseManifest) -> bool:
    latest = _semver(manifest.version, "manifest.version")
    installed_match = _BUILD_RELEASE.fullmatch(build.version)
    if installed_match is None:
        return False
    epoch = int(installed_match.group(1) or 0)
    installed = (
        int(installed_match.group(2)),
        int(installed_match.group(3)),
        int(installed_match.group(4) or 0),
    )
    suffix = installed_match.group(5)
    postrelease = suffix.startswith(".post") or re.fullmatch(r"\.[0-9]+", suffix) is not None
    prerelease = bool(suffix) and not (suffix.startswith("+") or postrelease)
    return (
        (epoch == 0 and latest > installed)
        or (epoch == 0 and latest == installed and prerelease)
        or (
            epoch == 0
            and latest == installed
            and not postrelease
            and build.commit is not None
            and build.commit != manifest.commit
        )
    )


@dataclass(frozen=True, slots=True)
class UpdateResult:
    checked_at: datetime | None
    manifest: ReleaseManifest | None
    error: str | None = None

    def available_for(self, build: BuildIdentity) -> bool:
        return self.manifest is not None and update_available(build, self.manifest)


@dataclass(frozen=True, slots=True)
class _FetchResult:
    content: bytes | None
    etag: str | None


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
        _https_url(new_url, "release redirect URL")
        redirected = super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )
        if redirected is not None and urlsplit(request.full_url).netloc != urlsplit(new_url).netloc:
            redirected.remove_header("If-None-Match")
        return redirected


def _fetch_manifest(url: str, etag: str | None, timeout: float) -> _FetchResult:
    headers = {"Accept": "application/json", "User-Agent": "tfr-update-checker"}
    if etag is not None:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_HttpsRedirectHandler())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return _FetchResult(content=None, etag=etag)
        raise UpdateError(f"release server returned HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f"cannot reach release server: {exc}") from exc
    with response:
        _https_url(response.geturl(), "release response URL")
        content = response.read(_MAX_MANIFEST_BYTES + 1)
        if len(content) > _MAX_MANIFEST_BYTES:
            raise UpdateError("release manifest exceeds the size limit")
        return _FetchResult(content=content, etag=response.headers.get("ETag"))


class UpdateChecker:
    def __init__(
        self,
        config: UpdateConfig,
        *,
        build: BuildIdentity | None = None,
        fetch: Callable[[str, str | None, float], _FetchResult] = _fetch_manifest,
    ) -> None:
        self.config = config
        self.build = build or current_build()
        self._fetch = fetch
        self._lock = asyncio.Lock()
        self._etag: str | None = None
        self._result = UpdateResult(checked_at=None, manifest=None)
        self._load_cache()

    @property
    def result(self) -> UpdateResult:
        return self._result

    @property
    def cache_path(self) -> Path:
        return self.config.state_directory / "stable.json"

    async def check(self) -> UpdateResult:
        if not self.config.enabled:
            self._result = UpdateResult(
                checked_at=None,
                manifest=None,
                error="update checks are disabled",
            )
            return self._result
        async with self._lock:
            try:
                fetched = await asyncio.to_thread(
                    self._fetch,
                    str(self.config.manifest_url),
                    self._etag,
                    self.config.timeout_seconds,
                )
                manifest = (
                    self._result.manifest
                    if fetched.content is None
                    else ReleaseManifest.from_json(fetched.content)
                )
                if manifest is None:
                    raise UpdateError(
                        "release server returned no manifest and no cache is available"
                    )
                checked_at = datetime.now(UTC)
                self._etag = fetched.etag
                self._result = UpdateResult(checked_at=checked_at, manifest=manifest)
                with contextlib.suppress(OSError, UpdateError):
                    self._write_cache()
            except (OSError, UpdateError, ValueError) as exc:
                self._result = UpdateResult(
                    checked_at=self._result.checked_at,
                    manifest=self._result.manifest,
                    error=str(exc),
                )
            return self._result

    async def run_periodically(self, notify: Callable[[UpdateResult], None]) -> None:
        await asyncio.sleep(self.config.initial_delay_seconds)
        while True:
            result = await self.check()
            if result.error is None and result.available_for(self.build):
                notify(result)
            delay = self.config.check_interval_seconds + random.uniform(
                0, self.config.jitter_seconds
            )
            await asyncio.sleep(delay)

    def _load_cache(self) -> None:
        try:
            content = self._read_cache_bytes()
            if content is None:
                return
            cache = _object(json.loads(content), "update cache")
            if cache.get("manifest_url") != str(self.config.manifest_url):
                return
            manifest = ReleaseManifest.from_json(
                json.dumps(cache["manifest"], separators=(",", ":")).encode()
            )
            checked_at = datetime.fromisoformat(str(cache["checked_at"]))
            if checked_at.tzinfo is None:
                return
            etag = cache.get("etag")
            if etag is not None and not isinstance(etag, str):
                return
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError, UpdateError):
            return
        self._etag = etag
        self._result = UpdateResult(checked_at=checked_at, manifest=manifest)

    def _write_cache(self) -> None:
        if self._result.manifest is None or self._result.checked_at is None:
            return
        directory = self.cache_path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._validate_cache_directory(directory.stat(follow_symlinks=False))
        payload = json.dumps(
            {
                "checked_at": self._result.checked_at.isoformat(),
                "etag": self._etag,
                "manifest_url": str(self.config.manifest_url),
                "manifest": self._result.manifest.as_dict(),
            },
            indent=2,
            sort_keys=True,
        ).encode()
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=directory,
                prefix=".stable-",
                delete=False,
            ) as output:
                temporary_name = output.name
                os.chmod(output.name, 0o600)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_name, self.cache_path)
        finally:
            if temporary_name is not None:
                with contextlib.suppress(FileNotFoundError):
                    Path(temporary_name).unlink()

    def _read_cache_bytes(self) -> bytes | None:
        directory = self.cache_path.parent
        try:
            directory_stat = directory.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        self._validate_cache_directory(directory_stat)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        descriptor = os.open(self.cache_path, flags)
        with os.fdopen(descriptor, "rb") as cache_file:
            file_stat = os.fstat(cache_file.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise UpdateError("update cache is not a regular file")
            if os.name == "posix" and (
                file_stat.st_uid != os.geteuid() or stat.S_IMODE(file_stat.st_mode) & 0o077
            ):
                raise UpdateError("update cache must be owner-controlled")
            content = cache_file.read(_MAX_MANIFEST_BYTES + 1)
        return content if len(content) <= _MAX_MANIFEST_BYTES else None

    @staticmethod
    def _validate_cache_directory(directory_stat: os.stat_result) -> None:
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise UpdateError("update cache parent is not a directory")
        if os.name == "posix" and (
            directory_stat.st_uid != os.geteuid() or stat.S_IMODE(directory_stat.st_mode) & 0o077
        ):
            raise UpdateError("update cache directory must be owner-controlled")


def format_update_status(label: str, build: BuildIdentity, result: UpdateResult) -> str:
    current = f"{build.version} ({build.commit[:12]})" if build.commit else build.version
    if result.error is not None:
        return f"{label} {current}: update check failed: {result.error}"
    if result.manifest is None:
        return f"{label} {current}: not checked yet"
    if result.available_for(build):
        compatibility = "" if result.manifest.supports(build) else "; protocol review required"
        return (
            f"{label} update available: {result.manifest.version} (running {current})"
            f"{compatibility}. {result.manifest.release_url}"
        )
    if result.manifest.version == build.version and build.commit is None:
        return f"{label} {current}: version is current; exact build identity is unavailable"
    if _BUILD_RELEASE.fullmatch(build.version) is None:
        return f"{label} {current}: stable version comparison is unavailable"
    return f"{label} {current}: up to date"
