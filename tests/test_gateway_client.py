from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import trustme

from tfr.config import GatewayReconnectConfig
from tfr.core import EventBus
from tfr.events import Actor, ActorType, CommandRequest, Direction, Event, EventKind
from tfr.gateway import EventHistory, GatewayServer
from tfr.gateway_client import GatewayClient, GatewayDisconnectedError, GatewayUiRuntime
from tfr.gateway_protocol import GatewayProtocolError
from tfr.gateway_transport import (
    create_gateway_client_tls_context,
    create_gateway_server_tls_context,
)
from tfr.plugins import PluginManager


def make_event(sequence: int) -> Event:
    return Event(
        session_id=UUID("63f755aa-e407-4f78-ae05-f9d62c23f765"),
        world="alpha",
        connection_generation=1,
        sequence=sequence,
        direction=Direction.INBOUND,
        kind=EventKind.RAW_OUTPUT,
        canonical_text=f"line {sequence}",
        display_text=f"line {sequence}",
    )


async def test_missing_gateway_socket_reports_the_path() -> None:
    socket_path = Path("/tmp") / f"tfr-missing-{uuid4().hex[:8]}.sock"

    with pytest.raises(GatewayDisconnectedError, match=r"missing-.*\.sock.*start `tfr gateway`"):
        await GatewayClient.connect(socket_path)


class FakeRuntime:
    def __init__(self, history: EventHistory) -> None:
        self.gateway_id = UUID("92716400-4bb9-43d2-845f-b8a0e51c9994")
        self.history = history
        self.commands: list[CommandRequest] = []
        self.agent_controls: list[tuple[str, str]] = []

    def world_descriptors(self) -> list[dict[str, object]]:
        return [
            {
                "world": "alpha",
                "session_id": "63f755aa-e407-4f78-ae05-f9d62c23f765",
                "state": "connected",
                "server": "bare",
                "encoding": "utf-8",
                "agent": False,
            }
        ]

    def agent_descriptors(self) -> list[dict[str, object]]:
        return [
            {
                "name": "bot",
                "world": "alpha",
                "state": "idle",
                "paused": False,
                "provider": "local",
                "endpoint": "http://localhost:11434/v1",
                "model": "example",
            }
        ]

    async def submit_command(
        self,
        *,
        world: str,
        text: str,
        client_id: str,
        request_id: UUID,
        sensitive: bool = False,
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        metadata: object = None,
    ) -> None:
        self.commands.append(
            CommandRequest(
                session_id=UUID("63f755aa-e407-4f78-ae05-f9d62c23f765"),
                world=world,
                actor=actor or Actor(ActorType.HUMAN, client_id),
                request_id=request_id,
                text=text,
                sensitive=sensitive,
                correlation_id=correlation_id,
                causation_id=causation_id,
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        )

    async def control(self, *, world: str, action: str) -> None:
        pass

    async def agent_control(self, *, name: str, action: str) -> None:
        self.agent_controls.append((name, action))


async def test_client_receives_snapshot_live_events_and_command_acks() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    await bus.publish(make_event(0))
    await history.flush()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()
    client = await GatewayClient.connect(socket_path)
    queue = client.event_bus.subscribe()
    client.start()
    try:
        assert [event.canonical_text for event in client.initial_events] == ["line 0"]
        await bus.publish(make_event(1))
        received = await asyncio.wait_for(queue.get(), timeout=1)
        assert received.canonical_text == "line 1"

        request = CommandRequest(
            session_id=client.sessions[0].session_id,
            world="alpha",
            actor=Actor(ActorType.HUMAN, "operator"),
            text="look",
        )
        await asyncio.wait_for(client.command_bus.submit(request), timeout=1)
        assert runtime.commands[0].text == "look"
        assert runtime.commands[0].request_id == request.request_id

        correlation_id = uuid4()
        causation_id = uuid4()
        plugin_request = CommandRequest(
            session_id=client.sessions[0].session_id,
            world="alpha",
            actor=Actor(ActorType.PLUGIN, "cat"),
            text="@emit test",
            sensitive=True,
            correlation_id=correlation_id,
            causation_id=causation_id,
            metadata={"line": 1},
        )
        await asyncio.wait_for(client.command_bus.submit(plugin_request), timeout=1)
        restored = runtime.commands[1]
        assert restored.actor == plugin_request.actor
        assert restored.sensitive is True
        assert restored.correlation_id == correlation_id
        assert restored.causation_id == causation_id
        assert restored.metadata["line"] == 1

        assert client.agents is not None
        assert client.agents.for_world("alpha") is not None
        assert await client.agents.pause("bot") is True
        assert runtime.agent_controls == [("bot", "pause")]
        assert client.agents.controllers["bot"].paused is True
    finally:
        await client.stop()
        await server.stop()
        await history.stop()
        await bus.close()


async def test_new_ui_can_rebuild_display_from_retained_gateway_history() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    await bus.publish(make_event(0))
    await bus.publish(make_event(1))
    await history.flush()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()

    client = await GatewayClient.connect(socket_path, after_cursor=None)
    try:
        assert [event.canonical_text for event in client.initial_events] == ["line 0", "line 1"]
    finally:
        await client.stop()
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_client_reconnects_and_restores_only_missed_events() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    await bus.publish(make_event(0))
    await history.flush()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()
    client = await GatewayClient.connect(socket_path)
    queue = client.event_bus.subscribe()
    client.start()
    try:
        await client.stop()
        await bus.publish(make_event(1))
        await history.flush()

        restored = await client.reconnect()

        assert restored == 1
        assert (await asyncio.wait_for(queue.get(), timeout=1)).canonical_text == "line 1"
        await bus.publish(make_event(2))
        assert (await asyncio.wait_for(queue.get(), timeout=1)).canonical_text == "line 2"
    finally:
        await client.stop()
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_client_reconnect_requires_reload_after_gateway_restart() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()
    client = await GatewayClient.connect(socket_path)
    client.start()
    try:
        await client.stop()
        runtime.gateway_id = uuid4()

        with pytest.raises(GatewayDisconnectedError, match=r"use /reload"):
            await client.reconnect()
    finally:
        await client.stop()
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_ping_and_verify_connection_confirm_a_live_gateway() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()
    client = await GatewayClient.connect(socket_path)
    client.start()
    try:
        await asyncio.wait_for(client.ping(), timeout=1)
        assert await client.verify_connection(timeout=1) is True
    finally:
        await client.stop()
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_verify_connection_returns_false_once_the_client_is_stopped() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()
    client = await GatewayClient.connect(socket_path)
    client.start()
    try:
        await client.stop()
        assert await client.verify_connection(timeout=1) is False
    finally:
        await client.stop()
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def make_plugin_manager(client: GatewayClient) -> PluginManager:
    return await PluginManager.load(
        enabled=(),
        config={},
        event_bus=client.event_bus,
        command_bus=client.command_bus,  # type: ignore[arg-type]
        targets={session.world: session.session_id for session in client.sessions},
        scope="ui",
    )


async def test_gateway_ui_runtime_supervisor_leaves_a_healthy_connection_alone() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()
    client = await GatewayClient.connect(socket_path)
    client.start()
    notices: list[str] = []
    ui_runtime = GatewayUiRuntime(
        client,
        await make_plugin_manager(client),
        reconnect_config=GatewayReconnectConfig(
            heartbeat_seconds=0.02,
            ping_timeout_seconds=1,
            max_attempts=3,
            retry_interval_seconds=0.01,
        ),
        notify=notices.append,
    )
    try:
        await ui_runtime.start()
        await asyncio.sleep(0.15)
        assert notices == []
    finally:
        await ui_runtime.stop()
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_gateway_ui_runtime_supervisor_recovers_from_a_dropped_connection() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()
    client = await GatewayClient.connect(socket_path)
    client.start()
    notices: list[str] = []
    ui_runtime = GatewayUiRuntime(
        client,
        await make_plugin_manager(client),
        reconnect_config=GatewayReconnectConfig(
            heartbeat_seconds=0.02,
            ping_timeout_seconds=1,
            max_attempts=3,
            retry_interval_seconds=0.01,
        ),
        notify=notices.append,
    )
    try:
        await ui_runtime.start()
        # Simulate a dropped connection the way a dead socket would surface:
        # the pump ends without the runtime having asked for it.
        await client.stop()

        await asyncio.wait_for(
            _wait_until(lambda: any("Reconnected to Gateway" in text for text in notices)),
            timeout=2,
        )
        assert any("reconnecting (attempt 1/3)" in text for text in notices)
    finally:
        await ui_runtime.stop()
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_gateway_ui_runtime_recover_connection_gives_up_after_max_attempts() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()
    client = await GatewayClient.connect(socket_path)
    client.start()
    notices: list[str] = []
    ui_runtime = GatewayUiRuntime(
        client,
        await make_plugin_manager(client),
        reconnect_config=GatewayReconnectConfig(
            heartbeat_seconds=10,
            ping_timeout_seconds=1,
            max_attempts=3,
            retry_interval_seconds=0.01,
        ),
        notify=notices.append,
    )
    await client.stop()
    await server.stop()
    try:
        await asyncio.wait_for(ui_runtime._recover_connection(), timeout=2)
    finally:
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()

    attempts = [text for text in notices if "reconnecting (attempt" in text]
    assert len(attempts) == 3
    assert attempts[-1].endswith("(attempt 3/3)...")
    assert any("failed after 3 attempts" in text for text in notices)
    assert "Use /gateway reconnect to retry manually." in notices[-1]


async def test_gateway_ui_runtime_stop_cancels_the_supervisor_promptly() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()
    client = await GatewayClient.connect(socket_path)
    client.start()
    notices: list[str] = []
    ui_runtime = GatewayUiRuntime(
        client,
        await make_plugin_manager(client),
        reconnect_config=GatewayReconnectConfig(heartbeat_seconds=10),
        notify=notices.append,
    )
    try:
        await ui_runtime.start()
        await asyncio.wait_for(ui_runtime.stop(), timeout=1)
        assert ui_runtime._supervisor_task is None
        assert notices == []
    finally:
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def _wait_until(predicate: Callable[[], bool], *, interval: float = 0.01) -> None:
    while not predicate():
        await asyncio.sleep(interval)


async def test_client_connects_to_authenticated_tls_gateway(tmp_path: Path) -> None:
    ca = trustme.CA()
    certificate = ca.issue_cert("localhost")
    ca_path = tmp_path / "ca.pem"
    certificate_path = tmp_path / "gateway.pem"
    key_path = tmp_path / "gateway.key"
    ca.cert_pem.write_to_path(ca_path)
    certificate.cert_chain_pems[0].write_to_path(certificate_path)
    certificate.private_key_pem.write_to_path(key_path)
    key_path.chmod(0o600)

    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    await bus.publish(make_event(0))
    await history.flush()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-tcp-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(
        runtime,  # type: ignore[arg-type]
        socket_path,
        tcp_host="127.0.0.1",
        tcp_port=0,
        tcp_auth_token="a" * 64,
        tcp_ssl_context=create_gateway_server_tls_context(certificate_path, key_path),
    )
    await server.start()
    port = int(server.tcp_addresses[0][1])
    client = await GatewayClient.connect_tcp(
        "127.0.0.1",
        port,
        auth_token="a" * 64,
        tls_context=create_gateway_client_tls_context(ca_path),
        server_hostname="localhost",
    )
    try:
        assert [event.canonical_text for event in client.initial_events] == ["line 0"]
        assert client.sessions[0].world == "alpha"
    finally:
        await client.stop()
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_tls_gateway_rejects_wrong_token_before_hello(tmp_path: Path) -> None:
    ca = trustme.CA()
    certificate = ca.issue_cert("localhost")
    ca_path = tmp_path / "ca.pem"
    certificate_path = tmp_path / "gateway.pem"
    key_path = tmp_path / "gateway.key"
    ca.cert_pem.write_to_path(ca_path)
    certificate.cert_chain_pems[0].write_to_path(certificate_path)
    certificate.private_key_pem.write_to_path(key_path)
    key_path.chmod(0o600)
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-tcp-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(
        runtime,  # type: ignore[arg-type]
        socket_path,
        tcp_host="127.0.0.1",
        tcp_port=0,
        tcp_auth_token="a" * 64,
        tcp_ssl_context=create_gateway_server_tls_context(certificate_path, key_path),
    )
    await server.start()
    try:
        with pytest.raises(GatewayProtocolError, match="authentication failed"):
            await GatewayClient.connect_tcp(
                "127.0.0.1",
                int(server.tcp_addresses[0][1]),
                auth_token="b" * 64,
                tls_context=create_gateway_client_tls_context(ca_path),
                server_hostname="localhost",
            )
    finally:
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_tls_gateway_does_not_disable_hostname_verification(tmp_path: Path) -> None:
    ca = trustme.CA()
    certificate = ca.issue_cert("localhost")
    ca_path = tmp_path / "ca.pem"
    certificate_path = tmp_path / "gateway.pem"
    key_path = tmp_path / "gateway.key"
    ca.cert_pem.write_to_path(ca_path)
    certificate.cert_chain_pems[0].write_to_path(certificate_path)
    certificate.private_key_pem.write_to_path(key_path)
    key_path.chmod(0o600)
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-tcp-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(
        runtime,  # type: ignore[arg-type]
        socket_path,
        tcp_host="127.0.0.1",
        tcp_port=0,
        tcp_auth_token="a" * 64,
        tcp_ssl_context=create_gateway_server_tls_context(certificate_path, key_path),
    )
    await server.start()
    try:
        with pytest.raises(GatewayDisconnectedError, match="verified gateway connection"):
            await GatewayClient.connect_tcp(
                "127.0.0.1",
                int(server.tcp_addresses[0][1]),
                auth_token="a" * 64,
                tls_context=create_gateway_client_tls_context(ca_path),
                server_hostname="wrong.example",
            )
    finally:
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_tcp_connection_api_rejects_unverified_tls_context() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with pytest.raises(ValueError, match="verify certificates and hostnames"):
        await GatewayClient.connect_tcp(
            "127.0.0.1",
            7347,
            auth_token="a" * 64,
            tls_context=context,
        )


async def test_unauthenticated_tcp_client_does_not_consume_unix_capacity(
    tmp_path: Path,
) -> None:
    ca = trustme.CA()
    certificate = ca.issue_cert("localhost")
    ca_path = tmp_path / "ca.pem"
    certificate_path = tmp_path / "gateway.pem"
    key_path = tmp_path / "gateway.key"
    ca.cert_pem.write_to_path(ca_path)
    certificate.cert_chain_pems[0].write_to_path(certificate_path)
    certificate.private_key_pem.write_to_path(key_path)
    key_path.chmod(0o600)
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-tcp-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(
        runtime,  # type: ignore[arg-type]
        socket_path,
        maximum_clients=1,
        maximum_tcp_clients=1,
        tcp_host="127.0.0.1",
        tcp_port=0,
        tcp_auth_token="a" * 64,
        tcp_ssl_context=create_gateway_server_tls_context(certificate_path, key_path),
    )
    await server.start()
    _tcp_reader, tcp_writer = await asyncio.open_connection(
        "127.0.0.1",
        int(server.tcp_addresses[0][1]),
        ssl=create_gateway_client_tls_context(ca_path),
        server_hostname="localhost",
    )
    await asyncio.sleep(0)
    client = await GatewayClient.connect(socket_path)
    try:
        assert client.sessions[0].world == "alpha"
    finally:
        tcp_writer.close()
        await tcp_writer.wait_closed()
        await client.stop()
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_unauthenticated_tcp_client_does_not_consume_authenticated_capacity(
    tmp_path: Path,
) -> None:
    ca = trustme.CA()
    certificate = ca.issue_cert("localhost")
    ca_path = tmp_path / "ca.pem"
    certificate_path = tmp_path / "gateway.pem"
    key_path = tmp_path / "gateway.key"
    ca.cert_pem.write_to_path(ca_path)
    certificate.cert_chain_pems[0].write_to_path(certificate_path)
    certificate.private_key_pem.write_to_path(key_path)
    key_path.chmod(0o600)
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-tcp-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(
        runtime,  # type: ignore[arg-type]
        socket_path,
        maximum_tcp_clients=1,
        tcp_host="127.0.0.1",
        tcp_port=0,
        tcp_auth_token="a" * 64,
        tcp_ssl_context=create_gateway_server_tls_context(certificate_path, key_path),
    )
    await server.start()
    port = int(server.tcp_addresses[0][1])
    _pending_reader, pending_writer = await asyncio.open_connection(
        "127.0.0.1",
        port,
        ssl=create_gateway_client_tls_context(ca_path),
        server_hostname="localhost",
    )
    await asyncio.sleep(0)
    client = await GatewayClient.connect_tcp(
        "127.0.0.1",
        port,
        auth_token="a" * 64,
        tls_context=create_gateway_client_tls_context(ca_path),
        server_hostname="localhost",
    )
    try:
        assert client.sessions[0].world == "alpha"
    finally:
        pending_writer.close()
        await pending_writer.wait_closed()
        await client.stop()
        await server.stop()
        await history.stop()
        await bus.close()
    socket_path.parent.rmdir()
