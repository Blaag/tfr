from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tfr.compatibility import compatibility_snapshot, compatibility_text


def test_central_compatibility_snapshot_requires_current_native_reports() -> None:
    now = datetime(2026, 11, 17, tzinfo=UTC)
    records = [
        {
            "ui_id": "11111111-1111-4111-8111-111111111111",
            "display_name": "Current Mac",
            "client_type": "terminal",
            "versions": {"tfr": "0.1.125"},
        },
        {
            "ui_id": "22222222-2222-4222-8222-222222222222",
            "display_name": "Stale Mac",
            "client_type": "terminal",
            "versions": {"tfr": "0.1.124"},
        },
        {
            "ui_id": "33333333-3333-4333-8333-333333333333",
            "display_name": "Phone",
            "client_type": "pwa",
            "versions": {"web_protocol": "1"},
        },
    ]

    def report(checked: datetime) -> dict[str, object]:
        return {
            "report_version": 1,
            "checked_at": checked.isoformat().replace("+00:00", "Z"),
            "tfr_version": "0.1.125",
            "protocol": 2,
            "profile_schema": 1,
            "identity_schema": 1,
            "deployment_schema": 2,
            "legacy_configuration_detected": False,
        }

    snapshot = compatibility_snapshot(
        migration_pending=False,
        legacy_bootstrap=False,
        legacy_plugin_sources=0,
        legacy_speaker_rules=0,
        ui_records=records,
        ui_reports={
            records[0]["ui_id"]: report(now),
            records[1]["ui_id"]: report(now - timedelta(days=31)),
        },
        pwa_device_count=1,
        service={"active": True, "enabled": True, "linger": True},
        web={"enabled": True, "active": True, "transport": "tailscale"},
        now=now,
    )

    assert snapshot["ready_for_cleanup"] is False
    assert [item["report_status"] for item in snapshot["clients"]] == [
        "current",
        "stale",
        "current",
    ]
    assert snapshot["blockers"] == ["client:22222222-2222-4222-8222-222222222222:stale"]
    text = compatibility_text(snapshot)
    assert "Compatibility floor: 0.1.122" in text
    assert "Current Mac · terminal · 0.1.125 · current" in text


def test_compatibility_snapshot_contains_no_sensitive_operational_values() -> None:
    snapshot = compatibility_snapshot(
        migration_pending=False,
        legacy_bootstrap=False,
        legacy_plugin_sources=0,
        legacy_speaker_rules=0,
        ui_records=[],
        ui_reports={},
        pwa_device_count=0,
        service={"active": True, "enabled": True, "linger": True},
        web={"enabled": False, "active": False, "transport": "tailscale"},
    )

    serialized = str(snapshot).casefold()
    for forbidden in ("token", "password", "endpoint", "worlds", "/home/", "tailscale_login"):
        assert forbidden not in serialized
