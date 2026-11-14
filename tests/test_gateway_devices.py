from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import tfr.gateway_devices as gateway_devices
from tfr.gateway_devices import DeviceStore, PushPreferences

P256DH = base64.urlsafe_b64encode(b"\x04" + b"p" * 64).rstrip(b"=").decode()
PUSH_AUTH = base64.urlsafe_b64encode(b"a" * 16).rstrip(b"=").decode()


def pairing_code(url: str) -> str:
    return parse_qs(urlsplit(url).fragment)["pair"][0]


async def test_pairing_creates_persistent_private_device_session(tmp_path: Path) -> None:
    state_directory = tmp_path / "web"
    store = DeviceStore(state_directory, "https://gateway.example.ts.net")

    device, token = await store.redeem(
        pairing_code(store.create_pairing_url("Black's iPhone", ("alpha",))),
        "black@example.com",
    )

    assert store.authenticate(token) == device
    assert store.authenticate("wrong") is None
    assert device.allowed_worlds == ("alpha",)
    assert device.ui_id != device.device_id
    assert device.expires_at > device.created_at
    assert device.tailscale_login == "black@example.com"
    assert "#pair=" in store.create_pairing_url("Second phone", ("alpha",))
    assert state_directory.joinpath("devices.json").read_text(encoding="utf-8").find(token) == -1
    if os.name == "posix":
        assert stat.S_IMODE(state_directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((state_directory / "devices.json").stat().st_mode) == 0o600

    reloaded = DeviceStore(state_directory, "https://gateway.example.ts.net")
    assert reloaded.authenticate(token) == device

    reloaded._devices[device.token_digest] = replace(
        device,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert reloaded.authenticate(token) is None


async def test_pairing_code_retry_is_atomic_and_idempotent(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "web", "https://gateway.example.ts.net")
    code = pairing_code(store.create_pairing_url("Phone", ("alpha",)))

    results = await asyncio.gather(
        store.redeem(code, "black@example.com"),
        store.redeem(code, "black@example.com"),
        return_exceptions=True,
    )

    assert all(isinstance(result, tuple) for result in results)
    assert results[0] == results[1]
    assert len(store.device_descriptors()) == 1

    assert await store.redeem(code, "black@example.com") == results[0]
    with pytest.raises(ValueError, match="invalid or expired"):
        await store.redeem(code, "other@example.com")


async def test_device_activity_slides_expiration_with_a_daily_write_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DeviceStore(tmp_path / "web", "https://gateway.example.ts.net")
    device, token = await store.redeem(
        pairing_code(store.create_pairing_url("Phone", ("alpha",))),
        "black@example.com",
    )
    old = device.last_used_at - timedelta(days=2)
    old_expiration = device.expires_at - timedelta(days=2)
    store._devices[device.token_digest] = replace(
        device,
        last_used_at=old,
        expires_at=old_expiration,
    )
    original_save = store._save
    saves = 0

    def save() -> None:
        nonlocal saves
        saves += 1
        original_save()

    monkeypatch.setattr(store, "_save", save)

    updated, changed = await store.mark_used(device.device_id)
    repeated, changed_again = await store.mark_used(device.device_id)

    assert changed is True
    assert changed_again is False
    assert updated is not None and updated.last_used_at > old
    assert updated.expires_at > old_expiration
    assert updated.expires_at - updated.last_used_at == timedelta(days=180)
    assert repeated == updated
    assert saves == 1
    assert DeviceStore(store.state_directory, store.origin).authenticate(token) == updated


async def test_push_subscription_is_private_persistent_and_latched(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "web", "https://gateway.example.ts.net")
    device, token = await store.redeem(
        pairing_code(store.create_pairing_url("Phone", ("alpha", "beta"))),
        "black@example.com",
    )
    preferences = PushPreferences(worlds=("beta",), mentions=True, pages=False, preview="details")

    subscribed = await store.set_push_subscription(
        device.device_id,
        endpoint="https://web.push.apple.com/QWERTY",
        p256dh=P256DH,
        auth=PUSH_AUTH,
        preferences=preferences,
    )
    assert subscribed.push is not None and subscribed.push.preferences == preferences
    assert store.device_descriptors()[0]["push_enabled"] is True
    assert await store.set_push_latched(device.device_id, True)

    reloaded = DeviceStore(store.state_directory, store.origin)
    persisted = reloaded.authenticate(token)
    assert persisted is not None and persisted.push is not None
    assert persisted.push.latched is True
    assert persisted.push.preferences.worlds == ("beta",)
    assert persisted.push.preferences.preview == "details"

    removed = await reloaded.remove_push_subscription(device.device_id)
    assert removed is not None and removed.push is None


def test_push_preferences_migrate_private_preview_and_reject_unknown_values() -> None:
    legacy = {
        "any_activity": False,
        "worlds": ["alpha"],
        "mentions": True,
        "pages": True,
    }
    assert PushPreferences.from_dict(legacy, ("alpha",)).preview == "private"
    assert PushPreferences.from_dict({**legacy, "preview": "full"}, ("alpha",)).preview == "details"
    with pytest.raises(ValueError, match="preview"):
        PushPreferences.from_dict({**legacy, "preview": "message"}, ("alpha",))


async def test_push_subscription_rejects_untrusted_endpoint_and_world(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "web", "https://gateway.example.ts.net")
    device, _token = await store.redeem(
        pairing_code(store.create_pairing_url("Phone", ("alpha",))),
        "black@example.com",
    )

    with pytest.raises(ValueError, match="provider is unsupported"):
        await store.set_push_subscription(
            device.device_id,
            endpoint="https://example.com/internal",
            p256dh=P256DH,
            auth=PUSH_AUTH,
            preferences=PushPreferences(),
        )
    with pytest.raises(ValueError, match="not authorized"):
        await store.set_push_subscription(
            device.device_id,
            endpoint="https://web.push.apple.com/QWERTY",
            p256dh=P256DH,
            auth=PUSH_AUTH,
            preferences=PushPreferences(worlds=("beta",)),
        )


async def test_device_state_without_last_use_migrates_from_creation_time(tmp_path: Path) -> None:
    state_directory = tmp_path / "web"
    store = DeviceStore(state_directory, "https://gateway.example.ts.net")
    await store.redeem(
        pairing_code(store.create_pairing_url("Phone", ("alpha",))),
        "black@example.com",
    )
    value = json.loads(store.path.read_text(encoding="utf-8"))
    value["schema_version"] = 1
    del value["devices"][0]["last_used_at"]
    del value["devices"][0]["push"]
    store.path.write_text(json.dumps(value), encoding="utf-8")
    store.path.chmod(0o600)

    migrated = DeviceStore(state_directory, store.origin).device_descriptors()[0]

    assert migrated["last_used_at"] == migrated["created_at"]


async def test_device_state_without_ui_id_migrates_to_stable_credential_id(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "web"
    store = DeviceStore(state_directory, "https://gateway.example.ts.net")
    device, _token = await store.redeem(
        pairing_code(store.create_pairing_url("Phone", ("alpha",))),
        "black@example.com",
    )
    value = json.loads(store.path.read_text(encoding="utf-8"))
    del value["devices"][0]["ui_id"]
    store.path.write_text(json.dumps(value), encoding="utf-8")
    store.path.chmod(0o600)

    migrated = DeviceStore(state_directory, store.origin).device_descriptors()[0]

    assert migrated["ui_id"] == str(device.device_id)


async def test_pairing_retry_secret_expires_and_is_cleared_on_revoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_devices, "PAIRING_TTL_SECONDS", 0.01)
    store = DeviceStore(tmp_path / "web", "https://gateway.example.ts.net")
    code = pairing_code(store.create_pairing_url("Phone", ("alpha",)))
    device, _token = await store.redeem(code, "black@example.com")

    await asyncio.sleep(0.03)
    assert not store._redeemed_pairings
    with pytest.raises(ValueError, match="invalid or expired"):
        await store.redeem(code, "black@example.com")

    monkeypatch.setattr(gateway_devices, "PAIRING_TTL_SECONDS", 600)
    second_code = pairing_code(store.create_pairing_url("Phone", ("alpha",)))
    second_device, _token = await store.redeem(second_code, "black@example.com")
    assert await store.revoke(second_device.device_id) is True
    assert not store._redeemed_pairings
    with pytest.raises(ValueError, match="invalid or expired"):
        await store.redeem(second_code, "black@example.com")

    assert await store.revoke(device.device_id) is True


def test_device_state_rejects_symlinks(tmp_path: Path) -> None:
    state_directory = tmp_path / "web"
    state_directory.mkdir(mode=0o700)
    target = tmp_path / "devices.json"
    target.write_text('{"schema_version":1,"devices":[]}', encoding="utf-8")
    target.chmod(0o600)
    (state_directory / "devices.json").symlink_to(target)

    with pytest.raises(ValueError, match="cannot open web device state"):
        DeviceStore(state_directory, "https://gateway.example.ts.net")
