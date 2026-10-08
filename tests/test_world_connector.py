from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tfr.config import WorldConfig, WorldDefaults
from tfr.core import CommandBus, EventBus
from tfr.events import Actor, ActorType, CommandRequest, Event
from tfr.sessions import ConnectorSessionBridge, ConnectorWorldSession, SessionState
from tfr.world_connector import (
    CONNECTOR_GRACE_SECONDS,
    ConnectorLease,
    FrameDecoder,
    ProtocolError,
    ReplayBuffer,
    WorldConnectorClient,
    WorldConnectorServer,
    connect_or_spawn_connector,
    connector_build_id,
    encode_message,
)


def test_connector_grace_period_is_thirty_minutes() -> None:
    assert CONNECTOR_GRACE_SECONDS == 30 * 60


def test_lease_stays_alive_while_attached() -> None:
    lease = ConnectorLease(now=10)

    assert lease.expired(now=100_000) is False
    assert lease.deadline is None


def test_detach_starts_grace_period_and_exact_deadline_expires() -> None:
    lease = ConnectorLease(now=10)
    lease.detach(now=20)

    assert lease.deadline == 20 + CONNECTOR_GRACE_SECONDS
    assert lease.expired(now=lease.deadline - 0.001) is False
    assert lease.expired(now=lease.deadline) is True


def test_reattach_cancels_grace_period() -> None:
    lease = ConnectorLease(now=0)
    lease.detach(now=5)
    lease.attach(now=100)

    assert lease.deadline is None
    assert lease.expired(now=100_000) is False


def test_repeated_detach_does_not_extend_existing_deadline() -> None:
    lease = ConnectorLease(now=0)
    lease.detach(now=5)
    lease.detach(now=500)

    assert lease.deadline == 5 + CONNECTOR_GRACE_SECONDS


def test_replay_buffer_assigns_monotonic_sequences_per_world() -> None:
    replay = ReplayBuffer(per_world_bytes=100, global_bytes=200)

    first = replay.append("alpha", b"first")
    second = replay.append("alpha", b"second")
    other = replay.append("beta", b"other")

    assert (first.sequence, second.sequence, other.sequence) == (1, 2, 1)
    assert replay.frames("alpha") == (first, second)
    assert replay.frames("beta") == (other,)


def test_acknowledgement_releases_only_acknowledged_world_frames() -> None:
    replay = ReplayBuffer(per_world_bytes=100, global_bytes=200)
    replay.append("alpha", b"one")
    second = replay.append("alpha", b"two")
    beta = replay.append("beta", b"beta")

    replay.acknowledge("alpha", 1)

    assert replay.frames("alpha") == (second,)
    assert replay.frames("beta") == (beta,)


def test_stale_acknowledgement_is_idempotent() -> None:
    replay = ReplayBuffer(per_world_bytes=100, global_bytes=200)
    replay.append("alpha", b"one")
    second = replay.append("alpha", b"two")
    replay.acknowledge("alpha", 1)

    replay.acknowledge("alpha", 1)

    assert replay.frames("alpha") == (second,)


def test_acknowledgement_cannot_exceed_last_assigned_sequence() -> None:
    replay = ReplayBuffer(per_world_bytes=100, global_bytes=200)
    replay.append("alpha", b"one")

    with pytest.raises(ValueError, match="beyond latest sequence"):
        replay.acknowledge("alpha", 2)


def test_per_world_overflow_drops_oldest_complete_frames_and_reports_gap() -> None:
    replay = ReplayBuffer(per_world_bytes=4, global_bytes=100)
    replay.append("alpha", b"111")
    replay.append("alpha", b"22")
    retained = replay.append("alpha", b"333")

    notices = replay.overflow_notices()

    assert replay.frames("alpha") == (retained,)
    assert len(notices) == 2
    assert all(notice.world == "alpha" for notice in notices)
    assert [notice.first_dropped_sequence for notice in notices] == [1, 2]
    assert [notice.last_dropped_sequence for notice in notices] == [1, 2]
    assert sum(notice.dropped_frames for notice in notices) == 2
    assert sum(notice.dropped_bytes for notice in notices) == 5


def test_global_overflow_drops_globally_oldest_frame() -> None:
    replay = ReplayBuffer(per_world_bytes=100, global_bytes=5)
    replay.append("alpha", b"aaa")
    beta = replay.append("beta", b"bbb")

    assert replay.frames("alpha") == ()
    assert replay.frames("beta") == (beta,)
    assert replay.overflow_notices()[0].world == "alpha"


def test_oversized_single_frame_is_reported_but_not_retained() -> None:
    replay = ReplayBuffer(per_world_bytes=4, global_bytes=10)
    frame = replay.append("alpha", b"oversized")

    assert replay.frames("alpha") == ()
    notice = replay.overflow_notices()[0]
    assert notice.first_dropped_sequence == frame.sequence
    assert notice.dropped_bytes == len(frame.payload)


def test_overflow_notice_persists_until_acknowledged() -> None:
    replay = ReplayBuffer(per_world_bytes=2, global_bytes=10)
    replay.append("alpha", b"one")
    notice = replay.overflow_notices()[0]

    assert replay.overflow_notices() == (notice,)
    replay.acknowledge_overflow(notice.notice_id)
    assert replay.overflow_notices() == ()


def test_unknown_overflow_acknowledgement_is_rejected() -> None:
    replay = ReplayBuffer(per_world_bytes=2, global_bytes=10)

    with pytest.raises(ValueError, match="unknown overflow notice"):
        replay.acknowledge_overflow(99)


def test_message_framing_handles_partial_and_multiple_messages() -> None:
    decoder = FrameDecoder(maximum_bytes=100)
    first = encode_message({"type": "hello", "value": 1})
    second = encode_message({"type": "ack", "value": 2})

    assert decoder.feed(first[:3]) == ()
    assert decoder.feed(first[3:] + second) == (
        {"type": "hello", "value": 1},
        {"type": "ack", "value": 2},
    )


def test_message_framing_rejects_oversized_and_non_object_messages() -> None:
    decoder = FrameDecoder(maximum_bytes=10)
    with pytest.raises(ProtocolError, match="size limit"):
        decoder.feed(b"x" * 11)

    decoder = FrameDecoder(maximum_bytes=100)
    with pytest.raises(ProtocolError, match="JSON object"):
        decoder.feed(json.dumps(["not", "object"]).encode() + b"\n")


def test_connector_build_id_is_stable_sha256() -> None:
    build_id = connector_build_id()

    assert len(build_id) == 64
    assert set(build_id) <= set("0123456789abcdef")


async def _read_message(reader: object) -> dict[str, object]:
    content = await reader.readline()  # type: ignore[attr-defined]
    value = json.loads(content)
    assert isinstance(value, dict)
    return value


async def _attach(path: Path, *, build_id: str) -> tuple[object, object, dict[str, object]]:
    import asyncio

    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(
        encode_message(
            {
                "type": "attach",
                "protocol": 1,
                "build_id": build_id,
                "worlds": {},
            }
        )
    )
    await writer.drain()
    return reader, writer, await _read_message(reader)


def _socket_path(tmp_path: Path) -> Path:
    digest = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    return tmp_path.parents[1] / f"wc-{digest}.sock"


async def test_connector_server_creates_owner_only_socket_and_accepts_exact_build(
    tmp_path: Path,
) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    try:
        reader, writer, welcome = await _attach(path, build_id="a" * 64)
        assert welcome == {
            "type": "attached",
            "protocol": 1,
            "build_id": "a" * 64,
        }
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        writer.close()
        await writer.wait_closed()
        del reader
    finally:
        await server.stop()


async def test_connector_server_rejects_incompatible_build(tmp_path: Path) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    try:
        _reader, writer, response = await _attach(path, build_id="b" * 64)
        assert response["type"] == "rejected"
        assert response["reason"] == "build_id"
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_connector_server_rejects_second_gateway(tmp_path: Path) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    try:
        _first_reader, first_writer, _welcome = await _attach(path, build_id="a" * 64)
        _second_reader, second_writer, response = await _attach(path, build_id="a" * 64)
        assert response == {"type": "rejected", "reason": "already_attached"}
        second_writer.close()
        first_writer.close()
        await second_writer.wait_closed()
        await first_writer.wait_closed()
    finally:
        await server.stop()


async def test_connector_server_replays_unacknowledged_frames_after_reattach(
    tmp_path: Path,
) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    try:
        first_reader, first_writer, _welcome = await _attach(path, build_id="a" * 64)
        frame = await server.publish("alpha", b"hello")
        assert (await _read_message(first_reader))["sequence"] == frame.sequence
        first_writer.close()
        await first_writer.wait_closed()
        await server.wait_detached()

        second_reader, second_writer, _welcome = await _attach(path, build_id="a" * 64)
        replayed = await _read_message(second_reader)
        assert replayed["type"] == "frame"
        assert replayed["world"] == "alpha"
        assert replayed["payload"] == "aGVsbG8="
        second_writer.close()
        await second_writer.wait_closed()
    finally:
        await server.stop()


async def test_connector_server_ack_prevents_replay(tmp_path: Path) -> None:
    import asyncio

    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    try:
        reader, writer, _welcome = await _attach(path, build_id="a" * 64)
        await server.publish("alpha", b"hello")
        frame = await _read_message(reader)
        writer.write(encode_message({"type": "ack", "world": "alpha", "sequence": 1}))
        await writer.drain()
        await server.wait_for_ack("alpha", 1)
        writer.close()
        await writer.wait_closed()
        await server.wait_detached()

        next_reader, next_writer, _welcome = await _attach(path, build_id="a" * 64)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(next_reader.readline(), timeout=0.05)
        assert frame["sequence"] == 1
        next_writer.close()
        await next_writer.wait_closed()
    finally:
        await server.stop()


async def test_connector_server_delivers_overflow_before_retained_frames(tmp_path: Path) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(
        path,
        build_id="a" * 64,
        grace_seconds=1,
        per_world_bytes=3,
        global_bytes=10,
    )
    await server.start()
    try:
        await server.publish("alpha", b"old")
        await server.publish("alpha", b"new")
        reader, writer, _welcome = await _attach(path, build_id="a" * 64)

        overflow = await _read_message(reader)
        retained = await _read_message(reader)
        assert overflow["type"] == "overflow"
        assert overflow["first_dropped_sequence"] == 1
        assert retained["type"] == "frame"
        assert retained["sequence"] == 2
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_connector_server_reports_live_overflow_before_new_frame(tmp_path: Path) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(
        path,
        build_id="a" * 64,
        grace_seconds=1,
        per_world_bytes=3,
        global_bytes=10,
    )
    await server.start()
    try:
        reader, writer, _welcome = await _attach(path, build_id="a" * 64)
        await server.publish("alpha", b"old")
        assert (await _read_message(reader))["sequence"] == 1
        await server.publish("alpha", b"new")

        overflow = await _read_message(reader)
        frame = await _read_message(reader)
        assert overflow["type"] == "overflow"
        assert overflow["first_dropped_sequence"] == 1
        assert frame["type"] == "frame"
        assert frame["sequence"] == 2
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_connector_server_expires_after_detached_grace_period(tmp_path: Path) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=0.05)
    await server.start()
    reader, writer, _welcome = await _attach(path, build_id="a" * 64)
    writer.close()
    await writer.wait_closed()
    del reader

    await server.wait_expired()

    assert server.expired is True
    await server.stop()
    assert not os.path.lexists(path)


async def test_connector_client_validates_handshake_receives_and_acknowledges(
    tmp_path: Path,
) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    client = WorldConnectorClient(path, build_id="a" * 64)
    try:
        await client.connect()
        await server.publish("alpha", b"hello")

        message = await client.receive()
        assert message == {
            "type": "frame",
            "world": "alpha",
            "sequence": 1,
            "payload": b"hello",
        }
        await client.acknowledge("alpha", 1)
        await server.wait_for_ack("alpha", 1)
    finally:
        await client.close()
        await server.stop()


async def test_connector_client_reports_build_mismatch(tmp_path: Path) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    try:
        client = WorldConnectorClient(path, build_id="b" * 64)
        with pytest.raises(ProtocolError, match="build_id"):
            await client.connect()
    finally:
        await server.stop()


async def test_incompatible_gateway_can_request_connector_replacement(tmp_path: Path) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    try:
        client = WorldConnectorClient(path, build_id="b" * 64)
        existing = await client.request_replacement()
        assert existing == "a" * 64
        await server.wait_expired()
    finally:
        await server.stop()


async def test_connector_client_decodes_overflow_and_acknowledges_notice(
    tmp_path: Path,
) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(
        path,
        build_id="a" * 64,
        grace_seconds=1,
        per_world_bytes=2,
        global_bytes=10,
    )
    await server.start()
    await server.publish("alpha", b"lost")
    client = WorldConnectorClient(path, build_id="a" * 64)
    try:
        await client.connect()
        notice = await client.receive()
        assert notice["type"] == "overflow"
        assert notice["world"] == "alpha"
        await client.acknowledge_overflow(notice["notice_id"])
        await server.wait_for_overflow_ack(notice["notice_id"])
    finally:
        await client.close()
        await server.stop()


async def test_connector_runs_as_lightweight_child_and_stops_on_gateway_shutdown(
    tmp_path: Path,
) -> None:
    import asyncio

    path = _socket_path(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "tfr.world_connector", "--socket", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    client = WorldConnectorClient(path)
    try:
        for _attempt in range(100):
            try:
                await client.connect()
                break
            except (ConnectionError, FileNotFoundError):
                await asyncio.sleep(0.01)
        else:
            raise AssertionError("connector child did not create its socket")
        await client.shutdown()
        await client.close()
        assert await asyncio.to_thread(process.wait, 2) == 0
        assert not os.path.lexists(path)
    finally:
        await client.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)


def test_connector_module_imports_only_lightweight_standard_library() -> None:
    command = [
        sys.executable,
        "-I",
        "-c",
        (
            "import json,sys; import tfr.world_connector; "
            "print(json.dumps(sorted(name for name in sys.modules "
            "if name.startswith(('prompt_toolkit','pydantic','openai','tfr.plugins',"
            "'tfr.gateway','tfr.tui')))))"
        ),
    ]

    result = subprocess.run(command, check=True, capture_output=True, text=True)

    assert json.loads(result.stdout) == []


async def test_gateway_session_objects_can_be_replaced_without_world_reconnect(
    tmp_path: Path,
) -> None:
    import asyncio

    connections = 0
    commands: asyncio.Queue[bytes] = asyncio.Queue()
    world_writers: list[asyncio.StreamWriter] = []

    async def accept_world(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal connections
        connections += 1
        world_writers.append(writer)
        try:
            while line := await reader.readline():
                await commands.put(line)
        finally:
            writer.close()
            await writer.wait_closed()

    class Sink:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def write(self, event: Event) -> None:
            self.events.append(event)

        async def close(self) -> None:
            pass

    world_server = await asyncio.start_server(accept_world, "127.0.0.1", 0)
    port = world_server.sockets[0].getsockname()[1]
    worlds = {
        "alpha": {
            "host": "127.0.0.1",
            "port": port,
            "encoding": "utf-8",
            "reconnect": False,
            "tls": {"enabled": False},
            "startup_commands": [],
            "login": None,
            "idle": None,
        }
    }
    path = _socket_path(tmp_path)
    connector = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await connector.start()

    async def gateway_session() -> tuple[
        WorldConnectorClient,
        ConnectorSessionBridge,
        ConnectorWorldSession,
        CommandBus,
        Sink,
    ]:
        client = WorldConnectorClient(path, build_id="a" * 64, worlds=worlds)
        await asyncio.wait_for(client.connect(), timeout=1)
        bridge = ConnectorSessionBridge(client)
        sink = Sink()
        event_bus = EventBus([sink])
        command_bus = CommandBus()
        session = ConnectorWorldSession(
            bridge=bridge,
            world="alpha",
            config=WorldConfig(host="127.0.0.1", port=port, reconnect=False),
            defaults=WorldDefaults(),
            event_bus=event_bus,
            command_bus=command_bus,
        )
        bridge.start()
        await asyncio.wait_for(session.start(), timeout=1)
        return client, bridge, session, command_bus, sink

    first_client, first_bridge, first_session, first_bus, _first_sink = await gateway_session()
    try:
        for _attempt in range(100):
            if first_session.state is SessionState.CONNECTED:
                break
            await asyncio.sleep(0.01)
        assert first_session.state is SessionState.CONNECTED
        assert connections == 1
        await asyncio.wait_for(first_session.detach(), timeout=1)
        await asyncio.wait_for(first_bridge.close(preserve_worlds=True), timeout=1)
        del first_client, first_bus

        second_client, second_bridge, second_session, second_bus, second_sink = (
            await gateway_session()
        )
        try:
            await second_bus.submit(
                CommandRequest(
                    session_id=second_session.session_id,
                    world="alpha",
                    actor=Actor(ActorType.HUMAN, "operator"),
                    text="look",
                )
            )
            assert await asyncio.wait_for(commands.get(), timeout=1) == b"look\r\n"
            assert connections == 1
            world_writers[0].write(b'Alice says, "still here"\n')
            await world_writers[0].drain()
            for _attempt in range(100):
                if any(
                    event.display_text and "still here" in event.display_text
                    for event in second_sink.events
                ):
                    break
                await asyncio.sleep(0.01)
            assert any(
                event.display_text and "still here" in event.display_text
                for event in second_sink.events
            )
        finally:
            await asyncio.wait_for(second_session.stop(), timeout=1)
            await asyncio.wait_for(
                second_bridge.close(preserve_worlds=False), timeout=1
            )
            del second_client
    finally:
        await asyncio.wait_for(connector.stop(), timeout=1)
        world_server.close()
        await asyncio.wait_for(world_server.wait_closed(), timeout=1)


async def test_gateway_helper_spawns_then_reattaches_to_same_connector(
    tmp_path: Path,
) -> None:
    path = _socket_path(tmp_path)
    first = await connect_or_spawn_connector(path, {}, timeout=2)
    try:
        await first.close()
        second = await connect_or_spawn_connector(path, {}, timeout=2)
        try:
            await second.shutdown()
        finally:
            await second.close()
        for _attempt in range(100):
            if not os.path.lexists(path):
                break
            import asyncio

            await asyncio.sleep(0.01)
        assert not os.path.lexists(path)
    finally:
        await first.close()


async def test_world_socket_survives_gateway_client_replacement(tmp_path: Path) -> None:
    import asyncio

    connections = 0
    world_reader: asyncio.StreamReader | None = None
    received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    async def accept_world(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal connections, world_reader
        connections += 1
        world_reader = reader
        writer.write(b"welcome\n")
        await writer.drain()
        received.set_result(await reader.readline())
        await reader.read()
        writer.close()
        await writer.wait_closed()

    world_server = await asyncio.start_server(accept_world, "127.0.0.1", 0)
    port = world_server.sockets[0].getsockname()[1]
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    worlds = {
        "alpha": {
            "host": "127.0.0.1",
            "port": port,
            "encoding": "utf-8",
            "reconnect": False,
            "tls": {"enabled": False},
            "startup_commands": [],
            "login": None,
            "idle": None,
        }
    }
    first = WorldConnectorClient(path, build_id="a" * 64, worlds=worlds)
    second = WorldConnectorClient(path, build_id="a" * 64, worlds=worlds)
    try:
        await first.connect()
        await first.start_world("alpha")
        messages = []
        while not any(message["kind"] == "data" for message in messages):
            messages.append(await first.receive())
        assert {message["kind"] for message in messages} == {"state", "data"}
        assert connections == 1

        await first.close()
        await server.wait_detached()
        assert world_reader is not None and not world_reader.at_eof()

        await second.connect()
        await second.send_world("alpha", b"look")
        assert await asyncio.wait_for(received, timeout=1) == b"look\r\n"
        assert connections == 1
    finally:
        await first.close()
        await second.close()
        await server.stop()
        world_server.close()
        await world_server.wait_closed()


async def test_idle_command_continues_while_gateway_is_detached(tmp_path: Path) -> None:
    import asyncio

    received: asyncio.Queue[bytes] = asyncio.Queue()

    async def accept_world(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while line := await reader.readline():
                await received.put(line)
        finally:
            writer.close()
            await writer.wait_closed()

    world_server = await asyncio.start_server(accept_world, "127.0.0.1", 0)
    port = world_server.sockets[0].getsockname()[1]
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    worlds = {
        "alpha": {
            "host": "127.0.0.1",
            "port": port,
            "encoding": "utf-8",
            "reconnect": False,
            "tls": {"enabled": False},
            "startup_commands": [],
            "login": None,
            "idle": {"after_seconds": 0.05, "command": "IDLE"},
        }
    }
    client = WorldConnectorClient(path, build_id="a" * 64, worlds=worlds)
    try:
        await client.connect()
        await client.start_world("alpha")
        while True:
            message = await client.receive()
            if message.get("kind") == "state" and message.get("state") == "connected":
                break
        await client.close()
        await server.wait_detached()

        assert await asyncio.wait_for(received.get(), timeout=1) == b"IDLE\r\n"
    finally:
        await client.close()
        await server.stop()
        world_server.close()
        await asyncio.wait_for(world_server.wait_closed(), timeout=1)


async def test_human_quit_does_not_reconnect_world(tmp_path: Path) -> None:
    import asyncio

    connections = 0

    async def accept_world(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal connections
        connections += 1
        await reader.readline()
        writer.close()
        await writer.wait_closed()

    world_server = await asyncio.start_server(accept_world, "127.0.0.1", 0)
    port = world_server.sockets[0].getsockname()[1]
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    worlds = {
        "alpha": {
            "host": "127.0.0.1",
            "port": port,
            "encoding": "utf-8",
            "reconnect": True,
            "tls": {"enabled": False},
            "startup_commands": [],
            "login": None,
            "idle": None,
        }
    }
    client = WorldConnectorClient(path, build_id="a" * 64, worlds=worlds)
    try:
        await client.connect()
        await client.start_world("alpha")
        while True:
            message = await client.receive()
            if message.get("kind") == "state" and message.get("state") == "connected":
                break
        await client.send_world("alpha", b"quit", quit=True)
        while True:
            message = await client.receive()
            if message.get("kind") == "state" and message.get("state") == "stopped":
                break
        await asyncio.sleep(0.05)
        assert connections == 1
    finally:
        await client.close()
        await server.stop()
        world_server.close()
        await asyncio.wait_for(world_server.wait_closed(), timeout=1)


async def test_changed_world_configuration_is_rejected_without_silent_reuse(
    tmp_path: Path,
) -> None:
    path = _socket_path(tmp_path)
    server = WorldConnectorServer(path, build_id="a" * 64, grace_seconds=1)
    await server.start()
    first = WorldConnectorClient(
        path,
        build_id="a" * 64,
        worlds={"alpha": {"host": "example.com", "port": 1}},
    )
    second = WorldConnectorClient(
        path,
        build_id="a" * 64,
        worlds={"alpha": {"host": "example.com", "port": 2}},
    )
    try:
        await first.connect()
        await first.close()
        await server.wait_detached()

        with pytest.raises(ProtocolError, match="configuration"):
            await second.connect()
    finally:
        await first.close()
        await second.close()
        await server.stop()


@pytest.mark.skipif(os.name != "posix", reason="process supervision uses POSIX signals")
async def test_tfr_gateway_supervisor_restarts_app_without_world_reconnect(
    tmp_path: Path,
) -> None:
    import asyncio

    connections = 0
    connected = asyncio.Event()

    async def accept_world(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal connections
        connections += 1
        connected.set()
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    world_server = await asyncio.start_server(accept_world, "127.0.0.1", 0)
    port = world_server.sockets[0].getsockname()[1]
    config = tmp_path / "config.jsonc"
    (tmp_path / "worlds.jsonc").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "worlds": {
                    "alpha": {
                        "host": "127.0.0.1",
                        "port": port,
                        "reconnect": False,
                        "autoconnect": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "agents.jsonc").write_text(
        '{"schema_version":1}', encoding="utf-8"
    )
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "worlds_file": "worlds.jsonc",
                "agents_file": "agents.jsonc",
                "logging": {"enabled": False},
                "updates": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    gateway_socket = _socket_path(tmp_path).with_name("gw.sock")
    connector_socket = gateway_socket.with_name("gw.sock.worlds")
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "tfr",
            "gateway",
            "--config",
            str(config),
            "--socket",
            str(gateway_socket),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    def children(parent: int) -> list[int]:
        output = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return [
            int(pid)
            for line in output.splitlines()
            if len(parts := line.split()) == 2
            for pid, ppid in [parts]
            if int(ppid) == parent
        ]

    try:
        for _attempt in range(200):
            if connected.is_set():
                break
            if process.poll() is not None:
                raise AssertionError("gateway supervisor exited early")
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("gateway supervisor did not connect its world")
        for _attempt in range(200):
            original_children = children(process.pid)
            if gateway_socket.exists() and connector_socket.exists() and original_children:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("gateway supervisor did not start its application")
        original_child = original_children[0]

        os.kill(original_child, signal.SIGKILL)

        for _attempt in range(300):
            replacement_children = children(process.pid)
            if (
                gateway_socket.exists()
                and replacement_children
                and replacement_children[0] != original_child
            ):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("gateway supervisor did not restart its application")
        assert connections == 1
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
        world_server.close()
        await asyncio.wait_for(world_server.wait_closed(), timeout=2)
        assert process.returncode == 0
