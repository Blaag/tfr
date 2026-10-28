from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import secrets
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from tfr.config import AgentsConfig, ConfigurationBundle, MainConfig, WorldsConfig
from tfr.configuration_registry import ConfigurationRegistry, SettingScope

MAX_GATEWAY_CONFIGURATION_BYTES = 1024 * 1024
MAX_GATEWAY_AUDIT_ENTRIES = 500
MAX_GATEWAY_BATCH_CHANGES = 64


class GatewayConfigurationError(ValueError):
    pass


class GatewayConfigurationRevisionConflict(GatewayConfigurationError):
    pass


@dataclass(slots=True)
class GatewayConfigurationDocument:
    schema_version: int = 1
    revision: int = 0
    shared: dict[str, object] = field(default_factory=dict)
    gateway: dict[str, object] = field(default_factory=dict)
    ui_types: dict[str, dict[str, object]] = field(default_factory=dict)
    uis: dict[str, dict[str, object]] = field(default_factory=dict)
    worlds: dict[str, dict[str, object]] = field(default_factory=dict)
    plugins: dict[str, dict[str, object]] = field(default_factory=dict)
    secret_references: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "shared": copy.deepcopy(self.shared),
            "gateway": copy.deepcopy(self.gateway),
            "ui_types": copy.deepcopy(self.ui_types),
            "uis": copy.deepcopy(self.uis),
            "worlds": copy.deepcopy(self.worlds),
            "plugins": copy.deepcopy(self.plugins),
            "secret_references": copy.deepcopy(self.secret_references),
        }

    @classmethod
    def from_dict(cls, value: object) -> GatewayConfigurationDocument:
        fields = {
            "schema_version",
            "revision",
            "shared",
            "gateway",
            "ui_types",
            "uis",
            "worlds",
            "plugins",
            "secret_references",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise GatewayConfigurationError("Gateway configuration document has invalid fields")
        if value["schema_version"] != 1:
            raise GatewayConfigurationError("Gateway configuration schema is unsupported")
        revision = value["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise GatewayConfigurationError("Gateway configuration revision is invalid")
        for name in fields - {"schema_version", "revision"}:
            if not isinstance(value[name], dict):
                raise GatewayConfigurationError(f"Gateway configuration {name} must be an object")
        for name in ("ui_types", "uis", "worlds", "plugins"):
            if not all(
                isinstance(key, str) and isinstance(item, dict) for key, item in value[name].items()
            ):
                raise GatewayConfigurationError(
                    f"Gateway configuration {name} entries must be objects"
                )
        return cls(
            schema_version=1,
            revision=revision,
            shared=copy.deepcopy(value["shared"]),
            gateway=copy.deepcopy(value["gateway"]),
            ui_types=copy.deepcopy(value["ui_types"]),
            uis=copy.deepcopy(value["uis"]),
            worlds=copy.deepcopy(value["worlds"]),
            plugins=copy.deepcopy(value["plugins"]),
            secret_references=copy.deepcopy(value["secret_references"]),
        )


@dataclass(frozen=True, slots=True)
class ConfigurationMutation:
    revision: int
    operation: str
    key: str
    scope: str
    target: str | None
    previous_present: bool
    previous_value: object
    value_present: bool
    value: object
    actor_ui_id: str
    actor_ui_name: str
    timestamp: str
    restart: str

    def as_dict(self, *, redact: bool = False) -> dict[str, object]:
        value = {
            "revision": self.revision,
            "operation": self.operation,
            "key": self.key,
            "scope": self.scope,
            "target": self.target,
            "actor_ui_id": self.actor_ui_id,
            "actor_ui_name": self.actor_ui_name,
            "timestamp": self.timestamp,
            "restart": self.restart,
        }
        if redact:
            value["previous_value"] = None
            value["value"] = None
            value["redacted"] = True
        else:
            value["previous_value"] = copy.deepcopy(self.previous_value)
            value["value"] = copy.deepcopy(self.value)
        value["previous_present"] = self.previous_present
        value["value_present"] = self.value_present
        return value


class GatewayConfigurationStore:
    def __init__(
        self,
        state_directory: Path | str,
        registry: ConfigurationRegistry,
        *,
        defer_plugin_validation: bool = False,
    ) -> None:
        self.state_directory = Path(state_directory).expanduser()
        self.path = self.state_directory / "gateway-configuration.json"
        self.previous_path = self.state_directory / "gateway-configuration.previous.json"
        self.audit_path = self.state_directory / "gateway-configuration-audit.jsonl"
        self.lock_path = self.state_directory / ".gateway-configuration.lock"
        self.registry = registry
        self._lock = threading.RLock()
        self._ensure_private_directory()
        self._document = self._load()
        self._validate_document(self._document, defer_plugin_validation=defer_plugin_validation)
        self._history = self._load_audit()

    @property
    def revision(self) -> int:
        return self._document.revision

    @property
    def document(self) -> GatewayConfigurationDocument:
        return GatewayConfigurationDocument.from_dict(self._document.as_dict())

    def get(self, key: str, *, scope: str, target: str | None = None) -> tuple[bool, object]:
        section = self._section(self._document, scope, target, create=False)
        if section is None or key not in section:
            return False, None
        return True, copy.deepcopy(section[key])

    def preview(
        self,
        key: str,
        value: object,
        *,
        scope: str,
        target: str | None,
        reset: bool = False,
    ) -> dict[str, object]:
        descriptor = self._descriptor(key, scope)
        self._validate_target_key(key, scope, target)
        if not reset:
            descriptor.validate(value)
        return {
            "key": key,
            "scope": scope,
            "target": target,
            "operation": "reset" if reset else "set",
            "restart": descriptor.restart.value,
            "requires_confirmation": True,
            "revision": self.revision,
        }

    def set(
        self,
        key: str,
        value: object,
        *,
        scope: str,
        target: str | None,
        expected_revision: int,
        actor_ui_id: str,
        actor_ui_name: str,
    ) -> ConfigurationMutation:
        descriptor = self._descriptor(key, scope)
        self._validate_target_key(key, scope, target)
        descriptor.validate(value)
        return self._mutate(
            "set",
            key,
            value,
            scope=scope,
            target=target,
            expected_revision=expected_revision,
            actor_ui_id=actor_ui_id,
            actor_ui_name=actor_ui_name,
            restart=descriptor.restart.value,
        )

    def reset(
        self,
        key: str,
        *,
        scope: str,
        target: str | None,
        expected_revision: int,
        actor_ui_id: str,
        actor_ui_name: str,
    ) -> ConfigurationMutation:
        descriptor = self._descriptor(key, scope)
        self._validate_target_key(key, scope, target)
        return self._mutate(
            "reset",
            key,
            None,
            scope=scope,
            target=target,
            expected_revision=expected_revision,
            actor_ui_id=actor_ui_id,
            actor_ui_name=actor_ui_name,
            restart=descriptor.restart.value,
        )

    def preview_batch(self, changes: list[dict[str, object]]) -> dict[str, object]:
        normalized = self._normalize_batch(changes)
        return {
            "operation": "batch",
            "changes": [
                {
                    "operation": operation,
                    "key": key,
                    "scope": scope,
                    "target": target,
                    "restart": descriptor.restart.value,
                }
                for operation, key, _value, scope, target, descriptor in normalized
            ],
            "restart": _maximum_restart(
                descriptor.restart.value for *_prefix, descriptor in normalized
            ),
            "requires_confirmation": True,
            "revision": self.revision,
        }

    def batch(
        self,
        changes: list[dict[str, object]],
        *,
        expected_revision: int,
        actor_ui_id: str,
        actor_ui_name: str,
    ) -> tuple[ConfigurationMutation, ...]:
        normalized = self._normalize_batch(changes)
        with self._mutation_lock():
            self._check_revision(expected_revision)
            proposed = GatewayConfigurationDocument.from_dict(self._document.as_dict())
            revision = self.revision + 1
            timestamp = _utc_now()
            mutations = []
            seen: set[tuple[str, str | None, str]] = set()
            for operation, key, value, scope, target, descriptor in normalized:
                identity = (scope, target, key)
                if identity in seen:
                    raise GatewayConfigurationError(f"batch changes {key} more than once")
                seen.add(identity)
                current = self._section(self._document, scope, target, create=False) or {}
                previous_present = key in current
                previous_value = copy.deepcopy(current.get(key))
                destination = self._section(proposed, scope, target, create=True)
                if destination is None:
                    raise GatewayConfigurationError("configuration scope is unavailable")
                if operation == "set":
                    destination[key] = copy.deepcopy(value)
                    value_present = True
                else:
                    destination.pop(key, None)
                    value_present = False
                if previous_present == value_present and previous_value == value:
                    raise GatewayConfigurationError(f"batch change for {key} would have no effect")
                mutations.append(
                    ConfigurationMutation(
                        revision=revision,
                        operation=operation,
                        key=key,
                        scope=scope,
                        target=target,
                        previous_present=previous_present,
                        previous_value=previous_value,
                        value_present=value_present,
                        value=copy.deepcopy(value),
                        actor_ui_id=actor_ui_id,
                        actor_ui_name=actor_ui_name,
                        timestamp=timestamp,
                        restart=descriptor.restart.value,
                    )
                )
            proposed.revision = revision
            self._validate_document(proposed)
            self._commit(proposed, tuple(mutations))
            return tuple(mutations)

    def _normalize_batch(
        self, changes: list[dict[str, object]]
    ) -> list[tuple[str, str, object, str, str | None, object]]:
        if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_GATEWAY_BATCH_CHANGES:
            raise GatewayConfigurationError("configuration batch must contain 1-64 changes")
        output = []
        for change in changes:
            if not isinstance(change, dict):
                raise GatewayConfigurationError("configuration batch change must be an object")
            operation = change.get("operation")
            key = change.get("key")
            scope = change.get("scope")
            target = change.get("target")
            value = change.get("value")
            allowed = {"operation", "key", "scope", "target"}
            if operation == "set":
                allowed.add("value")
            if set(change) != allowed or operation not in {"set", "reset"}:
                raise GatewayConfigurationError("configuration batch change has invalid fields")
            if not isinstance(key, str) or not isinstance(scope, str):
                raise GatewayConfigurationError("configuration batch key and scope must be strings")
            if target is not None and not isinstance(target, str):
                raise GatewayConfigurationError(
                    "configuration batch target must be a string or null"
                )
            descriptor = self._descriptor(key, scope)
            self._validate_target_key(key, scope, target)
            if operation == "set":
                descriptor.validate(value)
            output.append((operation, key, value, scope, target, descriptor))
        return output

    def undo(
        self,
        *,
        expected_revision: int,
        actor_ui_id: str,
        actor_ui_name: str,
    ) -> ConfigurationMutation:
        with self._mutation_lock():
            self._check_revision(expected_revision)
            previous_changes = [
                item for item in self._history if item.revision == self.revision
            ]
            if not previous_changes:
                raise GatewayConfigurationError(
                    "there is no committed configuration change to undo"
                )
            previous = previous_changes[-1]
            if len(previous_changes) > 1:
                proposed = GatewayConfigurationDocument.from_dict(self._document.as_dict())
                for item in reversed(previous_changes):
                    destination = self._section(proposed, item.scope, item.target, create=True)
                    if destination is None:
                        raise GatewayConfigurationError("configuration undo scope is unavailable")
                    if item.previous_present:
                        destination[item.key] = copy.deepcopy(item.previous_value)
                    else:
                        destination.pop(item.key, None)
                proposed.revision += 1
                self._validate_document(proposed)
                mutation = ConfigurationMutation(
                    revision=proposed.revision,
                    operation="undo",
                    key="batch",
                    scope="shared",
                    target=None,
                    previous_present=True,
                    previous_value={"revision": self._document.revision},
                    value_present=True,
                    value={"revision": proposed.revision, "changes": len(previous_changes)},
                    actor_ui_id=actor_ui_id,
                    actor_ui_name=actor_ui_name,
                    timestamp=_utc_now(),
                    restart=_maximum_restart(item.restart for item in previous_changes),
                )
                self._commit(proposed, mutation)
                return mutation
            section = self._section(self._document, previous.scope, previous.target, create=False)
            section = section or {}
            current_present = previous.key in section
            current_value = copy.deepcopy(section.get(previous.key))
            proposed = GatewayConfigurationDocument.from_dict(self._document.as_dict())
            destination = self._section(proposed, previous.scope, previous.target, create=True)
            if destination is None:
                raise GatewayConfigurationError("configuration undo scope is unavailable")
            if previous.previous_present:
                destination[previous.key] = copy.deepcopy(previous.previous_value)
            else:
                destination.pop(previous.key, None)
            proposed.revision += 1
            self._validate_document(proposed)
            mutation = ConfigurationMutation(
                revision=proposed.revision,
                operation="undo",
                key=previous.key,
                scope=previous.scope,
                target=previous.target,
                previous_present=current_present,
                previous_value=current_value,
                value_present=previous.previous_present,
                value=copy.deepcopy(previous.previous_value),
                actor_ui_id=actor_ui_id,
                actor_ui_name=actor_ui_name,
                timestamp=_utc_now(),
                restart=previous.restart,
            )
            self._commit(proposed, mutation)
            return mutation

    def changes(self, *, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 500:
            raise GatewayConfigurationError("configuration change limit must be 1-500")
        output = []
        for mutation in self._history[-limit:]:
            descriptor = self.registry.get(mutation.key)
            output.append(mutation.as_dict(redact=descriptor.sensitivity if descriptor else True))
        return output

    def mutations_at_revision(self, revision: int) -> tuple[ConfigurationMutation, ...]:
        return tuple(item for item in self._history if item.revision == revision)

    def export(self, *, include_secret_references: bool = False) -> dict[str, object]:
        value = self.document.as_dict()
        if not include_secret_references:
            value["secret_references"] = {}
        return value

    def validate_current(self, *, defer_plugin_validation: bool = False) -> None:
        self._validate_document(
            self._document,
            defer_plugin_validation=defer_plugin_validation,
        )

    def replace_for_migration(
        self,
        document: GatewayConfigurationDocument,
        *,
        expected_revision: int,
        actor_ui_id: str,
        actor_ui_name: str,
        operation: str = "migration",
    ) -> ConfigurationMutation:
        """Atomically replace all managed sections as one audited revision."""

        with self._mutation_lock():
            self._check_revision(expected_revision)
            proposed = GatewayConfigurationDocument.from_dict(document.as_dict())
            proposed.revision = self.revision + 1
            self._validate_document(proposed)
            mutation = ConfigurationMutation(
                revision=proposed.revision,
                operation=operation,
                key="migration",
                scope="shared",
                target=None,
                previous_present=True,
                previous_value={"revision": self._document.revision},
                value_present=True,
                value={"revision": proposed.revision},
                actor_ui_id=actor_ui_id,
                actor_ui_name=actor_ui_name,
                timestamp=_utc_now(),
                restart="client-reload",
            )
            self._commit(proposed, mutation)
            return mutation

    def _mutate(
        self,
        operation: str,
        key: str,
        value: object,
        *,
        scope: str,
        target: str | None,
        expected_revision: int,
        actor_ui_id: str,
        actor_ui_name: str,
        restart: str,
    ) -> ConfigurationMutation:
        with self._mutation_lock():
            self._check_revision(expected_revision)
            section = self._section(self._document, scope, target, create=False)
            section = section or {}
            previous_present = key in section
            previous_value = copy.deepcopy(section.get(key))
            proposed = GatewayConfigurationDocument.from_dict(self._document.as_dict())
            destination = self._section(proposed, scope, target, create=True)
            if destination is None:
                raise GatewayConfigurationError("configuration scope is unavailable")
            if operation == "set":
                destination[key] = copy.deepcopy(value)
                value_present = True
            else:
                destination.pop(key, None)
                value_present = False
            if previous_present == value_present and previous_value == value:
                raise GatewayConfigurationError("configuration change would have no effect")
            proposed.revision += 1
            self._validate_document(proposed)
            mutation = ConfigurationMutation(
                revision=proposed.revision,
                operation=operation,
                key=key,
                scope=scope,
                target=target,
                previous_present=previous_present,
                previous_value=previous_value,
                value_present=value_present,
                value=copy.deepcopy(value),
                actor_ui_id=actor_ui_id,
                actor_ui_name=actor_ui_name,
                timestamp=_utc_now(),
                restart=restart,
            )
            self._commit(proposed, mutation)
            return mutation

    def _commit(
        self,
        document: GatewayConfigurationDocument,
        mutation: ConfigurationMutation | tuple[ConfigurationMutation, ...],
    ) -> None:
        mutations = mutation if isinstance(mutation, tuple) else (mutation,)
        encoded = _encode(document.as_dict())
        previous = _encode(self._document.as_dict())
        self._write_atomic(self.previous_path, previous)
        self._write_atomic(self.path, encoded)
        try:
            self._append_audit(mutations)
        except BaseException:
            self._write_atomic(self.path, previous)
            raise
        self._document = document
        self._history.extend(mutations)
        self._history = self._history[-MAX_GATEWAY_AUDIT_ENTRIES:]

    def _descriptor(self, key: str, scope: str):
        descriptor = self.registry.get(key)
        if descriptor is None:
            raise GatewayConfigurationError(self.registry.unknown_key_message(key))
        allowed = {
            SettingScope.SHARED: {"shared"},
            SettingScope.GATEWAY: {"gateway"},
            SettingScope.UI_TYPE: {"ui-type", "ui"},
            SettingScope.UI: {"ui"},
            SettingScope.WORLD: {"world"},
            SettingScope.PLUGIN: {"plugin"},
            SettingScope.SECRET: {"secret"},
            SettingScope.BOOTSTRAP: set(),
        }[descriptor.scope]
        if scope not in allowed:
            raise GatewayConfigurationError(f"setting {key} cannot be changed in {scope} scope")
        return descriptor

    @staticmethod
    def _validate_target_key(key: str, scope: str, target: str | None) -> None:
        if scope == "world" and target is not None and not key.startswith(f"worlds.{target}."):
            raise GatewayConfigurationError("world target does not match the setting key")
        if (
            scope == "plugin"
            and target is not None
            and not key.startswith(f"plugins.config.{target}.")
        ):
            raise GatewayConfigurationError("plugin target does not match the setting key")
        if scope == "ui-type" and target not in {"terminal", "pwa"}:
            raise GatewayConfigurationError("UI-type target must be terminal or pwa")

    def _section(
        self,
        document: GatewayConfigurationDocument,
        scope: str,
        target: str | None,
        *,
        create: bool,
    ) -> dict[str, object] | None:
        if scope == "shared":
            if target is not None:
                raise GatewayConfigurationError("shared scope does not accept a target")
            return document.shared
        if scope == "gateway":
            if target is not None:
                raise GatewayConfigurationError("gateway scope does not accept a target")
            return document.gateway
        mappings = {
            "ui-type": document.ui_types,
            "ui": document.uis,
            "world": document.worlds,
            "plugin": document.plugins,
        }
        if scope == "secret":
            if target is not None:
                raise GatewayConfigurationError("secret scope does not accept a target")
            return document.secret_references
        mapping = mappings.get(scope)
        if mapping is None:
            raise GatewayConfigurationError(f"unsupported configuration scope: {scope}")
        if not isinstance(target, str) or not target:
            raise GatewayConfigurationError(f"{scope} scope requires a target")
        if create:
            return mapping.setdefault(target, {})
        return mapping.get(target)

    def _validate_document(
        self,
        document: GatewayConfigurationDocument,
        *,
        defer_plugin_validation: bool = False,
    ) -> None:
        for scope, _target, section in self._sections(document):
            for key, value in section.items():
                if defer_plugin_validation and scope == "plugin" and self.registry.get(key) is None:
                    continue
                descriptor = self._descriptor(key, scope)
                self._validate_target_key(key, scope, _target)
                if scope == "secret" and (
                    not isinstance(value, str)
                    or not value.startswith(("keychain:", "env:", "file:"))
                ):
                    raise GatewayConfigurationError(
                        "secret settings must use a keychain:, env:, or file: reference"
                    )
                descriptor.validate(value)
        if len(_encode(document.as_dict())) > MAX_GATEWAY_CONFIGURATION_BYTES:
            raise GatewayConfigurationError("Gateway configuration document exceeds 1 MiB")

    @staticmethod
    def _sections(document: GatewayConfigurationDocument):
        yield "shared", None, document.shared
        yield "gateway", None, document.gateway
        yield "secret", None, document.secret_references
        for scope, mapping in (
            ("ui-type", document.ui_types),
            ("ui", document.uis),
            ("world", document.worlds),
            ("plugin", document.plugins),
        ):
            for target, section in mapping.items():
                yield scope, target, section

    def _check_revision(self, expected: int) -> None:
        if expected != self.revision:
            raise GatewayConfigurationRevisionConflict(
                f"Gateway configuration revision is {self.revision}, not {expected}"
            )

    def _load(self) -> GatewayConfigurationDocument:
        try:
            raw = self._read_private(self.path)
        except FileNotFoundError:
            return GatewayConfigurationDocument()
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GatewayConfigurationError("Gateway configuration document is invalid") from exc
        if isinstance(value, dict) and value.get("schema_version") == 0:
            value = self._migrate_v0(value)
            self._write_atomic(self.previous_path, raw)
            self._write_atomic(self.path, _encode(value))
        return GatewayConfigurationDocument.from_dict(value)

    @staticmethod
    def _migrate_v0(value: dict[str, object]) -> dict[str, object]:
        if set(value) != {"schema_version", "revision", "settings"} or not isinstance(
            value["settings"], dict
        ):
            raise GatewayConfigurationError("legacy Gateway configuration is invalid")
        return {
            "schema_version": 1,
            "revision": value["revision"],
            "shared": value["settings"],
            "gateway": {},
            "ui_types": {},
            "uis": {},
            "worlds": {},
            "plugins": {},
            "secret_references": {},
        }

    def _load_audit(self) -> list[ConfigurationMutation]:
        try:
            raw = self._read_private(self.audit_path)
        except FileNotFoundError:
            return []
        output = []
        try:
            for line in raw.decode().splitlines():
                value = json.loads(line)
                output.append(ConfigurationMutation(**value))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise GatewayConfigurationError("Gateway configuration audit is invalid") from exc
        return output[-MAX_GATEWAY_AUDIT_ENTRIES:]

    def _append_audit(self, mutations: tuple[ConfigurationMutation, ...]) -> None:
        existing = b""
        with contextlib.suppress(FileNotFoundError):
            existing = self._read_private(self.audit_path)
        newest = [_encode(mutation.as_dict()).rstrip(b"\n") for mutation in mutations]
        total = sum(len(line) + 1 for line in newest)
        if total > MAX_GATEWAY_CONFIGURATION_BYTES:
            raise GatewayConfigurationError("Gateway configuration audit entries are too large")
        lines = list(newest)
        for line in reversed(existing.splitlines()):
            if (
                len(lines) >= MAX_GATEWAY_AUDIT_ENTRIES
                or total + len(line) + 1 > MAX_GATEWAY_CONFIGURATION_BYTES
            ):
                break
            lines.insert(0, line)
            total += len(line) + 1
        self._write_atomic(self.audit_path, b"\n".join(lines) + b"\n")

    @contextlib.contextmanager
    def _mutation_lock(self):
        with self._lock:
            flags = (
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(self.lock_path, flags, 0o600)
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode) or (
                    os.name == "posix"
                    and (details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077)
                ):
                    raise GatewayConfigurationError(
                        "Gateway configuration lock must be private and owner-controlled"
                    )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._document = self._load()
                self._history = self._load_audit()
                yield
            finally:
                os.close(descriptor)

    def _ensure_private_directory(self) -> None:
        self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        details = self.state_directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode) or (
            os.name == "posix"
            and (details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077)
        ):
            raise GatewayConfigurationError(
                "Gateway configuration directory must be private and owner-controlled"
            )

    def _read_private(self, path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or (
                os.name == "posix"
                and (details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077)
            ):
                raise GatewayConfigurationError(
                    "Gateway configuration files must be private regular files"
                )
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                value = stream.read(MAX_GATEWAY_CONFIGURATION_BYTES + 1)
            if len(value) > MAX_GATEWAY_CONFIGURATION_BYTES:
                raise GatewayConfigurationError("Gateway configuration file exceeds 1 MiB")
            return value
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _write_atomic(self, destination: Path, value: bytes) -> None:
        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory = os.open(
                self.state_directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
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


def _encode(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _maximum_restart(values) -> str:
    ranks = {"live": 0, "client-reload": 1, "client-restart": 2, "gateway-restart": 3}
    return max(values, key=lambda value: ranks.get(value, 4))


def apply_gateway_configuration(
    bundle: ConfigurationBundle,
    document: GatewayConfigurationDocument,
) -> ConfigurationBundle:
    """Apply persisted desired values before runtime objects are constructed."""

    main = bundle.main.model_dump(mode="python", by_alias=True)
    worlds = bundle.worlds.model_dump(mode="python", by_alias=True)
    agents = bundle.agents.model_dump(mode="python", by_alias=True)
    for scope, mapping in (
        ("shared", document.shared),
        ("gateway", document.gateway),
    ):
        del scope
        for key, value in mapping.items():
            _apply_key(main, worlds, agents, key, value)
    for mapping in (document.worlds, document.plugins):
        for section in mapping.values():
            for key, value in section.items():
                _apply_key(main, worlds, agents, key, value)
    for key, reference in document.secret_references.items():
        if not isinstance(reference, str):  # already enforced by document validation
            raise GatewayConfigurationError("secret reference is invalid")
        _apply_key(main, worlds, agents, key, _resolve_secret_reference(reference))
    try:
        return bundle.model_copy(
            update={
                "main": MainConfig.model_validate(main),
                "worlds": WorldsConfig.model_validate(worlds),
                "agents": AgentsConfig.model_validate(agents),
            }
        )
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)[:5]
        )
        raise GatewayConfigurationError(
            "Gateway configuration produces invalid effective configuration"
            + (f": {details}" if details else "")
        ) from exc


def _apply_key(
    main: dict[str, object],
    worlds: dict[str, object],
    agents: dict[str, object],
    key: str,
    value: object,
) -> None:
    segments = key.split(".")
    if segments[0] == "worlds":
        destination = worlds
        if len(segments) > 1 and segments[1] in {"$schema", "schema_version", "defaults"}:
            segments = segments[1:]
    elif segments[0] in {"providers", "agents"}:
        destination = agents
    else:
        destination = main
    current: dict[str, object] = destination
    for segment in segments[:-1]:
        child = current.get(segment)
        if not isinstance(child, dict):
            child = {}
            current[segment] = child
        current = child
    current[segments[-1]] = copy.deepcopy(value)


def _resolve_secret_reference(reference: str) -> str:
    """Resolve a protected reference only while constructing Gateway runtime state."""

    scheme, separator, location = reference.partition(":")
    if not separator or not location:
        raise GatewayConfigurationError("secret reference is invalid")
    try:
        if scheme == "env":
            value = os.environ[location]
        elif scheme == "file":
            path = Path(location).expanduser()
            details = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(details.st_mode) or (
                os.name == "posix" and stat.S_IMODE(details.st_mode) & 0o077
            ):
                raise GatewayConfigurationError("secret reference file must be private")
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        elif scheme == "keychain" and sys.platform == "darwin":
            service, split, account = location.partition(":")
            if not split or not service or not account:
                raise GatewayConfigurationError(
                    "keychain references must be keychain:SERVICE:ACCOUNT"
                )
            value = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    service,
                    "-a",
                    account,
                    "-w",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.rstrip("\r\n")
        else:
            raise GatewayConfigurationError("secret reference scheme is unavailable")
    except (KeyError, OSError, subprocess.SubprocessError) as exc:
        raise GatewayConfigurationError("secret reference could not be resolved") from exc
    if not value or len(value) > 65_536:
        raise GatewayConfigurationError("resolved secret has an invalid size")
    return value
