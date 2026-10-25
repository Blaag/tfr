from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tfr.gateway_uis import UiRecord, UiRegistration, UiStore


async def test_register_persists_and_reuses_stable_ui_record(tmp_path) -> None:
    ui_id = UUID("11111111-1111-4111-8111-111111111111")
    times = iter(
        [
            datetime(2026, 10, 24, 10, tzinfo=UTC),
            datetime(2026, 10, 24, 11, tzinfo=UTC),
            datetime(2026, 10, 24, 12, tzinfo=UTC),
            datetime(2026, 10, 24, 13, tzinfo=UTC),
            datetime(2026, 10, 24, 14, tzinfo=UTC),
            datetime(2026, 10, 24, 15, tzinfo=UTC),
        ]
    )
    store = UiStore(tmp_path, clock=lambda: next(times))

    original = await store.register(
        ui_id=ui_id,
        client_type="terminal",
        platform="office-mac",
        versions={"tfr": "0.1.67"},
        capabilities=["color", "timeline"],
    )
    reconnected = await store.register(
        ui_id=ui_id,
        client_type="terminal",
        platform="new-hostname",
        versions={"tfr": "0.1.68"},
        capabilities=["color"],
        applied_config_revision=4,
    )

    assert original.display_name == "office-mac · Terminal"
    assert reconnected.ui_id == original.ui_id
    assert reconnected.display_name == original.display_name
    assert reconnected.created_at == original.created_at
    assert reconnected.platform == "new-hostname"
    assert reconnected.applied_config_revision == 4
    assert UiStore(tmp_path).get(ui_id) == reconnected
    assert store.find_by_name("OFFICE-MAC · TERMINAL") == reconnected
    assert store.find_by_name("missing · Terminal") is None


async def test_duplicate_default_names_receive_stable_short_suffix(tmp_path) -> None:
    store = UiStore(tmp_path)
    first = await store.register(
        ui_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        client_type="pwa",
        platform="John’s iPhone",
        versions={},
        capabilities=[],
    )
    second = await store.register(
        ui_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        client_type="pwa",
        platform="John’s iPhone",
        versions={},
        capabilities=[],
    )

    assert first.display_name == "John’s iPhone · PWA"
    assert second.display_name == "John’s iPhone · PWA · bbbbbb"
    assert len(second.display_name) <= 80

    renamed = await store.rename(first.ui_id, "John's primary phone")
    assert renamed.display_name == "John's primary phone"
    assert UiStore(tmp_path).find_by_name("JOHN'S PRIMARY PHONE") == renamed
    with pytest.raises(ValueError, match="already in use"):
        await store.rename(first.ui_id, second.display_name.upper())
    admin_renamed = await store.rename_named(second.display_name.upper(), "Travel phone")
    assert admin_renamed.ui_id == second.ui_id
    assert admin_renamed.display_name == "Travel phone"
    with pytest.raises(ValueError, match="unknown UI"):
        await store.rename_named("Missing phone", "Unused name")
    forgotten = await store.forget_named("TRAVEL PHONE")
    assert forgotten.ui_id == second.ui_id
    assert store.get(second.ui_id) is None
    assert UiStore(tmp_path).get(second.ui_id) is None
    with pytest.raises(ValueError, match="unknown UI"):
        await store.forget_named("Travel phone")


async def test_duplicate_suffix_expands_if_short_ui_ids_collide(tmp_path) -> None:
    store = UiStore(tmp_path)
    records = []
    for ui_id in (
        "aaaaaa11-1111-4111-8111-111111111111",
        "aaaaaa22-2222-4222-8222-222222222222",
        "aaaaaa33-3333-4333-8333-333333333333",
    ):
        records.append(
            await store.register(
                ui_id=UUID(ui_id),
                client_type="terminal",
                platform="office",
                versions={},
                capabilities=[],
            )
        )

    assert [record.display_name for record in records] == [
        "office · Terminal",
        "office · Terminal · aaaaaa",
        "office · Terminal · aaaaaa33",
    ]


async def test_stale_ui_records_remain_visible_and_reconnect_in_place(tmp_path) -> None:
    stale_id = UUID("11111111-1111-4111-8111-111111111111")
    current_id = UUID("22222222-2222-4222-8222-222222222222")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    times = iter((start, start + timedelta(days=90), start + timedelta(days=91)))
    store = UiStore(tmp_path, clock=lambda: next(times))
    stale = await store.register(
        ui_id=stale_id,
        client_type="terminal",
        platform="old-laptop",
        versions={"tfr": "0.1.60"},
        capabilities=[],
    )
    await store.register(
        ui_id=current_id,
        client_type="terminal",
        platform="new-laptop",
        versions={"tfr": "0.1.67"},
        capabilities=[],
    )

    assert {record.ui_id for record in UiStore(tmp_path).records()} == {stale_id, current_id}

    reconnected = await store.register(
        ui_id=stale_id,
        client_type="terminal",
        platform="old-laptop-renamed",
        versions={"tfr": "0.1.68"},
        capabilities=["timeline"],
    )

    assert reconnected.ui_id == stale_id
    assert reconnected.display_name == stale.display_name
    assert reconnected.created_at == stale.created_at
    assert reconnected.last_seen_at == start + timedelta(days=91)
    assert len(store.records()) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_type", "browser"),
        ("platform", " bad "),
        ("capabilities", ["same", "same"]),
        ("applied_config_revision", True),
    ],
)
def test_ui_record_rejects_malformed_fields(field, value) -> None:
    record = {
        "ui_id": "11111111-1111-4111-8111-111111111111",
        "display_name": "office-mac · Terminal",
        "client_type": "terminal",
        "platform": "office-mac",
        "versions": {"tfr": "0.1.67"},
        "capabilities": ["color"],
        "created_at": "2026-10-24T10:00:00Z",
        "last_seen_at": "2026-10-24T11:00:00Z",
        "applied_config_revision": None,
    }
    record[field] = value

    with pytest.raises(ValueError):
        UiRecord.from_dict(record)


def test_ui_registration_rejects_unknown_and_unbounded_metadata() -> None:
    registration = {
        "client_type": "terminal",
        "platform": "office-mac",
        "versions": {},
        "capabilities": [],
        "applied_config_revision": None,
    }

    with pytest.raises(ValueError, match="invalid fields"):
        UiRegistration.from_dict({**registration, "unexpected": True})
    with pytest.raises(ValueError, match="capabilities"):
        UiRegistration.from_dict({**registration, "capabilities": ["x"] * 65})


def test_store_rejects_duplicate_names_case_insensitively(tmp_path) -> None:
    created = datetime(2026, 10, 24, tzinfo=UTC)
    records = []
    for ui_id, display_name in (
        ("11111111-1111-4111-8111-111111111111", "Office · Terminal"),
        ("22222222-2222-4222-8222-222222222222", "office · terminal"),
    ):
        records.append(
            {
                "ui_id": ui_id,
                "display_name": display_name,
                "client_type": "terminal",
                "platform": "office",
                "versions": {},
                "capabilities": [],
                "created_at": created.isoformat(),
                "last_seen_at": (created + timedelta(minutes=1)).isoformat(),
                "applied_config_revision": None,
            }
        )
    state_path = tmp_path / "uis.json"
    state_path.write_text(json.dumps({"schema_version": 1, "uis": records}), encoding="utf-8")
    state_path.chmod(0o600)

    with pytest.raises(ValueError, match="duplicate display names"):
        UiStore(tmp_path)
