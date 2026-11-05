from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from tfr.config import ConfigurationBundle
from tfr.gateway_configuration import (
    GatewayConfigurationDocument,
    GatewayConfigurationError,
    GatewayConfigurationStore,
    _resolve_secret_reference,
    apply_gateway_configuration,
)
from tfr.gateway_uis import UiRecord, UiStore

PORTABLE_CONFIGURATION_FORMAT = "tfr-gateway-configuration"
PORTABLE_CONFIGURATION_VERSION = 1
MAX_PORTABLE_CONFIGURATION_BYTES = 4 * 1024 * 1024
MAX_PORTABLE_SECRET_BYTES = 65_536

_RESTART_RANK = {"live": 0, "client-reload": 1, "client-restart": 2, "gateway-restart": 3}


@dataclass(frozen=True, slots=True)
class ImportProposal:
    document: GatewayConfigurationDocument
    digest: str
    preview: dict[str, object]
    pending_profiles: tuple[dict[str, object], ...]
    profile_links: tuple[tuple[UUID, UUID], ...]
    secret_values: dict[str, str]


class GatewayConfigurationTransfer:
    """Portable export/import, recovery state, and imported UI profile management."""

    def __init__(
        self,
        state_directory: Path | str,
        configuration: GatewayConfigurationStore,
        ui_store: UiStore,
        base_bundle: ConfigurationBundle,
    ) -> None:
        self.directory = Path(state_directory).expanduser()
        self.configuration = configuration
        self.ui_store = ui_store
        self.base_bundle = base_bundle
        self.backup_directory = self.directory / "configuration-import-backups"
        self.secret_directory = self.directory / "imported-secrets"
        self.state_path = self.directory / "configuration-import-state.json"
        self._state = self._load_state()

    def export(self, *, include_secrets: bool = False) -> dict[str, object]:
        document = self.configuration.document
        records = self.ui_store.records()
        by_profile: dict[str, UiRecord] = {}
        for record in records:
            profile_id = self.ui_store.configuration_profile_id(record.ui_id)
            by_profile[str(profile_id)] = record
        pending = {
            str(item["profile_id"]): item
            for item in self._state.get("pending_profiles", [])
            if isinstance(item, dict) and isinstance(item.get("profile_id"), str)
        }
        named_uis = []
        profile_ids = set(document.uis) | set(by_profile) | set(pending)
        for profile_id in sorted(profile_ids):
            settings = document.uis.get(profile_id, {})
            record = by_profile.get(profile_id)
            pending_item = pending.get(profile_id, {})
            named_uis.append(
                {
                    "source_ui_id": profile_id,
                    "display_name": (
                        record.display_name
                        if record is not None
                        else str(
                            pending_item.get("display_name", f"Unrecognized UI {profile_id[:8]}")
                        )
                    ),
                    "client_type": (
                        record.client_type
                        if record is not None
                        else str(pending_item.get("client_type", "terminal"))
                    ),
                    "settings": copy.deepcopy(settings),
                }
            )
        secret_values: dict[str, str] = {}
        omitted = sorted(document.secret_references)
        if include_secrets:
            for key, reference in sorted(document.secret_references.items()):
                value = _resolve_secret_reference(str(reference))
                if len(value.encode("utf-8")) > MAX_PORTABLE_SECRET_BYTES:
                    raise GatewayConfigurationError(f"secret {key} exceeds the portable size limit")
                secret_values[key] = value
            omitted = []
        gateway = copy.deepcopy(document.gateway)
        plugin_sources = gateway.pop("plugins.sources", [])
        archive: dict[str, object] = {
            "format": PORTABLE_CONFIGURATION_FORMAT,
            "format_version": PORTABLE_CONFIGURATION_VERSION,
            "exported_at": _now(),
            "source_revision": document.revision,
            "configuration": {
                "shared": copy.deepcopy(document.shared),
                "gateway": gateway,
                "ui_types": copy.deepcopy(document.ui_types),
                "named_uis": named_uis,
                "worlds": copy.deepcopy(document.worlds),
                "plugins": copy.deepcopy(document.plugins),
                "plugin_sources": plugin_sources,
            },
            "secrets": {
                "included": include_secrets,
                "values": secret_values,
                "omitted_keys": omitted,
            },
        }
        _encoded_archive(archive)
        return archive

    def preview_import(self, archive: object) -> ImportProposal:
        normalized, digest = _validate_archive(archive)
        configuration = normalized["configuration"]
        secret_section = normalized["secrets"]
        if not isinstance(configuration, dict) or not isinstance(secret_section, dict):
            raise GatewayConfigurationError("portable configuration sections are invalid")
        current = self.configuration.document
        proposed = GatewayConfigurationDocument(
            schema_version=1,
            revision=current.revision,
            shared=copy.deepcopy(configuration["shared"]),
            gateway=copy.deepcopy(configuration["gateway"]),
            ui_types=copy.deepcopy(configuration["ui_types"]),
            worlds=copy.deepcopy(configuration["worlds"]),
            plugins=copy.deepcopy(configuration["plugins"]),
        )
        plugin_sources = copy.deepcopy(configuration["plugin_sources"])
        if plugin_sources:
            proposed.gateway["plugins.sources"] = plugin_sources

        records = self.ui_store.records()
        by_id = {str(record.ui_id): record for record in records}
        by_name: dict[str, list[UiRecord]] = {}
        for record in records:
            by_name.setdefault(record.display_name.casefold(), []).append(record)
        pending = []
        mappings = []
        links: list[tuple[UUID, UUID]] = []
        matched_destinations: set[UUID] = set()
        for item in configuration["named_uis"]:
            if not isinstance(item, dict):
                raise GatewayConfigurationError("portable named UI is invalid")
            source_id = str(item["source_ui_id"])
            display_name = str(item["display_name"])
            client_type = str(item["client_type"])
            record = by_id.get(source_id)
            match = "stable-id"
            if record is None:
                candidates = [
                    candidate
                    for candidate in by_name.get(display_name.casefold(), [])
                    if candidate.client_type == client_type
                ]
                record = candidates[0] if len(candidates) == 1 else None
                match = "display-name" if record is not None else "pending"
            if record is not None and record.ui_id in matched_destinations:
                raise GatewayConfigurationError(
                    f"more than one imported UI profile matches {record.display_name}"
                )
            if record is not None:
                matched_destinations.add(record.ui_id)
            profile_id = source_id
            if record is not None:
                existing_profile = self.ui_store.configuration_profile_id(record.ui_id)
                profile_id = str(
                    existing_profile if existing_profile != record.ui_id else UUID(source_id)
                )
                if UUID(profile_id) != record.ui_id:
                    links.append((record.ui_id, UUID(profile_id)))
            else:
                pending.append(
                    {
                        "profile_id": source_id,
                        "display_name": display_name,
                        "client_type": client_type,
                    }
                )
            proposed.uis[profile_id] = copy.deepcopy(item["settings"])
            mappings.append(
                {
                    "display_name": display_name,
                    "source_ui_id": source_id,
                    "destination_ui_id": str(record.ui_id) if record is not None else None,
                    "configuration_profile_id": profile_id,
                    "match": match,
                    "settings": len(item["settings"]),
                }
            )

        omitted = set(secret_section["omitted_keys"])
        included_values = copy.deepcopy(secret_section["values"])
        for key in omitted:
            if key in current.secret_references:
                proposed.secret_references[key] = current.secret_references[key]
        # Placeholders allow complete registry/effective validation without ever
        # putting plaintext into the proposed managed document.
        for key in included_values:
            proposed.secret_references[key] = (
                f"file:{self.secret_directory / _secret_name(key, digest, current.revision)}"
            )

        self.configuration._validate_document(proposed, defer_plugin_validation=True)
        # Validate the complete replacement against bootstrap/default state. Included
        # secret files do not exist yet, so validate their model values in memory.
        for key, value in included_values.items():
            _validate_secret_text(key, value)
        apply_gateway_configuration(
            self.base_bundle,
            proposed,
            secret_values=included_values,
        )

        changes = _document_changes(current, proposed, self.configuration)
        restart = _required_import_action(changes, self.configuration)
        missing = sorted(key for key in omitted if key not in current.secret_references)
        preview = {
            "base_revision": current.revision,
            "digest": digest,
            "changes": changes,
            "change_count": len(changes),
            "ui_mappings": mappings,
            "pending_profiles": pending,
            "included_secret_keys": sorted(included_values),
            "preserved_secret_keys": sorted(omitted - set(missing)),
            "missing_secret_keys": missing,
            "restart": restart,
            "requires_confirmation": True,
        }
        return ImportProposal(
            proposed,
            digest,
            preview,
            tuple(pending),
            tuple(links),
            included_values,
        )

    async def apply_import(
        self,
        archive: object,
        *,
        expected_revision: int,
        expected_digest: str,
        actor_ui_id: str,
        actor_ui_name: str,
    ) -> dict[str, object]:
        proposal = self.preview_import(archive)
        if proposal.digest != expected_digest:
            raise GatewayConfigurationError("configuration archive changed after preview")
        if expected_revision != self.configuration.revision:
            raise GatewayConfigurationError("configuration changed after import preview")
        previous_document = self.configuration.document.as_dict()
        previous_claims = self.ui_store.profile_claims()
        backup_path = self._write_backup(previous_document, expected_revision)
        created: list[str] = []
        try:
            for key, value in proposal.secret_values.items():
                path = self.secret_directory / _secret_name(key, proposal.digest, expected_revision)
                self._write_private(path, value.encode("utf-8") + b"\n")
                created.append(str(path))
            previous_state = copy.deepcopy(self._state)
            self._state = {
                "schema_version": 1,
                "applied_revision": expected_revision + 1,
                "applied_at": _now(),
                "backup": str(backup_path),
                "backup_digest": _digest(previous_document),
                "created_secret_files": created,
                "pending_profiles": list(proposal.pending_profiles),
                "previous_pending_profiles": copy.deepcopy(self._state.get("pending_profiles", [])),
                "previous_profile_claims": {
                    str(ui_id): str(profile_id) for ui_id, profile_id in previous_claims.items()
                },
            }
            self._save_state()
            for ui_id, profile_id in proposal.profile_links:
                await self.ui_store.claim_configuration_profile(ui_id, profile_id)
            mutation = self.configuration.replace_document(
                proposal.document,
                expected_revision=expected_revision,
                actor_ui_id=actor_ui_id,
                actor_ui_name=actor_ui_name,
                operation="import",
                restart=str(proposal.preview["restart"]),
            )
        except BaseException:
            for ui_id, _profile_id in proposal.profile_links:
                previous_profile = previous_claims.get(ui_id, ui_id)
                await self.ui_store.claim_configuration_profile(ui_id, previous_profile)
            self._state = locals().get("previous_state", self._state)
            self._save_state()
            for path in created:
                Path(path).unlink(missing_ok=True)
            raise
        return {
            **proposal.preview,
            "revision": mutation.revision,
            "requires_confirmation": False,
            "applied": True,
        }

    def status(self) -> dict[str, object]:
        applied = self._state.get("applied_revision")
        return {
            "revision": self.configuration.revision,
            "applied_revision": applied,
            "applied_at": self._state.get("applied_at"),
            "rollback_safe": isinstance(applied, int) and applied == self.configuration.revision,
            "pending_profiles": copy.deepcopy(self._state.get("pending_profiles", [])),
        }

    async def rollback(
        self, *, expected_revision: int, actor_ui_id: str, actor_ui_name: str
    ) -> dict[str, object]:
        applied = self._state.get("applied_revision")
        if not isinstance(applied, int) or applied != self.configuration.revision:
            raise GatewayConfigurationError(
                "configuration changed after import; rollback is no longer safe"
            )
        if expected_revision != self.configuration.revision:
            raise GatewayConfigurationError("configuration changed after rollback preview")
        backup = self._state.get("backup")
        if not isinstance(backup, str):
            raise GatewayConfigurationError("pre-import backup is unavailable")
        previous = GatewayConfigurationDocument.from_dict(
            json.loads(self._read_private(Path(backup)).decode("utf-8"))
        )
        changes = _document_changes(self.configuration.document, previous, self.configuration)
        restart = _required_import_action(changes, self.configuration)
        mutation = self.configuration.replace_document(
            previous,
            expected_revision=expected_revision,
            actor_ui_id=actor_ui_id,
            actor_ui_name=actor_ui_name,
            operation="rollback-import",
            restart=restart,
        )
        raw_claims = self._state.get("previous_profile_claims", {})
        previous_claims = raw_claims if isinstance(raw_claims, dict) else {}
        for ui_id in self.ui_store.profile_claims():
            await self.ui_store.claim_configuration_profile(ui_id, ui_id)
        for raw_ui_id, raw_profile_id in previous_claims.items():
            try:
                await self.ui_store.claim_configuration_profile(
                    UUID(str(raw_ui_id)), UUID(str(raw_profile_id))
                )
            except (ValueError, TypeError, AttributeError):
                continue
        for raw in self._state.get("created_secret_files", []):
            if isinstance(raw, str) and raw not in previous.secret_references.values():
                Path(raw).unlink(missing_ok=True)
        self._state["applied_revision"] = None
        self._state["applied_at"] = None
        self._state["pending_profiles"] = copy.deepcopy(
            self._state.get("previous_pending_profiles", [])
        )
        self._save_state()
        return {"revision": mutation.revision, "rolled_back": True, "restart": restart}

    def profiles(self) -> list[dict[str, object]]:
        document = self.configuration.document
        claims = self.ui_store.profile_claims()
        claimed = {str(profile): str(ui_id) for ui_id, profile in claims.items()}
        records = {str(record.ui_id): record for record in self.ui_store.records()}
        pending = {
            str(item.get("profile_id")): item
            for item in self._state.get("pending_profiles", [])
            if isinstance(item, dict)
        }
        output = []
        for profile_id, settings in sorted(document.uis.items()):
            direct = records.get(profile_id)
            owner_id = claimed.get(profile_id)
            owner = records.get(owner_id) if owner_id else direct
            metadata = pending.get(profile_id, {})
            output.append(
                {
                    "profile_id": profile_id,
                    "display_name": owner.display_name if owner else metadata.get("display_name"),
                    "client_type": owner.client_type if owner else metadata.get("client_type"),
                    "ui_id": str(owner.ui_id) if owner else None,
                    "claimed": owner is not None,
                    "pending": profile_id in pending and owner is None,
                    "settings": len(settings),
                    "last_seen_at": owner.as_dict()["last_seen_at"] if owner else None,
                }
            )
        return output

    def pending_candidates(self, record: UiRecord) -> list[dict[str, object]]:
        """Return unclaimed restore profiles that exactly match a newly connected UI."""

        return [
            item
            for item in self.profiles()
            if item["pending"]
            and isinstance(item["display_name"], str)
            and item["display_name"].casefold() == record.display_name.casefold()
            and item["client_type"] == record.client_type
        ]

    async def claim(self, ui_id: UUID, profile_id: UUID) -> dict[str, object]:
        profile = next(
            (item for item in self.profiles() if item["profile_id"] == str(profile_id)), None
        )
        if profile is None or not profile["pending"]:
            raise GatewayConfigurationError("imported UI configuration profile is unavailable")
        await self.ui_store.claim_configuration_profile(ui_id, profile_id)
        pending = [
            item
            for item in self._state.get("pending_profiles", [])
            if not isinstance(item, dict) or item.get("profile_id") != str(profile_id)
        ]
        self._state["pending_profiles"] = pending
        self._save_state()
        return {**profile, "ui_id": str(ui_id), "claimed": True, "pending": False}

    async def remove_profile(
        self,
        profile_id: UUID,
        *,
        expected_revision: int,
        actor_ui_id: str,
        actor_ui_name: str,
    ) -> dict[str, object]:
        profile = next(
            (item for item in self.profiles() if item["profile_id"] == str(profile_id)), None
        )
        if profile is None:
            raise GatewayConfigurationError("UI configuration profile is unavailable")
        mutation = self.configuration.remove_ui_profile(
            str(profile_id),
            expected_revision=expected_revision,
            actor_ui_id=actor_ui_id,
            actor_ui_name=actor_ui_name,
        )
        await self.ui_store.clear_configuration_profile(profile_id)
        self._state["pending_profiles"] = [
            item
            for item in self._state.get("pending_profiles", [])
            if not isinstance(item, dict) or item.get("profile_id") != str(profile_id)
        ]
        self._save_state()
        return {"revision": mutation.revision, "removed": profile, "restart": mutation.restart}

    def _write_backup(self, value: object, revision: int) -> Path:
        self.backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = (
            self.backup_directory
            / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-r{revision}.json"
        )
        self._write_private(path, _json_bytes(value))
        return path

    def _load_state(self) -> dict[str, object]:
        try:
            value = json.loads(self._read_private(self.state_path).decode("utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "pending_profiles": []}
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise GatewayConfigurationError("configuration import state is invalid")
        return value

    def _save_state(self) -> None:
        self._write_private(self.state_path, _json_bytes(self._state))

    def _write_private(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_private(path: Path) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            details = os.fstat(descriptor)
            if os.name == "posix" and (details.st_uid != os.geteuid() or details.st_mode & 0o077):
                raise GatewayConfigurationError("configuration import file must be private")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                data = stream.read(MAX_PORTABLE_CONFIGURATION_BYTES + 1)
            if len(data) > MAX_PORTABLE_CONFIGURATION_BYTES:
                raise GatewayConfigurationError("configuration import file is too large")
            return data
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _validate_archive(value: object) -> tuple[dict[str, object], str]:
    data = _encoded_archive(value)
    if not isinstance(value, dict) or set(value) != {
        "format",
        "format_version",
        "exported_at",
        "source_revision",
        "configuration",
        "secrets",
    }:
        raise GatewayConfigurationError("portable configuration has invalid fields")
    if value["format"] != PORTABLE_CONFIGURATION_FORMAT or value["format_version"] != 1:
        raise GatewayConfigurationError("portable configuration format is unsupported")
    if not isinstance(value["source_revision"], int) or isinstance(value["source_revision"], bool):
        raise GatewayConfigurationError("portable configuration revision is invalid")
    configuration = value["configuration"]
    expected = {"shared", "gateway", "ui_types", "named_uis", "worlds", "plugins", "plugin_sources"}
    if not isinstance(configuration, dict) or set(configuration) != expected:
        raise GatewayConfigurationError("portable configuration sections are invalid")
    for key in ("shared", "gateway", "ui_types", "worlds", "plugins"):
        if not isinstance(configuration[key], dict):
            raise GatewayConfigurationError(f"portable configuration {key} must be an object")
    if not isinstance(configuration["plugin_sources"], list) or not isinstance(
        configuration["named_uis"], list
    ):
        raise GatewayConfigurationError("portable configuration lists are invalid")
    seen: set[str] = set()
    for item in configuration["named_uis"]:
        if not isinstance(item, dict) or set(item) != {
            "source_ui_id",
            "display_name",
            "client_type",
            "settings",
        }:
            raise GatewayConfigurationError("portable named UI is invalid")
        try:
            ui_id = str(UUID(str(item["source_ui_id"])))
        except (TypeError, ValueError, AttributeError) as exc:
            raise GatewayConfigurationError("portable named UI ID is invalid") from exc
        if ui_id in seen:
            raise GatewayConfigurationError("portable configuration contains duplicate UI IDs")
        seen.add(ui_id)
        if (
            not isinstance(item["display_name"], str)
            or not item["display_name"].strip()
            or item["client_type"] not in {"terminal", "pwa"}
            or not isinstance(item["settings"], dict)
        ):
            raise GatewayConfigurationError("portable named UI metadata is invalid")
    secret_section = value["secrets"]
    if not isinstance(secret_section, dict) or set(secret_section) != {
        "included",
        "values",
        "omitted_keys",
    }:
        raise GatewayConfigurationError("portable secret section is invalid")
    values = secret_section["values"]
    omitted = secret_section["omitted_keys"]
    if (
        not isinstance(secret_section["included"], bool)
        or not isinstance(values, dict)
        or not all(
            isinstance(key, str) and isinstance(secret, str) for key, secret in values.items()
        )
        or not isinstance(omitted, list)
        or not all(isinstance(key, str) for key in omitted)
        or set(values) & set(omitted)
    ):
        raise GatewayConfigurationError("portable secret values are invalid")
    return copy.deepcopy(value), hashlib.sha256(data).hexdigest()


def _document_changes(
    current: GatewayConfigurationDocument,
    proposed: GatewayConfigurationDocument,
    store: GatewayConfigurationStore,
) -> list[dict[str, object]]:
    before = _flatten(current)
    after = _flatten(proposed)
    output = []
    for identity in sorted(set(before) | set(after)):
        old_present = identity in before
        new_present = identity in after
        old = before.get(identity)
        new = after.get(identity)
        if old_present == new_present and old == new:
            continue
        scope, target, key = identity
        descriptor = store.registry.get(key)
        sensitive = scope == "secret" or descriptor is None or descriptor.sensitivity
        output.append(
            {
                "scope": scope,
                "target": target,
                "key": key,
                "operation": "add"
                if not old_present
                else "remove"
                if not new_present
                else "change",
                "old": None if sensitive else copy.deepcopy(old),
                "new": None if sensitive else copy.deepcopy(new),
                "sensitive": sensitive,
                "restart": descriptor.restart.value
                if descriptor is not None
                else "gateway-restart",
            }
        )
    return output


def _flatten(document: GatewayConfigurationDocument) -> dict[tuple[str, str | None, str], object]:
    output: dict[tuple[str, str | None, str], object] = {}
    sections = [
        ("shared", None, document.shared),
        ("gateway", None, document.gateway),
        ("secret", None, document.secret_references),
    ]
    for scope, mapping in (
        ("ui-type", document.ui_types),
        ("ui", document.uis),
        ("world", document.worlds),
        ("plugin", document.plugins),
    ):
        sections.extend((scope, target, section) for target, section in mapping.items())
    for scope, target, section in sections:
        for key, value in section.items():
            output[(scope, target, key)] = value
    return output


def _validate_secret_text(key: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_PORTABLE_SECRET_BYTES
    ):
        raise GatewayConfigurationError(f"portable secret {key} is invalid")


def _secret_name(key: str, archive_digest: str, base_revision: int) -> str:
    identity = f"{base_revision}:{archive_digest}:{key}".encode()
    return hashlib.sha256(identity).hexdigest() + ".secret"


def _maximum_restart(values) -> str:
    return max(values, key=lambda value: _RESTART_RANK.get(str(value), 4), default="live")


def _required_import_action(
    changes: list[dict[str, object]], store: GatewayConfigurationStore
) -> str:
    restart = _maximum_restart(item["restart"] for item in changes)
    if any(
        item["scope"] not in {"ui", "ui-type"}
        and (descriptor := store.registry.get(str(item["key"]))) is not None
        and "gateway" in descriptor.consumers
        for item in changes
    ):
        # Whole-document import intentionally does not replay a long sequence
        # of partial live handlers. Rebuild Gateway runtime state once instead.
        return "gateway-restart"
    return restart


def _encoded_archive(value: object) -> bytes:
    data = _json_bytes(value)
    if len(data) > MAX_PORTABLE_CONFIGURATION_BYTES:
        raise GatewayConfigurationError("portable configuration exceeds 4 MiB")
    return data


def _json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    except (TypeError, ValueError) as exc:
        raise GatewayConfigurationError("portable configuration is not JSON-compatible") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
