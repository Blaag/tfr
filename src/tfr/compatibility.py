from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

COMPATIBILITY_REPORT_VERSION = 1
COMPATIBILITY_FLOOR = "0.1.122"
COMPATIBILITY_REPORT_MAX_AGE = timedelta(days=30)

CURRENT_SCHEMAS = {
    "gateway_configuration": 1,
    "ui_authority": 3,
    "pwa_devices": 2,
    "portable_export": 2,
    "ui_profiles": 1,
    "ui_identity": 1,
}
CURRENT_GATEWAY_PROTOCOL = 2
CURRENT_WEB_PROTOCOL = 1


def utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")


def validate_ui_compatibility_report(value: object) -> dict[str, object]:
    expected = {
        "report_version",
        "checked_at",
        "tfr_version",
        "protocol",
        "profile_schema",
        "identity_schema",
        "deployment_schema",
        "legacy_configuration_detected",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("UI compatibility report has invalid fields")
    if value["report_version"] != COMPATIBILITY_REPORT_VERSION:
        raise ValueError("UI compatibility report version is unsupported")
    if (
        not isinstance(value["checked_at"], str)
        or not isinstance(value["tfr_version"], str)
        or not 1 <= len(value["tfr_version"]) <= 80
        or not isinstance(value["protocol"], int)
        or isinstance(value["protocol"], bool)
        or not isinstance(value["profile_schema"], int)
        or isinstance(value["profile_schema"], bool)
        or not isinstance(value["identity_schema"], int)
        or isinstance(value["identity_schema"], bool)
        or (
            value["deployment_schema"] is not None
            and (
                not isinstance(value["deployment_schema"], int)
                or isinstance(value["deployment_schema"], bool)
            )
        )
        or not isinstance(value["legacy_configuration_detected"], bool)
    ):
        raise ValueError("UI compatibility report is invalid")
    try:
        checked = datetime.fromisoformat(value["checked_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("UI compatibility report timestamp is invalid") from exc
    if checked.tzinfo is None or checked.utcoffset() is None:
        raise ValueError("UI compatibility report timestamp must include a timezone")
    return dict(value)


def ui_compatibility_report(
    *,
    tfr_version: str,
    protocol: int,
    deployment_schema: int | None,
    legacy_configuration_detected: bool,
) -> dict[str, object]:
    return validate_ui_compatibility_report(
        {
            "report_version": COMPATIBILITY_REPORT_VERSION,
            "checked_at": utc_text(),
            "tfr_version": tfr_version,
            "protocol": protocol,
            "profile_schema": CURRENT_SCHEMAS["ui_profiles"],
            "identity_schema": CURRENT_SCHEMAS["ui_identity"],
            "deployment_schema": deployment_schema,
            "legacy_configuration_detected": legacy_configuration_detected,
        }
    )


def compatibility_snapshot(
    *,
    migration_pending: bool,
    legacy_bootstrap: bool,
    legacy_plugin_sources: int,
    legacy_speaker_rules: int,
    ui_records: list[dict[str, object]],
    ui_reports: dict[str, dict[str, object]],
    pwa_device_count: int,
    service: dict[str, object],
    web: dict[str, object],
    now: datetime | None = None,
) -> dict[str, object]:
    checked = now or datetime.now(UTC)
    blockers: list[str] = []
    warnings: list[str] = []
    clients: list[dict[str, object]] = []
    for record in ui_records:
        ui_id = str(record.get("ui_id", ""))
        client_type = str(record.get("client_type", "unknown"))
        report = ui_reports.get(ui_id)
        versions = dict(record.get("versions", {}))
        status = (
            "current"
            if client_type == "pwa" and versions.get("web_protocol") == str(CURRENT_WEB_PROTOCOL)
            else "incompatible"
            if client_type == "pwa"
            else "missing"
        )
        report_checked = None
        if client_type == "terminal" and report is not None:
            validated = validate_ui_compatibility_report(report)
            report_checked = str(validated["checked_at"])
            timestamp = datetime.fromisoformat(report_checked.replace("Z", "+00:00")).astimezone(
                UTC
            )
            incompatible = (
                not _version_at_least_floor(str(validated["tfr_version"]))
                or validated["protocol"] != CURRENT_GATEWAY_PROTOCOL
                or validated["profile_schema"] != CURRENT_SCHEMAS["ui_profiles"]
                or validated["identity_schema"] != CURRENT_SCHEMAS["ui_identity"]
                or validated["deployment_schema"] not in {1, 2}
                or validated["legacy_configuration_detected"] is True
            )
            status = (
                "incompatible"
                if incompatible
                else "stale"
                if checked - timestamp > COMPATIBILITY_REPORT_MAX_AGE
                else "current"
            )
        if status in {"missing", "stale", "incompatible"}:
            blockers.append(f"client:{ui_id}:{status}")
        clients.append(
            {
                "ui_id": ui_id,
                "name": str(record.get("display_name", "Unknown UI"))[:80],
                "type": client_type,
                "version": str(
                    versions.get("tfr", f"web protocol {versions.get('web_protocol', 'unknown')}")
                )[:80],
                "report_status": status,
                "checked_at": report_checked,
            }
        )
    if migration_pending:
        blockers.append("gateway:migration-pending")
    if legacy_bootstrap:
        blockers.append("gateway:legacy-bootstrap")
    if legacy_plugin_sources:
        blockers.append("gateway:legacy-plugin-sources")
    if legacy_speaker_rules:
        blockers.append("gateway:legacy-speaker-rules")
    for key in ("active", "enabled", "linger"):
        if service.get(key) is not True:
            warnings.append(f"service:{key}-not-confirmed")
    if web.get("enabled") and web.get("active") is not True:
        blockers.append("gateway:web-backend-inactive")
    return {
        "report_version": COMPATIBILITY_REPORT_VERSION,
        "checked_at": utc_text(checked),
        "compatibility_floor": COMPATIBILITY_FLOOR,
        "ready_for_cleanup": not blockers,
        "gateway": {
            **CURRENT_SCHEMAS,
            "bootstrap": "legacy" if legacy_bootstrap else "canonical",
            "migration_pending": migration_pending,
            "legacy_plugin_sources": legacy_plugin_sources,
            "legacy_speaker_rules": legacy_speaker_rules,
            "pwa_device_count": pwa_device_count,
        },
        "host": {
            "user_service_active": service.get("active"),
            "user_service_enabled": service.get("enabled"),
            "linger_enabled": service.get("linger"),
            "web_backend_active": web.get("active"),
            "web_transport": web.get("transport"),
        },
        "clients": clients,
        "blockers": blockers,
        "warnings": warnings,
    }


def _version_at_least_floor(value: str) -> bool:
    try:
        version = tuple(int(part) for part in value.split(".")[:3])
        floor = tuple(int(part) for part in COMPATIBILITY_FLOOR.split("."))
    except ValueError:
        return False
    return len(version) == 3 and version >= floor


def legacy_speaker_rule_count(value: object) -> int:
    if isinstance(value, dict):
        count = 0
        kinds = value.get("kinds")
        if isinstance(kinds, list) and "page" in kinds:
            count += 1
        return count + sum(legacy_speaker_rule_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(legacy_speaker_rule_count(item) for item in value)
    return 0


def compatibility_text(report: dict[str, Any]) -> str:
    gateway = report.get("gateway", {})
    host = report.get("host", {})
    lines = [
        f"Compatibility floor: {report.get('compatibility_floor')}",
        "Ready to retire pre-floor compatibility: "
        + ("yes" if report.get("ready_for_cleanup") else "no"),
        "",
        "Gateway",
        f"  Bootstrap: {gateway.get('bootstrap')}",
        f"  UI authority schema: {gateway.get('ui_authority')}",
        f"  PWA device schema: {gateway.get('pwa_devices')}",
        f"  Portable export format: {gateway.get('portable_export')}",
        f"  Migration pending: {'yes' if gateway.get('migration_pending') else 'no'}",
        "",
        "Host",
        f"  User service active: {host.get('user_service_active')}",
        f"  User service enabled: {host.get('user_service_enabled')}",
        f"  Linger enabled: {host.get('linger_enabled')}",
        f"  Web backend active: {host.get('web_backend_active')}",
        "",
        "Clients",
    ]
    for client in report.get("clients", []):
        lines.append(
            f"  {client.get('name')} · {client.get('type')} · {client.get('version')}"
            f" · {client.get('report_status')}"
        )
    blockers = report.get("blockers", [])
    if blockers:
        lines.extend(("", "Blockers", *(f"  {item}" for item in blockers)))
    return "\n".join(lines)
