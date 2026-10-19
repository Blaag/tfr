from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from tfr.config import ConfigurationBundle, GagConfig, StrictModel, UiConfig, WorldsConfig

MAX_MANAGED_SETTINGS_BYTES = 64 * 1024


class ManagedSettingsError(ValueError):
    """Raised when managed settings cannot be validated or safely persisted."""


class ManagedSettingsRevisionConflict(ManagedSettingsError):
    """Raised when an operation was based on a stale managed-settings revision."""


class ManagedWorldOverride(StrictModel):
    gag: GagConfig


class ManagedSpellcheckOverride(StrictModel):
    enabled: bool


class ManagedSpeakingStreaksOverride(StrictModel):
    enabled: bool


class ManagedUiOverride(StrictModel):
    spellcheck: ManagedSpellcheckOverride | None = None
    speaking_streaks: ManagedSpeakingStreaksOverride | None = None


class ManagedSettingsDocument(StrictModel):
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    worlds: dict[str, ManagedWorldOverride] = Field(default_factory=dict)
    ui: ManagedUiOverride = Field(default_factory=ManagedUiOverride)


class ManagedSettingsResult(StrictModel):
    revision: int
    patterns: tuple[str, ...]
    changed: bool


class ManagedBooleanResult(StrictModel):
    revision: int
    enabled: bool
    changed: bool


AuditSink = Callable[[dict[str, object]], None]


def apply_managed_settings(
    bundle: ConfigurationBundle,
    *,
    audit: AuditSink | None = None,
) -> tuple[ConfigurationBundle, ManagedSettingsStore]:
    """Load managed overrides and return the validated effective configuration."""
    store = ManagedSettingsStore(
        bundle.main.managed_settings.state_directory,
        bundle.worlds,
        base_ui=bundle.main.ui,
        audit=audit,
    )
    main = bundle.main.model_copy(update={"ui": store.effective_ui})
    return bundle.model_copy(update={"main": main, "worlds": store.effective_worlds}), store


class ManagedSettingsStore:
    """Secure, revisioned storage for allowlisted Gateway-managed settings."""

    def __init__(
        self,
        state_directory: Path | str,
        base_worlds: WorldsConfig,
        *,
        base_ui: UiConfig | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self.state_directory = Path(state_directory).expanduser()
        self.path = self.state_directory / "managed-overrides.json"
        self.previous_path = self.state_directory / "managed-overrides.previous.json"
        self.lock_path = self.state_directory / ".managed-overrides.lock"
        self._base_worlds = base_worlds
        self._base_ui = base_ui or UiConfig()
        self._audit = audit
        self._lock = threading.RLock()
        self._ensure_private_directory()
        self._document = self._load()
        self._validate_effective(self._document)

    @property
    def revision(self) -> int:
        return self._document.revision

    @property
    def document(self) -> ManagedSettingsDocument:
        return self._document

    @property
    def effective_worlds(self) -> WorldsConfig:
        return self._effective_worlds(self._document)

    @property
    def effective_ui(self) -> UiConfig:
        return self._effective_ui(self._document)

    def set_audit_handler(self, audit: AuditSink | None) -> None:
        self._audit = audit

    def get_spellcheck_enabled(self) -> ManagedBooleanResult:
        with self._mutation_lock():
            override = self._document.ui.spellcheck
            return ManagedBooleanResult(
                revision=self._document.revision,
                enabled=(override.enabled if override else self._base_ui.spellcheck.enabled),
                changed=False,
            )

    def set_spellcheck_enabled(
        self, enabled: bool, expected_revision: int
    ) -> ManagedBooleanResult:
        return self._set_ui_boolean("spellcheck", enabled, expected_revision)

    def get_speaking_streaks_enabled(self) -> ManagedBooleanResult:
        with self._mutation_lock():
            override = self._document.ui.speaking_streaks
            return ManagedBooleanResult(
                revision=self._document.revision,
                enabled=(override.enabled if override else self._base_ui.speaking_streaks.enabled),
                changed=False,
            )

    def set_speaking_streaks_enabled(
        self, enabled: bool, expected_revision: int
    ) -> ManagedBooleanResult:
        return self._set_ui_boolean("speaking_streaks", enabled, expected_revision)

    def _set_ui_boolean(
        self,
        setting: Literal["spellcheck", "speaking_streaks"],
        enabled: bool,
        expected_revision: int,
    ) -> ManagedBooleanResult:
        with self._mutation_lock():
            self._check_revision(expected_revision)
            current = (
                self._effective_ui(self._document).spellcheck.enabled
                if setting == "spellcheck"
                else self._effective_ui(self._document).speaking_streaks.enabled
            )
            if current is enabled:
                self._record_audit(f"set_{setting}_enabled", "ui", self.revision, "unchanged")
                return ManagedBooleanResult(
                    revision=self.revision, enabled=enabled, changed=False
                )
            value = (
                ManagedSpellcheckOverride(enabled=enabled)
                if setting == "spellcheck"
                else ManagedSpeakingStreaksOverride(enabled=enabled)
            )
            ui = self._document.ui.model_copy(update={setting: value})
            proposed = ManagedSettingsDocument(
                revision=self._document.revision + 1,
                worlds=self._document.worlds,
                ui=ui,
            )
            self._validate_effective(proposed)
            operation = f"set_{setting}_enabled"
            try:
                self._save(proposed)
            except BaseException:
                self._record_audit(operation, "ui", self.revision, "failed")
                raise
            self._document = proposed
            self._record_audit(operation, "ui", proposed.revision, "committed")
            return ManagedBooleanResult(
                revision=proposed.revision, enabled=enabled, changed=True
            )

    def list_gag_patterns(self, world: str) -> ManagedSettingsResult:
        with self._mutation_lock():
            patterns = self._effective_patterns(self._document, world)
            return ManagedSettingsResult(
                revision=self._document.revision,
                patterns=patterns,
                changed=False,
            )

    def add_gag_pattern(
        self, world: str, pattern: str, expected_revision: int
    ) -> ManagedSettingsResult:
        try:
            GagConfig(patterns=(pattern,))
        except ValidationError:
            raise ManagedSettingsError("gag pattern is invalid") from None

        def update(patterns: tuple[str, ...]) -> tuple[str, ...]:
            return patterns if pattern in patterns else (*patterns, pattern)

        return self._mutate("add_gag_pattern", world, expected_revision, update)

    def remove_gag_pattern(
        self, world: str, pattern: str, expected_revision: int
    ) -> ManagedSettingsResult:
        return self._mutate(
            "remove_gag_pattern",
            world,
            expected_revision,
            lambda patterns: tuple(item for item in patterns if item != pattern),
        )

    def reset_gag_patterns(self, world: str, expected_revision: int) -> ManagedSettingsResult:
        with self._mutation_lock():
            self._check_world(world)
            self._check_revision(expected_revision)
            if world not in self._document.worlds:
                return self._unchanged("reset_gag_patterns", world)
            worlds = dict(self._document.worlds)
            del worlds[world]
            return self._commit("reset_gag_patterns", world, worlds)

    def _mutate(
        self,
        operation: str,
        world: str,
        expected_revision: int,
        update: Callable[[tuple[str, ...]], tuple[str, ...]],
    ) -> ManagedSettingsResult:
        with self._mutation_lock():
            self._check_world(world)
            self._check_revision(expected_revision)
            current = self._effective_patterns(self._document, world)
            proposed = update(current)
            if proposed == current:
                return self._unchanged(operation, world)
            try:
                override = ManagedWorldOverride(gag=GagConfig(patterns=proposed))
            except ValidationError:
                raise ManagedSettingsError("effective gag patterns are invalid") from None
            worlds = {**self._document.worlds, world: override}
            base_patterns = self._base_worlds.worlds[world].gag.patterns
            if proposed == base_patterns:
                worlds.pop(world, None)
            return self._commit(operation, world, worlds)

    def _commit(
        self,
        operation: str,
        world: str,
        worlds: dict[str, ManagedWorldOverride],
    ) -> ManagedSettingsResult:
        proposed = ManagedSettingsDocument(
            revision=self._document.revision + 1,
            worlds=worlds,
            ui=self._document.ui,
        )
        self._validate_effective(proposed)
        try:
            self._save(proposed)
        except BaseException:
            self._record_audit(operation, world, self._document.revision, "failed")
            raise
        self._document = proposed
        self._record_audit(operation, world, proposed.revision, "committed")
        return ManagedSettingsResult(
            revision=proposed.revision,
            patterns=self._effective_patterns(proposed, world),
            changed=True,
        )

    def _unchanged(self, operation: str, world: str) -> ManagedSettingsResult:
        self._record_audit(operation, world, self._document.revision, "unchanged")
        return ManagedSettingsResult(
            revision=self._document.revision,
            patterns=self._effective_patterns(self._document, world),
            changed=False,
        )

    def _check_world(self, world: str) -> None:
        if world not in self._base_worlds.worlds:
            raise ManagedSettingsError(f"unknown world: {world}")

    def _check_revision(self, expected_revision: int) -> None:
        if expected_revision != self._document.revision:
            raise ManagedSettingsRevisionConflict(
                f"managed settings revision is {self._document.revision}, "
                f"not {expected_revision}"
            )

    def _effective_patterns(
        self, document: ManagedSettingsDocument, world: str
    ) -> tuple[str, ...]:
        self._check_world(world)
        override = document.worlds.get(world)
        return (
            override.gag.patterns
            if override is not None
            else self._base_worlds.worlds[world].gag.patterns
        )

    def _effective_worlds(self, document: ManagedSettingsDocument) -> WorldsConfig:
        worlds = self._base_worlds.model_dump(mode="python", by_alias=True)
        for name, override in document.worlds.items():
            if name not in self._base_worlds.worlds:
                raise ManagedSettingsError(f"managed settings reference unknown world: {name}")
            worlds["worlds"][name]["gag"] = override.gag.model_dump(mode="python")
        try:
            return WorldsConfig.model_validate(worlds)
        except ValidationError:
            raise ManagedSettingsError(
                "managed settings produce invalid effective configuration"
            ) from None

    def _validate_effective(self, document: ManagedSettingsDocument) -> None:
        self._effective_worlds(document)
        self._effective_ui(document)

    def _effective_ui(self, document: ManagedSettingsDocument) -> UiConfig:
        ui = self._base_ui
        if document.ui.spellcheck is not None:
            ui = ui.model_copy(
                update={
                    "spellcheck": ui.spellcheck.model_copy(
                        update={"enabled": document.ui.spellcheck.enabled}
                    )
                }
            )
        if document.ui.speaking_streaks is not None:
            ui = ui.model_copy(
                update={
                    "speaking_streaks": ui.speaking_streaks.model_copy(
                        update={"enabled": document.ui.speaking_streaks.enabled}
                    )
                }
            )
        return UiConfig.model_validate(ui.model_dump(mode="python"))

    def _load(self) -> ManagedSettingsDocument:
        try:
            raw = self._read_private_file(self.path)
        except FileNotFoundError:
            return ManagedSettingsDocument()
        try:
            value = json.loads(raw)
            return ManagedSettingsDocument.model_validate(value)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
            raise ManagedSettingsError(f"invalid managed settings: {self.path}") from exc

    def _read_private_file(self, path: Path) -> bytes:
        descriptor = self._open_private_file(path)
        with os.fdopen(descriptor, "rb") as state_file:
            raw = state_file.read(MAX_MANAGED_SETTINGS_BYTES + 1)
        if len(raw) > MAX_MANAGED_SETTINGS_BYTES:
            raise ManagedSettingsError("managed settings exceed the maximum document size")
        return raw

    def _open_private_file(self, path: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ManagedSettingsError(f"cannot safely open managed settings: {path}") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ManagedSettingsError(f"managed settings must be a regular file: {path}")
            if os.name == "posix" and (
                details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077
            ):
                raise ManagedSettingsError(
                    f"managed settings must be private and owner-controlled: {path}"
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextlib.contextmanager
    def _mutation_lock(self) -> Iterator[None]:
        with self._lock:
            self._ensure_private_directory()
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = os.open(self.lock_path, flags, 0o600)
            except OSError as exc:
                raise ManagedSettingsError("cannot acquire managed settings lock") from exc
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode):
                    raise ManagedSettingsError("managed settings lock must be a regular file")
                if os.name == "posix" and (
                    details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077
                ):
                    raise ManagedSettingsError(
                        "managed settings lock must be private and owner-controlled"
                    )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._document = self._load()
                self._validate_effective(self._document)
                yield
            finally:
                os.close(descriptor)

    def _ensure_private_directory(self) -> None:
        try:
            self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            details = self.state_directory.stat(follow_symlinks=False)
        except OSError as exc:
            raise ManagedSettingsError(
                f"cannot create managed settings directory: {self.state_directory}"
            ) from exc
        if not stat.S_ISDIR(details.st_mode):
            raise ManagedSettingsError(
                f"managed settings path must be a directory: {self.state_directory}"
            )
        if os.name == "posix" and (
            details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise ManagedSettingsError(
                f"managed settings directory must be private and owner-controlled: "
                f"{self.state_directory}"
            )

    def _save(self, document: ManagedSettingsDocument) -> None:
        self._ensure_private_directory()
        encoded = (
            json.dumps(
                document.model_dump(mode="json"),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_MANAGED_SETTINGS_BYTES:
            raise ManagedSettingsError("managed settings exceed the maximum document size")
        try:
            previous = self._read_private_file(self.path)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            self._write_atomic(self.previous_path, previous)
        self._write_atomic(self.path, encoded)
        if os.name == "posix":
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(self.state_directory, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _write_atomic(self, destination: Path, value: bytes) -> None:
        if destination.exists() or destination.is_symlink():
            descriptor = self._open_private_file(destination)
            os.close(descriptor)
        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
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
            with os.fdopen(descriptor, "wb") as state_file:
                descriptor = -1
                state_file.write(value)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, destination)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def _record_audit(self, operation: str, world: str, revision: int, result: str) -> None:
        if self._audit is not None:
            self._audit(
                {
                    "operation": operation,
                    "world": world,
                    "revision": revision,
                    "result": result,
                }
            )
