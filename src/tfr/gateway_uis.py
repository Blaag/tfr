from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

MAX_UI_NAME_CHARACTERS = 80
MAX_UI_PLATFORM_CHARACTERS = 80
MAX_UI_CAPABILITIES = 64
MAX_UI_CAPABILITY_CHARACTERS = 64
MAX_UI_VERSIONS = 16
MAX_UI_VERSION_NAME_CHARACTERS = 32
MAX_UI_VERSION_VALUE_CHARACTERS = 80
UI_CLIENT_TYPES = frozenset({"terminal", "pwa"})


@dataclass(frozen=True, slots=True)
class UiRegistration:
    client_type: str
    platform: str
    versions: tuple[tuple[str, str], ...]
    capabilities: tuple[str, ...]
    applied_config_revision: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "client_type": self.client_type,
            "platform": self.platform,
            "versions": dict(self.versions),
            "capabilities": list(self.capabilities),
            "applied_config_revision": self.applied_config_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> UiRegistration:
        expected = {
            "client_type",
            "platform",
            "versions",
            "capabilities",
            "applied_config_revision",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("UI registration has invalid fields")
        client_type = value["client_type"]
        if client_type not in UI_CLIENT_TYPES:
            raise ValueError("UI client type is invalid")
        revision = value["applied_config_revision"]
        if revision is not None and (
            not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
        ):
            raise ValueError("UI applied configuration revision is invalid")
        return cls(
            client_type=client_type,
            platform=_bounded_text(value["platform"], "platform", MAX_UI_PLATFORM_CHARACTERS),
            versions=UiRecord._versions(value["versions"]),
            capabilities=UiRecord._capabilities(value["capabilities"]),
            applied_config_revision=revision,
        )


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"UI {field} must be a timestamp")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"UI {field} must be a timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"UI {field} timestamp must include a timezone")
    return timestamp.astimezone(UTC)


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not value.isprintable()
        or value != value.strip()
    ):
        raise ValueError(f"UI {field} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class UiRecord:
    ui_id: UUID
    display_name: str
    client_type: str
    platform: str
    versions: tuple[tuple[str, str], ...]
    capabilities: tuple[str, ...]
    created_at: datetime
    last_seen_at: datetime
    applied_config_revision: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ui_id": str(self.ui_id),
            "display_name": self.display_name,
            "client_type": self.client_type,
            "platform": self.platform,
            "versions": dict(self.versions),
            "capabilities": list(self.capabilities),
            "created_at": _utc_text(self.created_at),
            "last_seen_at": _utc_text(self.last_seen_at),
            "applied_config_revision": self.applied_config_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> UiRecord:
        expected = {
            "ui_id",
            "display_name",
            "client_type",
            "platform",
            "versions",
            "capabilities",
            "created_at",
            "last_seen_at",
            "applied_config_revision",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("UI record has invalid fields")
        try:
            ui_id = UUID(str(value["ui_id"]))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("UI ID is invalid") from exc
        display_name = _bounded_text(value["display_name"], "display name", MAX_UI_NAME_CHARACTERS)
        client_type = value["client_type"]
        if client_type not in UI_CLIENT_TYPES:
            raise ValueError("UI client type is invalid")
        platform = _bounded_text(value["platform"], "platform", MAX_UI_PLATFORM_CHARACTERS)
        versions = cls._versions(value["versions"])
        capabilities = cls._capabilities(value["capabilities"])
        created_at = _timestamp(value["created_at"], "creation time")
        last_seen_at = _timestamp(value["last_seen_at"], "last-seen time")
        if last_seen_at < created_at:
            raise ValueError("UI last-seen time precedes its creation time")
        revision = value["applied_config_revision"]
        if revision is not None and (
            not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
        ):
            raise ValueError("UI applied configuration revision is invalid")
        return cls(
            ui_id=ui_id,
            display_name=display_name,
            client_type=client_type,
            platform=platform,
            versions=versions,
            capabilities=capabilities,
            created_at=created_at,
            last_seen_at=last_seen_at,
            applied_config_revision=revision,
        )

    @staticmethod
    def _versions(value: object) -> tuple[tuple[str, str], ...]:
        if (
            not isinstance(value, dict)
            or len(value) > MAX_UI_VERSIONS
            or not all(isinstance(name, str) for name in value)
        ):
            raise ValueError("UI versions are invalid")
        versions = tuple(
            sorted(
                (
                    _bounded_text(name, "version name", MAX_UI_VERSION_NAME_CHARACTERS),
                    _bounded_text(version, "version value", MAX_UI_VERSION_VALUE_CHARACTERS),
                )
                for name, version in value.items()
            )
        )
        return versions

    @staticmethod
    def _capabilities(value: object) -> tuple[str, ...]:
        if (
            not isinstance(value, list)
            or len(value) > MAX_UI_CAPABILITIES
            or len(set(item for item in value if isinstance(item, str))) != len(value)
        ):
            raise ValueError("UI capabilities are invalid")
        return tuple(
            sorted(
                _bounded_text(item, "capability", MAX_UI_CAPABILITY_CHARACTERS) for item in value
            )
        )


class UiStore:
    """Persistent Gateway authority for stable native and PWA UI records."""

    def __init__(
        self,
        state_directory: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_directory = Path(state_directory).expanduser()
        self.path = self.state_directory / "uis.json"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._records: dict[UUID, UiRecord] = {}
        self._configuration_reports: dict[UUID, tuple[int, str, tuple[str, ...]]] = {}
        self._lock = asyncio.Lock()
        self._load()

    def get(self, ui_id: UUID) -> UiRecord | None:
        return self._records.get(ui_id)

    def records(self) -> tuple[UiRecord, ...]:
        return tuple(sorted(self._records.values(), key=lambda item: item.display_name.casefold()))

    def configuration_report(self, ui_id: UUID) -> tuple[int, str, tuple[str, ...]] | None:
        return self._configuration_reports.get(ui_id)

    def find_by_name(self, display_name: str) -> UiRecord | None:
        normalized = _bounded_text(display_name, "display name", MAX_UI_NAME_CHARACTERS).casefold()
        return next(
            (
                record
                for record in self._records.values()
                if record.display_name.casefold() == normalized
            ),
            None,
        )

    async def register(
        self,
        *,
        ui_id: UUID | None,
        client_type: str,
        platform: str,
        versions: Mapping[str, str],
        capabilities: Sequence[str],
        applied_config_revision: int | None = None,
    ) -> UiRecord:
        now = self._now()
        candidate = {
            "ui_id": str(ui_id or uuid4()),
            "display_name": "pending",
            "client_type": client_type,
            "platform": platform,
            "versions": dict(versions),
            "capabilities": list(capabilities),
            "created_at": _utc_text(now),
            "last_seen_at": _utc_text(now),
            "applied_config_revision": applied_config_revision,
        }
        validated = UiRecord.from_dict(candidate)
        async with self._lock:
            previous = self._records.get(validated.ui_id)
            if previous is None:
                record = replace(
                    validated,
                    display_name=self._default_name(
                        validated.ui_id, validated.platform, validated.client_type
                    ),
                    created_at=now,
                    last_seen_at=now,
                )
            else:
                record = replace(
                    previous,
                    client_type=validated.client_type,
                    platform=validated.platform,
                    versions=validated.versions,
                    capabilities=validated.capabilities,
                    last_seen_at=max(now, previous.last_seen_at),
                    applied_config_revision=(
                        applied_config_revision
                        if applied_config_revision is not None
                        else previous.applied_config_revision
                    ),
                )
            self._records[record.ui_id] = record
            try:
                self._save()
            except BaseException:
                if previous is None:
                    self._records.pop(record.ui_id, None)
                else:
                    self._records[record.ui_id] = previous
                raise
            return record

    async def register_metadata(self, ui_id: UUID, value: object) -> UiRecord:
        registration = UiRegistration.from_dict(value)
        return await self.register(
            ui_id=ui_id,
            client_type=registration.client_type,
            platform=registration.platform,
            versions=dict(registration.versions),
            capabilities=registration.capabilities,
            applied_config_revision=registration.applied_config_revision,
        )

    async def acknowledge_configuration(self, ui_id: UUID, revision: int) -> UiRecord:
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("UI applied configuration revision is invalid")
        async with self._lock:
            previous = self._records.get(ui_id)
            if previous is None:
                raise ValueError(f"unknown UI ID: {ui_id}")
            if (
                previous.applied_config_revision is not None
                and revision < previous.applied_config_revision
            ):
                return previous
            record = replace(previous, applied_config_revision=revision)
            self._records[ui_id] = record
            try:
                self._save()
            except BaseException:
                self._records[ui_id] = previous
                raise
            return record

    async def update_capabilities(self, ui_id: UUID, capabilities: Sequence[str]) -> UiRecord:
        validated = UiRecord._capabilities(list(capabilities))
        async with self._lock:
            previous = self._records.get(ui_id)
            if previous is None:
                raise ValueError(f"unknown UI ID: {ui_id}")
            record = replace(previous, capabilities=validated)
            self._records[ui_id] = record
            try:
                self._save()
            except BaseException:
                self._records[ui_id] = previous
                raise
            return record

    async def report_configuration(
        self, ui_id: UUID, revision: int, status: str, issues: tuple[str, ...]
    ) -> UiRecord:
        if status == "applied":
            record = await self.acknowledge_configuration(ui_id, revision)
        else:
            record = self._records.get(ui_id)
            if record is None:
                raise ValueError(f"unknown UI ID: {ui_id}")
        previous_report = self._configuration_reports.get(ui_id)
        if previous_report is None or revision >= previous_report[0]:
            self._configuration_reports[ui_id] = (revision, status, issues)
        return record

    async def rename(self, ui_id: UUID, display_name: str) -> UiRecord:
        validated_name = _bounded_text(display_name, "display name", MAX_UI_NAME_CHARACTERS)
        async with self._lock:
            previous = self._records.get(ui_id)
            if previous is None:
                raise ValueError(f"unknown UI ID: {ui_id}")
            return self._rename_locked(previous, validated_name)

    async def rename_named(self, target_name: str, display_name: str) -> UiRecord:
        normalized_target = _bounded_text(
            target_name, "target display name", MAX_UI_NAME_CHARACTERS
        ).casefold()
        validated_name = _bounded_text(display_name, "display name", MAX_UI_NAME_CHARACTERS)
        async with self._lock:
            target = next(
                (
                    record
                    for record in self._records.values()
                    if record.display_name.casefold() == normalized_target
                ),
                None,
            )
            if target is None:
                raise ValueError(f"unknown UI: {target_name}")
            return self._rename_locked(target, validated_name)

    def _rename_locked(self, previous: UiRecord, display_name: str) -> UiRecord:
        if any(
            record.ui_id != previous.ui_id
            and record.display_name.casefold() == display_name.casefold()
            for record in self._records.values()
        ):
            raise ValueError(f"UI display name is already in use: {display_name}")
        if previous.display_name == display_name:
            return previous
        record = replace(previous, display_name=display_name)
        self._records[previous.ui_id] = record
        try:
            self._save()
        except BaseException:
            self._records[previous.ui_id] = previous
            raise
        return record

    async def forget_named(self, target_name: str) -> UiRecord:
        normalized_target = _bounded_text(
            target_name, "target display name", MAX_UI_NAME_CHARACTERS
        ).casefold()
        async with self._lock:
            target = next(
                (
                    record
                    for record in self._records.values()
                    if record.display_name.casefold() == normalized_target
                ),
                None,
            )
            if target is None:
                raise ValueError(f"unknown UI: {target_name}")
            del self._records[target.ui_id]
            try:
                self._save()
            except BaseException:
                self._records[target.ui_id] = target
                raise
            return target

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("UI store clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def _default_name(self, ui_id: UUID, platform: str, client_type: str) -> str:
        type_name = "Terminal" if client_type == "terminal" else "PWA"
        suffix = f" · {type_name}"
        base = f"{platform[: MAX_UI_NAME_CHARACTERS - len(suffix)]}{suffix}"
        names = {record.display_name.casefold() for record in self._records.values()}
        if base.casefold() not in names:
            return base
        for suffix_length in range(6, 33, 2):
            duplicate_suffix = f" · {ui_id.hex[:suffix_length]}"
            candidate = (
                f"{base[: MAX_UI_NAME_CHARACTERS - len(duplicate_suffix)]}{duplicate_suffix}"
            )
            if candidate.casefold() not in names:
                return candidate
        raise ValueError("cannot generate a unique UI display name")

    def _load(self) -> None:
        self._ensure_private_directory()
        try:
            descriptor = self._open_private_file()
        except FileNotFoundError:
            return
        with os.fdopen(descriptor, encoding="utf-8") as state_file:
            try:
                value = json.load(state_file)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError(f"cannot read UI state: {self.path}") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "uis"}:
            raise ValueError("UI state has invalid fields")
        if value["schema_version"] != 1 or not isinstance(value["uis"], list):
            raise ValueError("UI state has an unsupported schema")
        records = [UiRecord.from_dict(item) for item in value["uis"]]
        if len({record.ui_id for record in records}) != len(records):
            raise ValueError("UI state contains duplicate IDs")
        if len({record.display_name.casefold() for record in records}) != len(records):
            raise ValueError("UI state contains duplicate display names")
        self._records = {record.ui_id: record for record in records}

    def _ensure_private_directory(self) -> None:
        try:
            self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ValueError(f"cannot create UI state directory: {self.state_directory}") from exc
        details = self.state_directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode):
            raise ValueError(f"UI state parent is not a directory: {self.state_directory}")
        if os.name == "posix":
            if details.st_uid != os.geteuid():
                raise ValueError(
                    f"UI state directory must be owned by this user: {self.state_directory}"
                )
            if stat.S_IMODE(details.st_mode) & 0o077:
                raise ValueError(
                    f"UI state directory must not grant group or other permissions: "
                    f"{self.state_directory}"
                )

    def _open_private_file(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError(f"cannot open UI state: {self.path}") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"UI state must be a regular file: {self.path}")
            if os.name == "posix":
                if details.st_uid != os.geteuid():
                    raise ValueError(f"UI state must be owned by this user: {self.path}")
                if stat.S_IMODE(details.st_mode) & 0o077:
                    raise ValueError(
                        f"UI state must not grant group or other permissions: {self.path}"
                    )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _save(self) -> None:
        self._ensure_private_directory()
        if self.path.exists() or self.path.is_symlink():
            descriptor = self._open_private_file()
            os.close(descriptor)
        value = {
            "schema_version": 1,
            "uis": [
                record.as_dict()
                for record in sorted(self._records.values(), key=lambda item: str(item.ui_id))
            ],
        }
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
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
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                descriptor = -1
                json.dump(value, state_file, ensure_ascii=True, separators=(",", ":"))
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, self.path)
            if os.name == "posix":
                directory_descriptor = os.open(
                    self.state_directory,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
