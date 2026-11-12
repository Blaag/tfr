from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from tfr.release_leases import (
    ReleaseLeaseError,
    exclusive_release_lease,
    pruning_is_enabled,
    pruning_state,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - managed installs target POSIX hosts
    fcntl = None  # type: ignore[assignment]

_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}$")
_STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LAUNCHER_MARKER = "# Managed by TFR's versioned installer."
_MANAGED_ROOT_ENV = "TFR_MANAGED_ROOT"
_RELEASE_RETENTION = 5
_LOGGER = logging.getLogger(__name__)


class InstallationError(ValueError):
    """Raised when a managed installation is unsafe or invalid."""


def default_installation_root() -> Path:
    return Path.home() / ".local" / "share" / "tfr"


def default_bin_directory() -> Path:
    return Path.home() / ".local" / "bin"


def checkout_release_id(version: str, commit: str) -> str:
    if _VERSION.fullmatch(version) is None:
        raise InstallationError("project version is invalid")
    if _COMMIT.fullmatch(commit) is None:
        raise InstallationError("Git commit must be 40 lowercase hexadecimal characters")
    return f"{version}+git.{commit}"


def stable_release_id(version: str, commit: str) -> str:
    if _STABLE_VERSION.fullmatch(version) is None:
        raise InstallationError("stable release version is invalid")
    if _COMMIT.fullmatch(commit) is None:
        raise InstallationError("stable release commit must be 40 lowercase hexadecimal characters")
    return f"{version}+stable.{commit}"


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    release_id: str
    version: str
    commit: str
    source: str
    installed_at: str
    python_version: str
    wheel_sha256: str
    lock_sha256: str

    def __post_init__(self) -> None:
        if _RELEASE_ID.fullmatch(self.release_id) is None:
            raise InstallationError("release ID is invalid")
        if _VERSION.fullmatch(self.version) is None:
            raise InstallationError("release version is invalid")
        if _COMMIT.fullmatch(self.commit) is None:
            raise InstallationError("release commit is invalid")
        if self.source not in {"git-checkout", "stable-release"}:
            raise InstallationError("release source is invalid")
        if self.source == "git-checkout" and self.release_id != checkout_release_id(
            self.version, self.commit
        ):
            raise InstallationError("checkout release ID does not match its version and commit")
        if self.source == "stable-release" and self.release_id != stable_release_id(
            self.version, self.commit
        ):
            raise InstallationError("stable release ID does not match its version and commit")
        try:
            installed_at = datetime.fromisoformat(self.installed_at)
        except ValueError as exc:
            raise InstallationError("release installation time is invalid") from exc
        if installed_at.tzinfo is None:
            raise InstallationError("release installation time must include a timezone")
        if not self.python_version or any(character.isspace() for character in self.python_version):
            raise InstallationError("release Python version is invalid")
        for name, digest in (
            ("wheel", self.wheel_sha256),
            ("lock", self.lock_sha256),
        ):
            if _DIGEST.fullmatch(digest) is None:
                raise InstallationError(f"release {name} digest is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "release_id": self.release_id,
            "version": self.version,
            "commit": self.commit,
            "source": self.source,
            "installed_at": self.installed_at,
            "python_version": self.python_version,
            "wheel_sha256": self.wheel_sha256,
            "lock_sha256": self.lock_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ReleaseMetadata:
        if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
            raise InstallationError("release metadata must be an object")
        expected = {
            "schema_version",
            "release_id",
            "version",
            "commit",
            "source",
            "installed_at",
            "python_version",
            "wheel_sha256",
            "lock_sha256",
        }
        schema_version = value.get("schema_version")
        if (
            set(value) != expected
            or not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != 1
        ):
            raise InstallationError("release metadata has unexpected or missing fields")
        fields = {name: value[name] for name in expected - {"schema_version"}}
        if not all(isinstance(field, str) for field in fields.values()):
            raise InstallationError("release metadata fields must be strings")
        return cls(**fields)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class InstalledRelease:
    path: Path
    metadata: ReleaseMetadata
    current: bool = False
    previous: bool = False


@dataclass(frozen=True, slots=True)
class ReleasePruneResult:
    enabled: bool
    deleted: tuple[str, ...] = ()
    retained_live: tuple[str, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()
    reclaimed_bytes: int = 0


@dataclass(frozen=True, slots=True)
class InstallationLayout:
    root: Path
    bin_directory: Path

    @classmethod
    def defaults(
        cls,
        *,
        root: Path | str | None = None,
        bin_directory: Path | str | None = None,
    ) -> InstallationLayout:
        return cls(
            root=Path(root).expanduser().resolve() if root else default_installation_root(),
            bin_directory=(
                Path(bin_directory).expanduser().resolve()
                if bin_directory
                else default_bin_directory()
            ),
        )

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

    @property
    def launcher(self) -> Path:
        return self.bin_directory / "tfr"

    def prepare(self) -> None:
        _validate_ancestors(self.root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _validate_directory(self.root, private=True)
        _validate_ancestors(self.root)
        self.releases.mkdir(exist_ok=True, mode=0o700)
        _validate_directory(self.releases, private=True)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.prepare()
        if fcntl is None:
            raise InstallationError("managed installation locking requires a POSIX host")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode) or (
                file_stat.st_uid != os.geteuid() or stat.S_IMODE(file_stat.st_mode) & 0o077
            ):
                raise InstallationError("installation lock must be an owner-only regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallationError("another TFR installation operation is running") from exc
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def create_staging_directory(self, release_id: str) -> Path:
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise InstallationError("release ID is invalid")
        self.prepare()
        return Path(tempfile.mkdtemp(prefix=f".staging-{release_id}-", dir=self.releases))

    def commit_staged_release(self, staging: Path, metadata: ReleaseMetadata) -> Path:
        staging = staging.resolve()
        if staging.parent != self.releases or not staging.name.startswith(".staging-"):
            raise InstallationError("staging directory is outside the managed releases directory")
        metadata_path = staging / "release.json"
        _write_json(metadata_path, metadata.as_dict())
        target = self.releases / metadata.release_id
        if os.path.lexists(target):
            installed = self.validate_release(metadata.release_id)
            comparable_existing = (
                installed.metadata.version,
                installed.metadata.commit,
                installed.metadata.source,
                installed.metadata.python_version,
                installed.metadata.wheel_sha256,
                installed.metadata.lock_sha256,
            )
            comparable_new = (
                metadata.version,
                metadata.commit,
                metadata.source,
                metadata.python_version,
                metadata.wheel_sha256,
                metadata.lock_sha256,
            )
            if comparable_existing != comparable_new:
                raise InstallationError(f"release already exists with different metadata: {target}")
            shutil.rmtree(staging)
            return target
        os.replace(staging, target)
        return target

    def validate_release(self, release_id: str) -> InstalledRelease:
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise InstallationError("release ID is invalid")
        path = self.releases / release_id
        _validate_directory(path, private=False)
        metadata_path = path / "release.json"
        metadata = _read_metadata(metadata_path)
        if metadata.release_id != release_id:
            raise InstallationError("release metadata does not match its directory")
        python = release_python(path)
        if not python.exists() or not os.access(python, os.X_OK):
            raise InstallationError(f"release Python interpreter is missing: {python}")
        return InstalledRelease(
            path=path,
            metadata=metadata,
            current=self._pointer_release_id(self.current) == release_id,
            previous=self._pointer_release_id(self.previous) == release_id,
        )

    def list_releases(self) -> tuple[InstalledRelease, ...]:
        with self.lock():
            return self._list_releases_locked()

    def _list_releases_locked(self) -> tuple[InstalledRelease, ...]:
        current = self._pointer_release_id(self.current)
        previous = self._pointer_release_id(self.previous)
        releases = []
        for path in sorted(self.releases.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                continue
            installed = self.validate_release(path.name)
            releases.append(
                InstalledRelease(
                    path=installed.path,
                    metadata=installed.metadata,
                    current=path.name == current,
                    previous=path.name == previous,
                )
            )
        return tuple(releases)

    def activate(self, release_id: str, *, write_launcher: bool = True) -> InstalledRelease:
        with self.lock():
            return self._activate_locked(release_id, write_launcher=write_launcher)

    def rollback(self) -> InstalledRelease:
        with self.lock():
            previous = self._pointer_release_id(self.previous)
            if previous is None:
                raise InstallationError("no previous TFR release is available")
            return self._activate_locked(previous)

    def prune_releases(self, *, retain: int = _RELEASE_RETENTION) -> ReleasePruneResult:
        if retain < 2:
            raise InstallationError("release retention must preserve at least two releases")
        with self.lock():
            return self._prune_releases_locked(retain=retain)

    def release_status(self, *, retain: int = _RELEASE_RETENTION) -> dict[str, Any]:
        """Describe retention without deleting or advancing the safety epoch."""
        with self.lock():
            releases = self._list_releases_locked()
            reasons, live = self._retention_reasons_locked(releases, retain=retain)
            entries = []
            total_bytes = 0
            for release in releases:
                release_id = release.metadata.release_id
                size = _directory_size(release.path)
                total_bytes += size
                entries.append(
                    {
                        "release_id": release_id,
                        "version": release.metadata.version,
                        "source": release.metadata.source,
                        "installed_at": release.metadata.installed_at,
                        "bytes": size,
                        "reasons": sorted(reasons.get(release_id, ())),
                        "eligible": release_id not in reasons,
                    }
                )
            state = pruning_state(self.root)
            return {
                "managed": True,
                "root": str(self.root),
                "retention_target": retain,
                "pruning_enabled": bool(state and state.get("enabled") is True),
                "boot_id": state.get("boot_id") if state else None,
                "installed_count": len(entries),
                "installed_bytes": total_bytes,
                "live_releases": sorted(live),
                "releases": entries,
                "last_cleanup": _read_cleanup_result(self.root),
            }

    def _activate_locked(
        self, release_id: str, *, write_launcher: bool = True
    ) -> InstalledRelease:
        self.validate_release(release_id)
        if write_launcher:
            # Create or validate the stable entry point before changing the active release.
            self.write_launcher()
        current = self._pointer_release_id(self.current)
        if current != release_id:
            previous = self._pointer_release_id(self.previous)
            if current is not None:
                self.validate_release(current)
                self._replace_pointer(self.previous, current)
            try:
                self._replace_pointer(self.current, release_id)
            except (InstallationError, OSError) as exc:
                if current is not None:
                    try:
                        if previous is None:
                            self.previous.unlink(missing_ok=True)
                        else:
                            self._replace_pointer(self.previous, previous)
                    except (InstallationError, OSError) as restore_exc:
                        raise InstallationError(
                            "activation failed and the previous pointer could not be restored"
                        ) from restore_exc
                if isinstance(exc, InstallationError):
                    raise
                raise InstallationError(f"cannot activate release: {release_id}") from exc
        try:
            self._prune_releases_locked(
                retain=_RELEASE_RETENTION,
                fresh_install=current is None,
            )
        except (InstallationError, OSError, ReleaseLeaseError) as exc:
            # Activation is authoritative once the pointer has moved. Cleanup
            # must never turn a successful activation into a reported failure.
            _LOGGER.warning("TFR release cleanup was skipped: %s", exc)
        return self.validate_release(release_id)

    def _prune_releases_locked(
        self,
        *,
        retain: int,
        fresh_install: bool = False,
    ) -> ReleasePruneResult:
        if not pruning_is_enabled(self.root, fresh_install=fresh_install):
            result = ReleasePruneResult(enabled=False)
            self._write_cleanup_result(result, before=len(self._list_releases_locked()), after=None)
            return result
        releases = self._list_releases_locked()
        reasons, live = self._retention_reasons_locked(releases, retain=retain)
        protected = set(reasons)
        newest = sorted(
            releases,
            key=lambda release: (
                datetime.fromisoformat(release.metadata.installed_at),
                release.metadata.release_id,
            ),
            reverse=True,
        )
        deleted: list[str] = []
        failures: list[tuple[str, str]] = []
        reclaimed_bytes = 0
        for release in reversed(newest):
            release_id = release.metadata.release_id
            if release_id in protected:
                continue
            with exclusive_release_lease(self.root, release_id) as available:
                if not available:
                    live.add(release_id)
                    continue
                try:
                    # Validate again after taking the exclusive lease so the
                    # path cannot be replaced between eligibility and removal.
                    self.validate_release(release_id)
                    size = _directory_size(release.path)
                    shutil.rmtree(release.path)
                except (InstallationError, OSError) as exc:
                    failures.append((release_id, str(exc)))
                else:
                    deleted.append(release_id)
                    reclaimed_bytes += size
        result = ReleasePruneResult(
            enabled=True,
            deleted=tuple(deleted),
            retained_live=tuple(sorted(live)),
            failures=tuple(failures),
            reclaimed_bytes=reclaimed_bytes,
        )
        self._write_cleanup_result(
            result,
            before=len(releases),
            after=len(releases) - len(deleted),
        )
        return result

    def _retention_reasons_locked(
        self, releases: Sequence[InstalledRelease], *, retain: int
    ) -> tuple[dict[str, set[str]], set[str]]:
        reasons: dict[str, set[str]] = {}

        def protect(release_id: str | None, reason: str) -> None:
            if release_id is not None:
                reasons.setdefault(release_id, set()).add(reason)

        protect(self._pointer_release_id(self.current), "current")
        protect(self._pointer_release_id(self.previous), "previous")
        live: set[str] = set()
        for release in releases:
            release_id = release.metadata.release_id
            with exclusive_release_lease(self.root, release_id) as available:
                if not available:
                    live.add(release_id)
                    protect(release_id, "live-lease")
        newest = sorted(
            releases,
            key=lambda release: (
                datetime.fromisoformat(release.metadata.installed_at),
                release.metadata.release_id,
            ),
            reverse=True,
        )
        for release in newest[:retain]:
            protect(release.metadata.release_id, "recent")
        stable_releases = [
            release
            for release in releases
            if release.metadata.source == "stable-release"
            and _STABLE_VERSION.fullmatch(release.metadata.version) is not None
        ]
        if stable_releases:
            highest_stable = max(
                stable_releases,
                key=lambda release: tuple(
                    int(part) for part in release.metadata.version.split(".")
                ),
            )
            protect(highest_stable.metadata.release_id, "downgrade-high-water")
        return reasons, live

    def _write_cleanup_result(
        self, result: ReleasePruneResult, *, before: int, after: int | None
    ) -> None:
        lease_directory = self.root / "release-leases"
        lease_directory.mkdir(exist_ok=True, mode=0o700)
        _write_json(
            lease_directory / "cleanup.json",
            {
                "schema_version": 1,
                "timestamp": datetime.now().astimezone().isoformat(),
                "enabled": result.enabled,
                "installed_before": before,
                "installed_after": after,
                "deleted": list(result.deleted),
                "retained_live": list(result.retained_live),
                "failures": [list(item) for item in result.failures],
                "reclaimed_bytes": result.reclaimed_bytes,
            },
        )

    def write_launcher(self) -> Path:
        _validate_ancestors(self.bin_directory)
        self.bin_directory.mkdir(parents=True, exist_ok=True, mode=0o755)
        _validate_directory(self.bin_directory, private=False)
        _validate_ancestors(self.bin_directory)
        root = str(self.root)
        if any(character in root for character in "\r\n\0"):
            raise InstallationError("installation root cannot contain control characters")
        launcher = (
            "#!/bin/sh\n"
            f"{_LAUNCHER_MARKER}\n"
            f"TFR_MANAGED_ROOT={shlex.quote(root)}\n"
            "export TFR_MANAGED_ROOT\n"
            f"exec {shlex.quote(str(self.current / 'bin' / 'python'))} -I -m tfr \"$@\"\n"
        )
        if os.path.lexists(self.launcher):
            launcher_stat = self.launcher.stat(follow_symlinks=False)
            if not stat.S_ISREG(launcher_stat.st_mode) or (
                os.name == "posix"
                and (
                    launcher_stat.st_uid != os.geteuid()
                    or stat.S_IMODE(launcher_stat.st_mode) & 0o022
                )
            ):
                raise InstallationError(f"refusing to replace non-file launcher: {self.launcher}")
            try:
                existing = self.launcher.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise InstallationError(
                    f"cannot inspect existing launcher: {self.launcher}"
                ) from exc
            if _LAUNCHER_MARKER not in existing:
                raise InstallationError(f"refusing to replace unmanaged launcher: {self.launcher}")
        _atomic_write(self.launcher, launcher.encode(), mode=0o700)
        return self.launcher

    def _pointer_release_id(self, pointer: Path) -> str | None:
        if not os.path.lexists(pointer):
            return None
        pointer_stat = pointer.stat(follow_symlinks=False)
        if not stat.S_ISLNK(pointer_stat.st_mode):
            raise InstallationError(f"managed release pointer is not a symlink: {pointer}")
        target = Path(os.readlink(pointer))
        if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != "releases":
            raise InstallationError(f"managed release pointer escapes releases: {pointer}")
        release_id = target.parts[1]
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise InstallationError(f"managed release pointer is invalid: {pointer}")
        return release_id

    def _replace_pointer(self, pointer: Path, release_id: str) -> None:
        self._pointer_release_id(pointer)
        temporary = self.root / f".{pointer.name}-{uuid4().hex}"
        try:
            os.symlink(Path("releases") / release_id, temporary)
            os.replace(temporary, pointer)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def release_python(release_path: Path) -> Path:
    return release_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def managed_restart_command(arguments: Sequence[str]) -> list[str] | None:
    managed = managed_installation()
    if managed is None:
        return None
    _layout, installed = managed
    python = release_python(installed.path)
    return [str(python), "-I", "-m", "tfr", *arguments]


def managed_release_status() -> dict[str, Any]:
    managed = managed_installation()
    if managed is None:
        return {"managed": False}
    layout, _installed = managed
    return layout.release_status()


def prune_managed_releases() -> ReleasePruneResult | None:
    """Apply automatic retention to the active managed installation."""
    managed = managed_installation()
    if managed is None:
        return None
    layout, _installed = managed
    return layout.prune_releases()


def managed_installation() -> tuple[InstallationLayout, InstalledRelease] | None:
    root = os.environ.get(_MANAGED_ROOT_ENV)
    if root is None:
        return None
    try:
        layout = InstallationLayout.defaults(root=root)
        _validate_directory(layout.root, private=True)
        _validate_ancestors(layout.root)
        release_id = layout._pointer_release_id(layout.current)
        if release_id is None:
            return None
        return layout, layout.validate_release(release_id)
    except (InstallationError, OSError):
        return None


def _validate_directory(path: Path, *, private: bool) -> None:
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise InstallationError(f"managed directory does not exist: {path}") from exc
    if not stat.S_ISDIR(path_stat.st_mode):
        raise InstallationError(f"managed path is not a directory: {path}")
    if os.name == "posix" and (
        path_stat.st_uid != os.geteuid()
        or stat.S_IMODE(path_stat.st_mode) & 0o022
        or (private and stat.S_IMODE(path_stat.st_mode) & 0o077)
    ):
        requirement = "owner-only" if private else "not group/other writable"
        raise InstallationError(f"managed directory must be {requirement}: {path}")


def _validate_ancestors(path: Path) -> None:
    current = path.parent
    while True:
        try:
            current_stat = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                raise InstallationError(
                    f"managed path has no existing ancestor: {path}"
                ) from None
            current = parent
            continue
        if not stat.S_ISDIR(current_stat.st_mode):
            raise InstallationError(f"managed path ancestor is not a directory: {current}")
        if os.name == "posix":
            if current_stat.st_uid not in {0, os.geteuid()}:
                raise InstallationError(f"managed path ancestor has an untrusted owner: {current}")
            if (
                stat.S_IMODE(current_stat.st_mode) & 0o022
                and not stat.S_IMODE(current_stat.st_mode) & stat.S_ISVTX
            ):
                raise InstallationError(
                    f"managed path ancestor is writable by other users: {current}"
                )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _read_metadata(path: Path) -> ReleaseMetadata:
    try:
        path_stat = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(path_stat.st_mode):
            raise InstallationError(f"release metadata is not a regular file: {path}")
        if os.name == "posix" and (
            path_stat.st_uid != os.geteuid() or stat.S_IMODE(path_stat.st_mode) & 0o077
        ):
            raise InstallationError(f"release metadata must be owner-only: {path}")
        if path_stat.st_size > 64 * 1024:
            raise InstallationError(f"release metadata exceeds the size limit: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallationError(f"cannot read release metadata: {path}") from exc
    return ReleaseMetadata.from_mapping(value)


def _read_cleanup_result(root: Path) -> dict[str, Any] | None:
    path = root / "release-leases" / "cleanup.json"
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid != os.geteuid()
        or stat.S_IMODE(path_stat.st_mode) & 0o077
        or path_stat.st_size > 64 * 1024
    ):
        raise InstallationError("release cleanup state must be an owner-only bounded file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallationError("cannot read release cleanup state") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise InstallationError("release cleanup state is invalid")
    return value


def _directory_size(path: Path) -> int:
    total = 0
    for directory, _subdirectories, files in os.walk(path, followlinks=False):
        for name in files:
            candidate = Path(directory) / name
            try:
                candidate_stat = candidate.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(candidate_stat.st_mode):
                total += candidate_stat.st_size
    return total


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    _atomic_write(path, content, mode=0o600)


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}-",
            delete=False,
        ) as output:
            temporary_name = output.name
            os.chmod(output.name, mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            with contextlib.suppress(FileNotFoundError):
                Path(temporary_name).unlink()
