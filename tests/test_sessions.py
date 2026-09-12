from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import Callable
from pathlib import Path

import pytest
import trustme

from tfr.config import (
    IdleConfig,
    LoginConfig,
    ProvenanceConfig,
    TlsConfig,
    WorldConfig,
    WorldDefaults,
)
from tfr.core import CommandBus, EventBus
from tfr.events import Actor, ActorType, CommandRequest, Direction, Event, EventKind
from tfr.sessions import SessionState, TextFramer, WorldSession
from tfr.telnet import Command, Option


class MemorySink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def write(self, event: Event) -> None:
        self.events.append(event)

    async def close(self) -> None:
        pass


def server_address(server: asyncio.Server) -> tuple[str, int]:
    socket = server.sockets[0]
    host, port = socket.getsockname()[:2]
    return str(host), int(port)


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 3.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=timeout)


def make_session(
    *,
    world: str,
    host: str,
    port: int,
    sink: MemorySink,
    event_bus: EventBus,
    command_bus: CommandBus,
    **config_values: object,
) -> WorldSession:
    return WorldSession(
        world=world,
        config=WorldConfig(host=host, port=port, reconnect=False, **config_values),
        defaults=WorldDefaults(),
        event_bus=event_bus,
        command_bus=command_bus,
        reconnect_delay=0.01,
        prompt_flush_seconds=0.01,
    )


def test_text_framer_preserves_line_endings_and_prompts() -> None:
    framer = TextFramer()

    assert framer.feed("one\r") == ()
    assert framer.feed("\ntwo\nPrompt>") == ("one\r\n", "two\n")
    assert framer.flush() == "Prompt>"
    assert framer.flush() is None


def test_text_framer_splits_oversized_lines() -> None:
    framer = TextFramer(maximum_frame_characters=4)

    assert framer.feed("abcdefghij\n") == ("abcd", "efgh", "ij\n")


async def test_two_worlds_exchange_traffic_without_leakage() -> None:
    sink = MemorySink()
    event_bus = EventBus([sink])
    command_bus = CommandBus()
    received: dict[str, asyncio.Future[bytes]] = {}
    servers: list[asyncio.Server] = []

    async def start_world(label: str) -> tuple[str, int]:
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        received[label] = future

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(bytes((Command.IAC, Command.WILL, Option.ECHO)))
            writer.write(f"welcome {label}\r\nPrompt>".encode())
            await writer.drain()
            negotiation = await reader.readexactly(3)
            assert negotiation == bytes((Command.IAC, Command.DO, Option.ECHO))
            future.set_result(await reader.readline())
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        servers.append(server)
        return server_address(server)

    alpha_address = await start_world("alpha")
    beta_address = await start_world("beta")
    alpha = make_session(
        world="alpha",
        host=alpha_address[0],
        port=alpha_address[1],
        sink=sink,
        event_bus=event_bus,
        command_bus=command_bus,
    )
    beta = make_session(
        world="beta",
        host=beta_address[0],
        port=beta_address[1],
        sink=sink,
        event_bus=event_bus,
        command_bus=command_bus,
    )

    try:
        await asyncio.gather(alpha.start(), beta.start())
        await asyncio.gather(alpha.wait_connected(), beta.wait_connected())
        await command_bus.submit(
            CommandRequest(
                session_id=alpha.session_id,
                world="alpha",
                actor=Actor(ActorType.HUMAN, "operator"),
                text="look alpha",
            )
        )
        await command_bus.submit(
            CommandRequest(
                session_id=beta.session_id,
                world="beta",
                actor=Actor(ActorType.HUMAN, "operator"),
                text="look beta",
            )
        )
        assert await received["alpha"] == b"look alpha\r\n"
        assert await received["beta"] == b"look beta\r\n"
        await asyncio.gather(alpha.wait_closed(), beta.wait_closed())
    finally:
        await asyncio.gather(alpha.stop(), beta.stop())
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers))

    alpha_events = [event for event in sink.events if event.world == "alpha"]
    beta_events = [event for event in sink.events if event.world == "beta"]
    assert [event.sequence for event in alpha_events] == list(range(len(alpha_events)))
    assert [event.sequence for event in beta_events] == list(range(len(beta_events)))
    assert any(event.canonical_text == "welcome alpha\r\n" for event in alpha_events)
    assert any(event.canonical_text == "Prompt>" for event in alpha_events)
    assert not any(event.canonical_text == "welcome beta\r\n" for event in alpha_events)
    assert any(event.kind is EventKind.TELNET for event in alpha_events)


@pytest.mark.parametrize("server_type", ["tinymush", "tinymux"])
async def test_login_nospoof_and_startup_are_ordered_and_redacted(server_type: str) -> None:
    sink = MemorySink()
    event_bus = EventBus([sink])
    command_bus = CommandBus()
    received: asyncio.Future[list[bytes]] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lines = [await reader.readline() for _ in range(3)]
        received.set_result(lines)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server_address(server)
    session = make_session(
        world="agent",
        host=host,
        port=port,
        sink=sink,
        event_bus=event_bus,
        command_bus=command_bus,
        server=server_type,
        login=LoginConfig(character="Agent Bot", password="world-secret"),
        provenance=ProvenanceConfig(nospoof=True),
        startup_commands=("look",),
    )

    try:
        await session.start()
        await session.wait_closed()
        assert await received == [
            b'connect "Agent Bot" world-secret\r\n',
            b"@set me=NOSPOOF\r\n",
            b"look\r\n",
        ]
    finally:
        await session.stop()
        server.close()
        await server.wait_closed()

    outbound = [event for event in sink.events if event.direction is Direction.OUTBOUND]
    serialized_text = "\n".join(event.canonical_text or "" for event in outbound)
    assert "world-secret" not in serialized_text
    assert outbound[0].canonical_text == 'connect "Agent Bot" [REDACTED]'
    assert outbound[0].redacted is True
    assert [event.canonical_text for event in outbound[1:]] == ["@set me=NOSPOOF", "look"]


async def test_incrementally_decodes_fragmented_utf8() -> None:
    sink = MemorySink()
    event_bus = EventBus([sink])
    command_bus = CommandBus()
    encoded = "before \U0010ffff after\r\n".encode()
    split = encoded.index(b"\xf4") + 2

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(encoded[:split])
        await writer.drain()
        await asyncio.sleep(0.01)
        writer.write(encoded[split:])
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server_address(server)
    session = make_session(
        world="unicode",
        host=host,
        port=port,
        sink=sink,
        event_bus=event_bus,
        command_bus=command_bus,
    )

    try:
        await session.start()
        await session.wait_closed()
    finally:
        await session.stop()
        server.close()
        await server.wait_closed()

    inbound = [
        event.canonical_text for event in sink.events if event.direction is Direction.INBOUND
    ]
    assert "".join(inbound) == "before \U0010ffff after\r\n"
    assert not any("\N{REPLACEMENT CHARACTER}" in (text or "") for text in inbound)


async def test_session_preserves_canonical_nospoof_and_projects_display() -> None:
    sink = MemorySink()
    event_bus = EventBus([sink])
    command_bus = CommandBus()
    message = '\x1b[31m[Alice(#12),saypose]\x1b[0m Alice says, "Hi"\r\n'

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(message.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server_address(server)
    session = make_session(
        world="parsed",
        host=host,
        port=port,
        sink=sink,
        event_bus=event_bus,
        command_bus=command_bus,
        server="tinymux",
    )

    try:
        await session.start()
        await session.wait_closed()
    finally:
        await session.stop()
        server.close()
        await server.wait_closed()

    inbound = [event for event in sink.events if event.direction is Direction.INBOUND]
    assert len(inbound) == 1
    event = inbound[0]
    assert event.canonical_text == message
    assert event.plain_text == '[Alice(#12),saypose] Alice says, "Hi"\r\n'
    assert event.display_text is not None
    assert "[Alice(#12)" not in event.display_text
    assert event.kind is EventKind.SAY
    assert event.provenance is not None
    assert event.provenance.sender_dbref == 12
    assert event.metadata["message_text"] == 'Alice says, "Hi"\r\n'


async def test_idle_command_resets_after_human_output() -> None:
    sink = MemorySink()
    event_bus = EventBus([sink])
    command_bus = CommandBus()
    received: asyncio.Future[tuple[bytes, bytes, float]] = (
        asyncio.get_running_loop().create_future()
    )

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        first = await reader.readline()
        first_time = asyncio.get_running_loop().time()
        second = await reader.readline()
        elapsed = asyncio.get_running_loop().time() - first_time
        received.set_result((first, second, elapsed))
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server_address(server)
    session = make_session(
        world="idle",
        host=host,
        port=port,
        sink=sink,
        event_bus=event_bus,
        command_bus=command_bus,
        idle=IdleConfig(after_seconds=1, command="IDLE"),
    )

    try:
        await session.start()
        await session.wait_connected()
        await asyncio.sleep(0.4)
        await command_bus.submit(
            CommandRequest(
                session_id=session.session_id,
                world="idle",
                actor=Actor(ActorType.HUMAN, "operator"),
                text="look",
            )
        )
        first, second, elapsed = await asyncio.wait_for(received, timeout=2.5)
        assert first == b"look\r\n"
        assert second == b"IDLE\r\n"
        assert elapsed >= 0.85
        await session.wait_closed()
    finally:
        await session.stop()
        server.close()
        await server.wait_closed()


async def test_reconnects_with_new_connection_generation() -> None:
    sink = MemorySink()
    event_bus = EventBus([sink])
    command_bus = CommandBus()
    connections = 0
    second_open = asyncio.Event()

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        generation = connections
        writer.write(f"generation {generation}\r\n".encode())
        await writer.drain()
        if generation == 1:
            writer.close()
            await writer.wait_closed()
            return
        second_open.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_reader.read(), timeout=2)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server_address(server)
    session = WorldSession(
        world="reconnect",
        config=WorldConfig(host=host, port=port, reconnect=True),
        defaults=WorldDefaults(),
        event_bus=event_bus,
        command_bus=command_bus,
        reconnect_delay=0.01,
        prompt_flush_seconds=0.01,
    )

    try:
        await session.start()
        await asyncio.wait_for(second_open.wait(), timeout=2)
        await wait_until(
            lambda: any(event.canonical_text == "generation 2\r\n" for event in sink.events)
        )
    finally:
        await session.stop()
        server.close()
        await server.wait_closed()

    inbound = [event for event in sink.events if event.direction is Direction.INBOUND]
    assert [(event.connection_generation, event.canonical_text) for event in inbound] == [
        (1, "generation 1\r\n"),
        (2, "generation 2\r\n"),
    ]


async def test_human_quit_stops_without_reconnecting() -> None:
    sink = MemorySink()
    event_bus = EventBus([sink])
    command_bus = CommandBus()
    connections = 0
    received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        received.set_result(await reader.readline())
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server_address(server)
    session = WorldSession(
        world="intentional-quit",
        config=WorldConfig(host=host, port=port, reconnect=True),
        defaults=WorldDefaults(),
        event_bus=event_bus,
        command_bus=command_bus,
        reconnect_delay=0.01,
    )

    try:
        await session.start()
        await session.wait_connected()
        await command_bus.submit(
            CommandRequest(
                session_id=session.session_id,
                world=session.world,
                actor=Actor(ActorType.HUMAN, "operator"),
                text="QUIT",
            )
        )
        assert await asyncio.wait_for(received, timeout=1) == b"QUIT\r\n"
        await session.wait_closed(timeout=1)
        await asyncio.sleep(0.05)
    finally:
        await session.stop()
        server.close()
        await server.wait_closed()

    assert connections == 1
    assert session.state is SessionState.STOPPED
    states = [
        event.metadata.get("state") for event in sink.events if event.kind is EventKind.CONNECTION
    ]
    assert SessionState.RECONNECT_WAIT.value not in states
    disconnected = [
        event
        for event in sink.events
        if event.kind is EventKind.CONNECTION
        and event.metadata.get("state") == SessionState.DISCONNECTED.value
    ]
    assert disconnected[0].metadata["intentional"] is True


async def test_verified_tls_connection(tmp_path: Path) -> None:
    ca = trustme.CA()
    certificate = ca.issue_cert("localhost")
    ca_path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(ca_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certificate.configure_cert(server_context)

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"secure\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    host, port = server_address(server)
    sink = MemorySink()
    event_bus = EventBus([sink])
    session = make_session(
        world="secure",
        host=host,
        port=port,
        sink=sink,
        event_bus=event_bus,
        command_bus=CommandBus(),
        tls=TlsConfig(
            enabled=True,
            verify=True,
            ca_file=ca_path,
            server_hostname="localhost",
        ),
    )

    try:
        await session.start()
        await session.wait_closed()
    finally:
        await session.stop()
        server.close()
        await server.wait_closed()

    assert any(event.canonical_text == "secure\r\n" for event in sink.events)
    connected = [
        event
        for event in sink.events
        if event.kind is EventKind.CONNECTION
        and event.metadata.get("state") == SessionState.CONNECTED
    ]
    assert connected[0].metadata["tls_verified"] is True


async def test_tls_hostname_failure_is_reported(tmp_path: Path) -> None:
    ca = trustme.CA()
    certificate = ca.issue_cert("localhost")
    ca_path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(ca_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certificate.configure_cert(server_context)

    server = await asyncio.start_server(
        lambda _reader, _writer: None,
        "127.0.0.1",
        0,
        ssl=server_context,
    )
    host, port = server_address(server)
    sink = MemorySink()
    event_bus = EventBus([sink])
    session = make_session(
        world="bad-tls",
        host=host,
        port=port,
        sink=sink,
        event_bus=event_bus,
        command_bus=CommandBus(),
        tls=TlsConfig(
            enabled=True,
            verify=True,
            ca_file=ca_path,
            server_hostname="wrong.example",
        ),
    )

    try:
        await session.start()
        await session.wait_closed()
    finally:
        await session.stop()
        server.close()
        await server.wait_closed()

    disconnected = [
        event
        for event in sink.events
        if event.kind is EventKind.CONNECTION
        and event.metadata.get("state") == SessionState.DISCONNECTED
    ]
    assert disconnected[0].metadata["error_type"] == "SSLCertVerificationError"
    assert not any(event.direction is Direction.INBOUND for event in sink.events)
