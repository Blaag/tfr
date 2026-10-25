from __future__ import annotations

import contextlib
import json
import os
import secrets
import stat
from pathlib import Path
from uuid import UUID, uuid4


def load_or_create_native_ui_id(state_directory: Path | str) -> UUID:
    """Return the stable ID for this native UI installation."""

    directory = Path(state_directory).expanduser()
    path = directory / "native-ui.json"
    _ensure_private_directory(directory)
    try:
        return _load(path)
    except FileNotFoundError:
        pass

    ui_id = uuid4()
    temporary = directory / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as identity_file:
            descriptor = -1
            json.dump(
                {"schema_version": 1, "ui_id": str(ui_id)},
                identity_file,
                ensure_ascii=True,
                separators=(",", ":"),
            )
            identity_file.write("\n")
            identity_file.flush()
            os.fsync(identity_file.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return _load(path)
        _fsync_directory(directory)
        return ui_id
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _load(path: Path) -> UUID:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(f"native UI identity must be a regular file: {path}")
        if os.name == "posix":
            if details.st_uid != os.geteuid():
                raise ValueError(f"native UI identity must be owned by this user: {path}")
            if stat.S_IMODE(details.st_mode) & 0o077:
                raise ValueError(
                    f"native UI identity must not grant group or other permissions: {path}"
                )
        with os.fdopen(descriptor, encoding="utf-8") as identity_file:
            descriptor = -1
            try:
                value = json.load(identity_file)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError(f"cannot read native UI identity: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict) or set(value) != {"schema_version", "ui_id"}:
        raise ValueError("native UI identity has invalid fields")
    if value["schema_version"] != 1:
        raise ValueError("native UI identity has an unsupported schema")
    try:
        return UUID(str(value["ui_id"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("native UI identity has an invalid UI ID") from exc


def _ensure_private_directory(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ValueError(f"cannot create native UI state directory: {directory}") from exc
    details = directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError(f"native UI state path is not a directory: {directory}")
    if os.name == "posix":
        if details.st_uid != os.geteuid():
            raise ValueError(f"native UI state directory must be owned by this user: {directory}")
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError(
                f"native UI state directory must not grant group or other permissions: {directory}"
            )


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
