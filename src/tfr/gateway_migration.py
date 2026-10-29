from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from tfr.configuration_registry import ConfigurationRegistry, SettingScope
from tfr.gateway_configuration import (
    GatewayConfigurationDocument,
    GatewayConfigurationStore,
)

MAX_MIGRATION_SETTINGS = 2_000
MAX_MIGRATION_PROPOSALS = 64


@dataclass(frozen=True, slots=True)
class MigrationSetting:
    key: str
    scope: str
    target: str | None
    value: object
    source: str
    sensitive: bool = False

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "key": self.key,
            "scope": self.scope,
            "target": self.target,
            "source": self.source,
            "sensitive": self.sensitive,
        }
        value["value"] = None if self.sensitive else copy.deepcopy(self.value)
        if self.sensitive:
            value["present"] = self.value is not None
        return value


@dataclass(frozen=True, slots=True)
class MigrationProposal:
    proposal_id: UUID
    ui_id: UUID
    ui_name: str
    base_revision: int
    submitted_at: str
    settings: tuple[MigrationSetting, ...]
    source_files: tuple[str, ...] = ()
    omitted_keys: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "proposal_id": str(self.proposal_id),
            "ui_id": str(self.ui_id),
            "ui_name": self.ui_name,
            "base_revision": self.base_revision,
            "submitted_at": self.submitted_at,
            "settings": [item.as_dict() for item in self.settings],
            "source_files": list(self.source_files),
            "omitted_keys": list(self.omitted_keys),
        }


def collect_migration_settings(
    values: dict[str, object],
    registry: ConfigurationRegistry,
    *,
    ui_id: UUID,
    source: str,
    ui_scope: str = "ui",
    ui_target: str | None = None,
) -> tuple[MigrationSetting, ...]:
    if len(values) > MAX_MIGRATION_SETTINGS:
        raise ValueError("migration proposal has too many settings")
    expanded: dict[str, object] = {}
    for key, value in values.items():
        if key == "plugins.config" and isinstance(value, dict):
            for plugin, plugin_value in value.items():
                _flatten_plugin_config(expanded, f"plugins.config.{plugin}", plugin_value, registry)
        else:
            expanded[key] = value
    output = []
    for key, value in sorted(expanded.items()):
        descriptor = registry.get(key)
        if descriptor is None:
            continue
        scope = descriptor.scope.value
        target: str | None = None
        if descriptor.scope is SettingScope.UI_TYPE:
            scope = ui_scope
            target = ui_target or str(ui_id)
        elif descriptor.scope is SettingScope.UI:
            scope = "ui"
            target = str(ui_id)
        elif descriptor.scope is SettingScope.WORLD:
            scope = "world"
            parts = key.split(".")
            target = parts[1] if len(parts) > 2 else None
        elif descriptor.scope is SettingScope.PLUGIN:
            scope = "plugin"
            parts = key.split(".")
            target = parts[2] if len(parts) > 3 else None
        elif descriptor.scope is SettingScope.BOOTSTRAP:
            scope = "bootstrap"
        elif descriptor.scope is SettingScope.SECRET:
            scope = "secret"
        output.append(
            MigrationSetting(
                key=key,
                scope=scope,
                target=target,
                value=None if descriptor.sensitivity else copy.deepcopy(value),
                source=source,
                sensitive=descriptor.sensitivity,
            )
        )
    return tuple(output)


def collect_omitted_migration_keys(
    values: dict[str, object], registry: ConfigurationRegistry
) -> tuple[str, ...]:
    """Return leaf paths migration cannot safely interpret; never include values."""

    omitted: list[str] = []
    for key, value in values.items():
        if key == "plugins.config" and isinstance(value, dict):
            for plugin, plugin_value in value.items():
                _collect_unknown_leaves(
                    omitted, f"plugins.config.{plugin}", plugin_value, registry
                )
        elif registry.get(key) is None:
            _collect_unknown_leaves(omitted, key, value, registry)
    return tuple(sorted(set(omitted)))


def _collect_unknown_leaves(
    output: list[str], prefix: str, value: object, registry: ConfigurationRegistry
) -> None:
    if registry.get(prefix) is not None:
        return
    if isinstance(value, dict) and value:
        for name, item in value.items():
            _collect_unknown_leaves(output, f"{prefix}.{name}", item, registry)
    else:
        output.append(prefix)


def _flatten_plugin_config(
    output: dict[str, object],
    prefix: str,
    value: object,
    registry: ConfigurationRegistry,
) -> None:
    if registry.get(prefix) is not None:
        output[prefix] = value
        return
    if isinstance(value, dict):
        for name, item in value.items():
            _flatten_plugin_config(output, f"{prefix}.{name}", item, registry)


class GatewayMigrationStore:
    def __init__(
        self,
        state_directory: Path | str,
        configuration: GatewayConfigurationStore,
    ) -> None:
        self.directory = Path(state_directory).expanduser()
        self.path = self.directory / "gateway-migration.json"
        self.backup_directory = self.directory / "migration-backups"
        self.configuration = configuration
        self.proposals: dict[UUID, MigrationProposal] = {}
        self.applied_revision: int | None = None
        self.applied_at: str | None = None
        self.pre_migration_document: dict[str, object] | None = None
        self.backups: tuple[dict[str, str], ...] = ()
        self._lock = threading.RLock()
        self._load()

    def submit(self, proposal: MigrationProposal) -> None:
        with self._lock:
            if len(proposal.settings) > MAX_MIGRATION_SETTINGS:
                raise ValueError("migration proposal has too many settings")
            if proposal.base_revision > self.configuration.revision:
                raise ValueError("migration proposal is based on a future revision")
            if (
                proposal.ui_id not in self.proposals
                and len(self.proposals) >= MAX_MIGRATION_PROPOSALS
            ):
                raise ValueError("migration proposal limit reached")
            self.proposals[proposal.ui_id] = proposal
            self._save()

    def preview(self, choices: dict[str, str] | None = None) -> dict[str, object]:
        with self._lock:
            return self._preview_locked(choices or {})

    def _preview_locked(self, choices: dict[str, str]) -> dict[str, object]:
        grouped: dict[
            tuple[str, str | None, str], list[tuple[MigrationProposal, MigrationSetting]]
        ] = {}
        for proposal in self.proposals.values():
            for setting in proposal.settings:
                grouped.setdefault((setting.scope, setting.target, setting.key), []).append(
                    (proposal, setting)
                )
        accepted: list[dict[str, object]] = []
        conflicts: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        restart_requirements: list[str] = []
        for proposal in self.proposals.values():
            skipped.extend(
                {
                    "key": key,
                    "reason": "unrecognized setting; retained in the source file but not imported",
                    "source": proposal.ui_name,
                }
                for key in proposal.omitted_keys
            )
        for (scope, target, key), candidates in sorted(grouped.items()):
            if scope in {"bootstrap", "secret"} or any(
                setting.sensitive for _proposal, setting in candidates
            ):
                reason = "secret" if any(
                    setting.sensitive for _proposal, setting in candidates
                ) else scope
                skipped.append({"key": key, "reason": f"{reason} values require separate setup"})
                continue
            managed, _managed_value = self.configuration.get(key, scope=scope, target=target)
            if managed:
                skipped.append(
                    {
                        "key": key,
                        "reason": "already managed; the existing Gateway value is preserved",
                    }
                )
                continue
            unique = {_canonical(item.value) for _proposal, item in candidates}
            renamed_target: str | None = None
            if len(unique) == 1:
                selected = candidates[0]
            elif scope == "ui":
                # UI proposals are already targeted to stable IDs and cannot collide.
                selected = candidates[0]
            else:
                choice = choices.get(key)
                selected_choice = choice
                if choice is not None and choice.startswith("rename:"):
                    try:
                        _marker, selected_choice, renamed_target = choice.split(":", 2)
                    except ValueError:
                        selected_choice = None
                selected = next(
                    (
                        candidate
                        for candidate in candidates
                        if str(candidate[0].ui_id) == selected_choice
                        or candidate[0].ui_name == selected_choice
                    ),
                    None,
                )
                if choice == "skip":
                    skipped.append({"key": key, "reason": "explicitly skipped"})
                    continue
                if selected is None:
                    conflicts.append(
                        {
                            "key": key,
                            "scope": scope,
                            "target": target,
                            "candidates": [
                                {
                                    "ui_id": str(proposal.ui_id),
                                    "ui_name": proposal.ui_name,
                                    "source": setting.source,
                                    "value": None if setting.sensitive else setting.value,
                                }
                                for proposal, setting in candidates
                            ],
                        }
                    )
                    continue
            proposal, setting = selected
            descriptor = self.configuration.registry.get(key)
            if descriptor is None:
                raise ValueError(f"migration setting is no longer registered: {key}")
            restart_requirements.append(descriptor.restart.value)
            accepted_target = renamed_target if len(unique) > 1 and renamed_target else target
            accepted_key = key
            if renamed_target and scope == "world" and target:
                accepted_key = key.replace(f"worlds.{target}.", f"worlds.{renamed_target}.", 1)
            accepted.append(
                {
                    "key": accepted_key,
                    "scope": scope,
                    "target": accepted_target,
                    "value": copy.deepcopy(setting.value),
                    "source": setting.source,
                    "ui_id": str(proposal.ui_id),
                }
            )
        return {
            "base_revision": self.configuration.revision,
            "proposal_count": len(self.proposals),
            "accepted": accepted,
            "conflicts": conflicts,
            "skipped": skipped,
            "restart": _maximum_restart(restart_requirements),
        }

    def apply(
        self,
        *,
        expected_revision: int,
        choices: dict[str, str] | None,
        actor_ui_id: str,
        actor_ui_name: str,
        secret_references: dict[str, str] | None = None,
    ) -> int:
        with self._lock:
            if expected_revision != self.configuration.revision:
                raise ValueError("configuration changed after migration proposals were collected")
            if any(
                proposal.base_revision != expected_revision for proposal in self.proposals.values()
            ):
                raise ValueError("migration proposal is stale; submit it again")
            preview = self._preview_locked(choices or {})
            if preview["conflicts"]:
                raise ValueError("migration has unresolved conflicts")
            proposed = self.configuration.document
            for item in preview["accepted"]:
                if not isinstance(item, dict):
                    raise ValueError("migration preview is invalid")
                destination = self.configuration._section(
                    proposed,
                    str(item["scope"]),
                    item["target"] if isinstance(item["target"], str) else None,
                    create=True,
                )
                if destination is None:
                    raise ValueError("migration destination is unavailable")
                destination[str(item["key"])] = copy.deepcopy(item["value"])
            for key, reference in (secret_references or {}).items():
                proposed.secret_references[key] = reference
            previous_state = (
                self.pre_migration_document,
                self.backups,
                self.applied_revision,
                self.applied_at,
            )
            self.pre_migration_document = self.configuration.document.as_dict()
            self.backups = self._backup_sources()
            self.applied_revision = expected_revision + 1
            self.applied_at = _now()
            self._save()
            try:
                mutation = self.configuration.replace_for_migration(
                    proposed,
                    expected_revision=expected_revision,
                    actor_ui_id=actor_ui_id,
                    actor_ui_name=actor_ui_name,
                )
            except BaseException:
                (
                    self.pre_migration_document,
                    self.backups,
                    self.applied_revision,
                    self.applied_at,
                ) = previous_state
                self._save()
                raise
            return mutation.revision

    def rollback(self, *, expected_revision: int, actor_ui_id: str, actor_ui_name: str) -> int:
        with self._lock:
            if self.pre_migration_document is None or self.applied_revision is None:
                raise ValueError("there is no applied migration to roll back")
            if expected_revision != self.configuration.revision:
                raise ValueError("configuration changed after migration; rollback is unsafe")
            previous = GatewayConfigurationDocument.from_dict(self.pre_migration_document)
            mutation = self.configuration.replace_for_migration(
                previous,
                expected_revision=expected_revision,
                actor_ui_id=actor_ui_id,
                actor_ui_name=actor_ui_name,
                operation="rollback-migration",
            )
            self.applied_revision = None
            self.applied_at = None
            self._save()
            return mutation.revision

    def status(self) -> dict[str, object]:
        return {
            "revision": self.configuration.revision,
            "proposal_count": len(self.proposals),
            "proposals": [proposal.as_dict() for proposal in self.proposals.values()],
            "applied_revision": self.applied_revision,
            "applied_at": self.applied_at,
            "backups": list(self.backups),
        }

    def _backup_sources(self) -> tuple[dict[str, str], ...]:
        sources = sorted(
            {
                Path(source).expanduser()
                for proposal in self.proposals.values()
                for source in proposal.source_files
            },
            key=str,
        )
        if not sources:
            return ()
        destination = self.backup_directory / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination.mkdir(parents=True, mode=0o700)
        output = []
        for index, source in enumerate(sources):
            if not source.is_file() or source.is_symlink():
                continue
            target = destination / f"{index:03d}-{source.name}"
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
            output.append(
                {
                    "source": str(source),
                    "backup": str(target),
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            )
        return tuple(output)

    def _load(self) -> None:
        if not self.path.exists():
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("Gateway migration state is invalid")
        self.applied_revision = value.get("applied_revision")
        self.applied_at = value.get("applied_at")
        self.pre_migration_document = value.get("pre_migration_document")
        self.backups = tuple(value.get("backups", ()))
        for raw in value.get("proposals", []):
            proposal = _proposal_from_dict(raw)
            self.proposals[proposal.ui_id] = proposal
        if (
            self.applied_revision is not None
            and self.applied_revision > self.configuration.revision
        ):
            self.applied_revision = None
            self.applied_at = None
            self.pre_migration_document = None
            self.backups = ()
            self._save()

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        value = {
            "schema_version": 1,
            "proposals": [proposal.as_dict() for proposal in self.proposals.values()],
            "applied_revision": self.applied_revision,
            "applied_at": self.applied_at,
            "pre_migration_document": self.pre_migration_document,
            "backups": list(self.backups),
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".migration.", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            Path(temporary).unlink(missing_ok=True)


def _proposal_from_dict(value: object) -> MigrationProposal:
    if not isinstance(value, dict):
        raise ValueError("migration proposal is invalid")
    settings = tuple(
        MigrationSetting(
            key=str(item["key"]),
            scope=str(item["scope"]),
            target=item.get("target"),
            value=item.get("value"),
            source=str(item["source"]),
            sensitive=bool(item.get("sensitive", False)),
        )
        for item in value.get("settings", [])
        if isinstance(item, dict)
    )
    return MigrationProposal(
        proposal_id=UUID(str(value["proposal_id"])),
        ui_id=UUID(str(value["ui_id"])),
        ui_name=str(value["ui_name"]),
        base_revision=int(value["base_revision"]),
        submitted_at=str(value["submitted_at"]),
        settings=settings,
        source_files=tuple(str(item) for item in value.get("source_files", [])),
        omitted_keys=tuple(str(item) for item in value.get("omitted_keys", [])),
    )


def new_proposal(
    *,
    ui_id: UUID,
    ui_name: str,
    base_revision: int,
    settings: tuple[MigrationSetting, ...],
    source_files: tuple[str, ...] = (),
    omitted_keys: tuple[str, ...] = (),
) -> MigrationProposal:
    return MigrationProposal(
        uuid4(), ui_id, ui_name, base_revision, _now(), settings, source_files, omitted_keys
    )


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _maximum_restart(values: list[str]) -> str:
    if not values:
        return "live"
    ranks = {"live": 0, "client-reload": 1, "client-restart": 2, "gateway-restart": 3}
    return max(values, key=lambda value: ranks.get(value, 4))


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
