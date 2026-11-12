from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - managed installs target POSIX hosts
    fcntl = None  # type: ignore[assignment]


_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_MANAGED_ROOT_ENV = "TFR_MANAGED_ROOT"
_LEASE_STATE_SCHEMA = 1
_PROCESS_LEASE_DESCRIPTORS: list[int] = []


class ReleaseLeaseError(ValueError):
    """Raised when managed release lease state is unsafe or invalid."""


def acquire_process_release_lease(package_file: str | Path) -> str | None:
    """Hold a shared lease for the managed release containing *package_file*."""
    configured_root = os.environ.get(_MANAGED_ROOT_ENV)
    if configured_root is None:
        return None
    if fcntl is None:
        raise ReleaseLeaseError("managed release leases require a POSIX host")
    root = Path(configured_root).expanduser().resolve()
    package_path = Path(package_file).resolve()
    releases = root / "releases"
    try:
        relative = package_path.relative_to(releases)
    except ValueError:
        # Installer bootstraps can intentionally import checkout code while a
        # managed root is present. Only code loaded from a release gets a lease.
        return None
    if not relative.parts or _RELEASE_ID.fullmatch(relative.parts[0]) is None:
        raise ReleaseLeaseError("managed package path has an invalid release ID")
    release_id = relative.parts[0]
    descriptor = _open_lease(root, release_id)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        release = releases / release_id
        if not release.is_dir() or not package_path.exists():
            raise ReleaseLeaseError(f"managed release disappeared while starting: {release_id}")
    except BaseException:
        os.close(descriptor)
        raise
    _PROCESS_LEASE_DESCRIPTORS.append(descriptor)
    return release_id


@contextlib.contextmanager
def exclusive_release_lease(root: Path, release_id: str) -> Iterator[bool]:
    """Yield whether an exclusive cleanup lease could be acquired."""
    if fcntl is None:
        raise ReleaseLeaseError("managed release leases require a POSIX host")
    descriptor = _open_lease(root, release_id)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def pruning_is_enabled(root: Path, *, fresh_install: bool = False) -> bool:
    """Advance the legacy-process safety epoch and return pruning eligibility."""
    lease_directory = _prepare_lease_directory(root)
    state_path = lease_directory / "state.json"
    boot_id = _boot_identity()
    state = _read_state(state_path)
    if state is None:
        enabled = fresh_install
        _write_state(state_path, enabled=enabled, boot_id=boot_id)
        return enabled
    if state["enabled"]:
        return True
    recorded_boot = state["boot_id"]
    if boot_id is not None and recorded_boot is not None and boot_id != recorded_boot:
        _write_state(state_path, enabled=True, boot_id=boot_id)
        return True
    return False


def _open_lease(root: Path, release_id: str) -> int:
    if _RELEASE_ID.fullmatch(release_id) is None:
        raise ReleaseLeaseError("release lease has an invalid release ID")
    lease_directory = _prepare_lease_directory(root)
    path = lease_directory / f"{release_id}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or (
            file_stat.st_uid != os.geteuid() or stat.S_IMODE(file_stat.st_mode) & 0o077
        ):
            raise ReleaseLeaseError("release lease must be an owner-only regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _prepare_lease_directory(root: Path) -> Path:
    lease_directory = root / "release-leases"
    lease_directory.mkdir(parents=False, exist_ok=True, mode=0o700)
    directory_stat = lease_directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(directory_stat.st_mode) or (
        directory_stat.st_uid != os.geteuid() or stat.S_IMODE(directory_stat.st_mode) & 0o077
    ):
        raise ReleaseLeaseError("release lease directory must be owner-only")
    return lease_directory


def _read_state(path: Path) -> dict[str, object] | None:
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(path_stat.st_mode) or (
        path_stat.st_uid != os.geteuid() or stat.S_IMODE(path_stat.st_mode) & 0o077
    ):
        raise ReleaseLeaseError("release lease state must be an owner-only regular file")
    if path_stat.st_size > 4096:
        raise ReleaseLeaseError("release lease state exceeds the size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseLeaseError("cannot read release lease state") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "enabled", "boot_id"}
        or value.get("schema_version") != _LEASE_STATE_SCHEMA
        or not isinstance(value.get("enabled"), bool)
        or not (value.get("boot_id") is None or isinstance(value.get("boot_id"), str))
    ):
        raise ReleaseLeaseError("release lease state is invalid")
    return value


def _write_state(path: Path, *, enabled: bool, boot_id: str | None) -> None:
    content = json.dumps(
        {
            "schema_version": _LEASE_STATE_SCHEMA,
            "enabled": enabled,
            "boot_id": boot_id,
        },
        separators=(",", ":"),
    )
    temporary = path.with_name(f".{path.name}-{uuid4().hex}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            state_file.write(content + "\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _boot_identity() -> str | None:
    linux_boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        value = linux_boot_id.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        value = ""
    if value:
        return f"linux:{value}"
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip()
        if value:
            return "darwin:" + hashlib.sha256(value.encode()).hexdigest()
    return None
