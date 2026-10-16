from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from tfr.core import EventBus
from tfr.gateway import EventHistory, GatewayServer
from tfr.gateway_admin import (
    create_device_pairing_url,
    gateway_socket_candidates,
    list_web_devices,
    revoke_web_device,
)
from tfr.gateway_client import GatewayClient


class AdminRuntime:
    def __init__(self, history: EventHistory) -> None:
        self.gateway_id = uuid4()
        self.history = history

    def world_descriptors(self) -> list[dict[str, object]]:
        return [
            {
                "world": "alpha",
                "session_id": "63f755aa-e407-4f78-ae05-f9d62c23f765",
                "state": "connected",
                "connection_generation": 1,
                "server": "bare",
                "encoding": "utf-8",
                "aliases": [],
                "agent": False,
            }
        ]

    def agent_descriptors(self) -> list[dict[str, object]]:
        return []


def test_admin_socket_discovery_tries_runtime_and_persistent_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    candidates = gateway_socket_candidates(None)

    assert candidates[0] == tmp_path / "runtime/tfr/gateway.sock"
    assert tmp_path / ".local/state/tfr/run/gateway.sock" in candidates
    explicit = tmp_path / "custom.sock"
    assert gateway_socket_candidates(explicit) == (explicit,)


async def test_local_admin_protocol_pairs_lists_and_revokes_without_history() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = AdminRuntime(history)
    socket_path = Path("/tmp") / f"tfr-admin-{uuid4().hex[:8]}" / "gateway.sock"
    device_id = UUID("eb4fd272-a20a-444e-8b4d-93f2e0ad2713")
    revoked: list[UUID] = []

    async def revoke(received: UUID) -> bool:
        revoked.append(received)
        return received == device_id

    server = GatewayServer(
        runtime,  # type: ignore[arg-type]
        socket_path,
        pairing_url_factory=lambda label: f"https://gateway.example.ts.net/#pair={label}",
        device_list_factory=lambda: [
            {
                "device_id": str(device_id),
                "label": "Phone",
                "scope": "chat",
                "created_at": "2026-09-23T12:00:00Z",
            }
        ],
        device_revoke_factory=revoke,
    )
    await server.start()
    try:
        assert await create_device_pairing_url(socket_path, "Phone") == (
            "https://gateway.example.ts.net/#pair=Phone"
        )
        assert (await list_web_devices(socket_path))[0]["device_id"] == str(device_id)
        await revoke_web_device(socket_path, device_id)
        assert revoked == [device_id]
    finally:
        await server.stop()
        await bus.close()
        socket_path.parent.rmdir()


async def test_attached_native_client_manages_web_devices() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = AdminRuntime(history)
    socket_path = Path("/tmp") / f"tfr-native-admin-{uuid4().hex[:8]}" / "gateway.sock"
    device_id = UUID("eb4fd272-a20a-444e-8b4d-93f2e0ad2713")
    revoked: list[UUID] = []
    cleared: list[bool] = []

    async def revoke(received: UUID) -> bool:
        revoked.append(received)
        return True

    server = GatewayServer(
        runtime,  # type: ignore[arg-type]
        socket_path,
        pairing_url_factory=lambda label: f"https://gateway.example.ts.net/#pair={label}",
        device_list_factory=lambda: [{"device_id": str(device_id), "label": "Phone"}],
        device_revoke_factory=revoke,
        web_origin="https://gateway.example.ts.net",
        diagnostic_list_factory=lambda category: [
            {
                "timestamp": "2026-10-16T00:00:00Z",
                "severity": "warning",
                "component": "presentation",
                "code": "program_rejected",
                "message": f"category={category}",
                "client": "gateway",
            }
        ],
        diagnostic_clear_factory=lambda: cleared.append(True),
    )
    await server.start()
    client = await GatewayClient.connect(socket_path)
    client.start()
    try:
        assert await client.web_status() == {
            "enabled": True,
            "origin": "https://gateway.example.ts.net",
            "device_count": 1,
        }
        assert await client.pair_web_device("Phone") == (
            "https://gateway.example.ts.net/#pair=Phone"
        )
        assert (await client.list_web_devices())[0]["device_id"] == str(device_id)
        await client.revoke_web_device(device_id)
        assert revoked == [device_id]
        assert (await client.diagnostics("presentations"))[0]["code"] == "program_rejected"
        await client.clear_diagnostics()
        assert cleared == [True]
    finally:
        await client.stop()
        await server.stop()
        await bus.close()
        socket_path.parent.rmdir()
