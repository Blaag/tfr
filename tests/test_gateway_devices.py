from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tfr.gateway_devices import DeviceStore


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


async def test_pairing_code_is_atomic_and_single_use(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "web", "https://gateway.example.ts.net")
    code = pairing_code(store.create_pairing_url("Phone", ("alpha",)))

    results = await asyncio.gather(
        store.redeem(code, "black@example.com"),
        store.redeem(code, "black@example.com"),
        return_exceptions=True,
    )

    assert sum(isinstance(result, tuple) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1


def test_device_state_rejects_symlinks(tmp_path: Path) -> None:
    state_directory = tmp_path / "web"
    state_directory.mkdir(mode=0o700)
    target = tmp_path / "devices.json"
    target.write_text('{"schema_version":1,"devices":[]}', encoding="utf-8")
    target.chmod(0o600)
    (state_directory / "devices.json").symlink_to(target)

    with pytest.raises(ValueError, match="cannot open web device state"):
        DeviceStore(state_directory, "https://gateway.example.ts.net")
