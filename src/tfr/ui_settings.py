from __future__ import annotations

import contextlib
import json
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from tfr.config import StrictModel

MAX_UI_SETTINGS_BYTES = 4 * 1024


class UiSettingsError(ValueError):
    pass


class UiSettingsDocument(StrictModel):
    schema_version: Literal[1] = 1
    mouse_mode: Literal["auto", "terminal", "tfr"] | None = None


class LocalUiSettingsStore:
    """Owner-private settings for preferences specific to one UI machine."""

    def __init__(self, state_directory: Path | str) -> None:
        self.state_directory = Path(state_directory).expanduser()
        self.path = self.state_directory / "ui-overrides.json"
        self._lock = threading.RLock()
        self._ensure_directory()
        self._document = self._load()

    @property
    def mouse_mode(self) -> Literal["auto", "terminal", "tfr"] | None:
        return self._document.mouse_mode

    def set_mouse_mode(self, mode: Literal["auto", "terminal", "tfr"]) -> None:
        with self._lock:
            proposed = UiSettingsDocument(mouse_mode=mode)
            self._save(proposed)
            self._document = proposed

    def _load(self) -> UiSettingsDocument:
        try:
            descriptor = self._open_private(self.path)
        except FileNotFoundError:
            return UiSettingsDocument()
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(MAX_UI_SETTINGS_BYTES + 1)
        if len(raw) > MAX_UI_SETTINGS_BYTES:
            raise UiSettingsError("local UI settings exceed the maximum size")
        try:
            return UiSettingsDocument.model_validate(json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            raise UiSettingsError("local UI settings are invalid") from None

    def _ensure_directory(self) -> None:
        try:
            self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            details = self.state_directory.stat(follow_symlinks=False)
        except OSError as exc:
            raise UiSettingsError("cannot create local UI settings directory") from exc
        if not stat.S_ISDIR(details.st_mode):
            raise UiSettingsError("local UI settings parent must be a directory")
        if os.name == "posix" and (
            details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise UiSettingsError(
                "local UI settings directory must be private and owner-controlled"
            )

    @staticmethod
    def _flags(base: int) -> int:
        return base | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    def _open_private(self, path: Path) -> int:
        try:
            descriptor = os.open(path, self._flags(os.O_RDONLY))
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise UiSettingsError("cannot safely open local UI settings") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise UiSettingsError("local UI settings must be a regular file")
            if os.name == "posix" and (
                details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077
            ):
                raise UiSettingsError("local UI settings must be private and owner-controlled")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _save(self, document: UiSettingsDocument) -> None:
        self._ensure_directory()
        encoded = (
            json.dumps(document.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        if len(encoded) > MAX_UI_SETTINGS_BYTES:
            raise UiSettingsError("local UI settings exceed the maximum size")
        if self.path.exists() or self.path.is_symlink():
            os.close(self._open_private(self.path))
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary,
            self._flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
        )
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            if os.name == "posix":
                directory = os.open(
                    self.state_directory,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
