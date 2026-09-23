from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from tfr.core import EventBus
from tfr.gateway import EventHistory, GatewayServer
from tfr.gateway_admin import create_device_pairing_url, list_web_devices, revoke_web_device


class AdminRuntime:
    def __init__(self, history: EventHistory) -> None:
        self.gateway_id = uuid4()
        self.history = history

    def world_descriptors(self) -> list[dict[str, object]]:
        return [{"world": "alpha"}]

    def agent_descriptors(self) -> list[dict[str, object]]:
        return []


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
