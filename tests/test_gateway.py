from __future__ import annotations

import asyncio
import contextlib
import os
import stat
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tfr.core import EventBus
from tfr.events import Actor, ActorType, Direction, Event, EventKind, SpoofAssessment, SpoofStatus
from tfr.gateway import EventHistory, GatewayRuntime, GatewayServer
from tfr.gateway_protocol import MAX_MESSAGE_BYTES, encode_message, event_message, read_message
from tfr.sessions import SessionState
from tfr.updates import current_build


def make_event(
    world: str,
    sequence: int,
    *,
    generation: int = 1,
    kind: EventKind = EventKind.RAW_OUTPUT,
    actor: Actor | None = None,
) -> Event:
    return Event(
        session_id=UUID("63f755aa-e407-4f78-ae05-f9d62c23f765"),
        world=world,
        connection_generation=generation,
        sequence=sequence,
        direction=Direction.INBOUND,
        kind=kind,
        canonical_text=f"{world} {sequence}",
        actor=actor,
    )


async def test_history_assigns_global_cursors_and_reports_truncation() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 2, "beta": 2})
    history.start()
    try:
        await bus.publish(make_event("alpha", 0))
        await bus.publish(make_event("beta", 0))
        await bus.publish(make_event("alpha", 1))
        await bus.publish(make_event("alpha", 2))
        await history.flush()

        subscription = await history.subscribe(0)

        assert [item.cursor for item in subscription.snapshot.events] == [2, 3, 4]
        assert subscription.snapshot.cursor == 4
        assert subscription.snapshot.oldest_cursor == 2
        assert subscription.snapshot.truncated is True

        await bus.publish(make_event("beta", 1))
        live = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert live is not None
        assert live.cursor == 5
    finally:
        await history.stop()
        await bus.close()


async def test_history_snapshot_filters_worlds_and_bounds_before_delivery() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10, "beta": 10})
    history.start()
    try:
        for sequence in range(4):
            await bus.publish(make_event("alpha", sequence))
            await bus.publish(make_event("beta", sequence))
        await history.flush()

        subscription = await history.subscribe(
            None,
            worlds=frozenset({"alpha"}),
            maximum_events=2,
        )

        assert [item.event.world for item in subscription.snapshot.events] == ["alpha", "alpha"]
        assert [item.cursor for item in subscription.snapshot.events] == [5, 7]
        assert subscription.snapshot.truncated is True

        await bus.publish(make_event("beta", 4))
        await bus.publish(make_event("alpha", 4))
        await history.flush()
        live = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert live is not None
        assert live.event.world == "alpha"
        assert subscription.queue.empty()
    finally:
        await history.stop()
        await bus.close()


async def test_history_snapshot_filters_before_per_world_limit_and_counts_generations() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10, "beta": 10})
    history.start()
    try:
        await bus.publish(make_event("alpha", 0, generation=1))
        await bus.publish(make_event("alpha", 1, generation=1, kind=EventKind.PLUGIN))
        await bus.publish(make_event("alpha", 2, generation=2))
        await bus.publish(make_event("alpha", 3, generation=2))
        await bus.publish(make_event("beta", 0, generation=4))
        await bus.publish(make_event("beta", 1, generation=4))
        await history.flush()

        subscription = await history.subscribe(
            None,
            maximum_events_per_world=2,
            event_filter=lambda event: event.kind is not EventKind.PLUGIN,
            event_class_filter=lambda kind, _actor_type: kind is not EventKind.PLUGIN,
        )

        assert [item.cursor for item in subscription.snapshot.events] == [3, 4, 5, 6]
        assert subscription.snapshot.available_counts == {
            ("alpha", 2): 2,
            ("beta", 4): 2,
        }
        assert subscription.snapshot.truncated is True
    finally:
        await history.stop()
        await bus.close()


async def test_history_combines_per_world_and_aggregate_snapshot_limits() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10, "beta": 10})
    history.start()
    try:
        for sequence in range(4):
            await bus.publish(make_event("alpha", sequence))
            await bus.publish(make_event("beta", sequence))
        await history.flush()

        subscription = await history.subscribe(
            None,
            maximum_events=3,
            maximum_events_per_world=2,
        )

        assert len(subscription.snapshot.events) == 3
        assert Counter(item.event.world for item in subscription.snapshot.events) == {
            "alpha": 1,
            "beta": 2,
        }
        assert subscription.snapshot.truncated is True
    finally:
        await history.stop()
        await bus.close()


async def test_history_generation_counts_survive_content_retention() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 2})
    history.start()
    try:
        for sequence in range(5):
            await bus.publish(make_event("alpha", sequence))
        await history.flush()

        subscription = await history.subscribe(None)

        assert [item.event.sequence for item in subscription.snapshot.events] == [3, 4]
        assert subscription.snapshot.available_counts == {("alpha", 1): 5}
    finally:
        await history.stop()
        await bus.close()


async def test_history_marks_resume_gap_after_retained_events_are_evicted() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 2})
    history.start()
    try:
        for sequence in range(5):
            await bus.publish(make_event("alpha", sequence))
        await history.flush()

        subscription = await history.subscribe(1)

        assert [item.event.sequence for item in subscription.snapshot.events] == [3, 4]
        assert subscription.snapshot.after_cursor_gaps == frozenset({("alpha", 1)})
    finally:
        await history.stop()
        await bus.close()


async def test_history_still_disconnects_an_undrained_slow_subscriber() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10}, subscriber_queue_size=2)
    history.start()
    try:
        subscription = await history.subscribe(None)
        for sequence in range(3):
            await bus.publish(make_event("alpha", sequence))
        await history.flush()

        assert await subscription.queue.get() is None
    finally:
        await history.stop()
        await bus.close()


async def test_history_ingress_is_bounded_and_backpressures_publishers() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10}, ingress_queue_size=2)
    history.start()
    try:
        assert history._queue is not None
        history._pump.cancel()  # type: ignore[union-attr]
        with contextlib.suppress(asyncio.CancelledError):
            await history._pump

        await bus.publish(make_event("alpha", 0))
        await bus.publish(make_event("alpha", 1))
        blocked = asyncio.create_task(bus.publish(make_event("alpha", 2)))
        await asyncio.sleep(0)

        assert history._queue.qsize() == 2
        assert blocked.done() is False
        history._queue.get_nowait()
        history._queue.task_done()
        await blocked
    finally:
        if history._queue is not None:
            while not history._queue.empty():
                history._queue.get_nowait()
                history._queue.task_done()
            bus.unsubscribe(history._queue)
            history._queue = None
        history._pump = None
        await bus.close()


def test_oversized_event_fallback_is_always_protocol_safe() -> None:
    event = make_event("alpha", 0)
    event = Event(
        session_id=event.session_id,
        world=event.world,
        connection_generation=event.connection_generation,
        sequence=event.sequence,
        direction=event.direction,
        kind=event.kind,
        actor=Actor(ActorType.PLUGIN, "x" * MAX_MESSAGE_BYTES),
        canonical_text="x" * MAX_MESSAGE_BYTES,
        spoof=SpoofAssessment(
            status=SpoofStatus.SPOOFED,
            speaker="Alice",
            speaker_span=(0, 5),
        ),
    )

    bounded = EventHistory._bounded_event(1, event)

    assert bounded.canonical_text == "[gateway omitted oversized event content]"
    assert bounded.actor is None
    assert bounded.spoof is None
    encode_message(event_message(1, bounded))


class FakeRuntime:
    def __init__(self, history: EventHistory) -> None:
        self.gateway_id = UUID("92716400-4bb9-43d2-845f-b8a0e51c9994")
        self.history = history
        self.commands: list[tuple[str, str, str, UUID, bool]] = []

    def world_descriptors(self) -> list[dict[str, object]]:
        return [{"world": "alpha", "state": "connected", "agent": False}]

    def agent_descriptors(self) -> list[dict[str, object]]:
        return []

    async def submit_command(
        self,
        *,
        world: str,
        text: str,
        client_id: str,
        request_id: UUID,
        sensitive: bool = False,
        **_values: object,
    ) -> None:
        self.commands.append((world, text, client_id, request_id, sensitive))

    async def control(self, *, world: str, action: str) -> None:
        raise AssertionError(f"unexpected control request: {world} {action}")


def test_runtime_world_descriptors_include_switch_aliases() -> None:
    session = SimpleNamespace(
        world="alpha",
        session_id=uuid4(),
        state=SessionState.CONNECTED,
        connection_generation=3,
        config=SimpleNamespace(
            server="tinymux",
            aliases=("a", "main"),
            capabilities=SimpleNamespace(unicode=True),
            login=SimpleNamespace(character="Hamilton", password="never-serialize-me"),
        ),
        encoding="utf-8",
        character_name="Hamilton",
        show_nospoof_prefix=False,
    )
    runtime = GatewayRuntime(
        event_bus=EventBus(),
        command_bus=SimpleNamespace(),  # type: ignore[arg-type]
        sessions=[session],  # type: ignore[list-item]
        manager=SimpleNamespace(sessions={"alpha": session}),  # type: ignore[arg-type]
        plugins=None,  # type: ignore[arg-type]
        agents=SimpleNamespace(controllers={}),  # type: ignore[arg-type]
        history=SimpleNamespace(limits={"alpha": 100}),  # type: ignore[arg-type]
    )

    descriptor = runtime.world_descriptors()[0]
    assert descriptor["aliases"] == ["a", "main"]
    assert descriptor["connection_generation"] == 3
    assert descriptor["capabilities"] == {"unicode": True}
    assert descriptor["character"] == "Hamilton"
    assert "password" not in descriptor


async def test_server_handshake_backfill_command_ack_and_detach() -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    await bus.publish(make_event("alpha", 0))
    await history.flush()
    runtime = FakeRuntime(history)
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    server = GatewayServer(runtime, socket_path)  # type: ignore[arg-type]
    await server.start()
    client_id = uuid4()
    request_id = uuid4()
    try:
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        reader, writer = await asyncio.open_unix_connection(
            socket_path,
            limit=MAX_MESSAGE_BYTES,
        )
        writer.write(
            encode_message(
                {
                    "type": "hello",
                    "client_id": str(client_id),
                    "gateway_id": None,
                    "after_cursor": None,
                }
            )
        )
        await writer.drain()

        hello = await read_message(reader)
        backfill = await read_message(reader)
        assert hello is not None
        assert hello["type"] == "hello"
        assert hello["cursor"] == 1
        assert hello["worlds"][0]["world"] == "alpha"
        assert hello["build"]["version"] == current_build().version
        assert hello["build"]["protocol"] == 2
        assert backfill is not None
        assert backfill["type"] == "event"
        assert backfill["cursor"] == 1

        writer.write(
            encode_message(
                {
                    "type": "command",
                    "request_id": str(request_id),
                    "world": "alpha",
                    "text": "look",
                }
            )
        )
        await writer.drain()
        ack = await read_message(reader)
        assert ack is not None
        assert ack == {
            "type": "ack",
            "request_id": str(request_id),
            "ok": True,
            "protocol": 2,
        }
        assert len(runtime.commands) == 1
        assert runtime.commands[0][0:2] == ("alpha", "look")
        assert UUID(runtime.commands[0][2])
        assert runtime.commands[0][3:] == (request_id, False)

        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0)
        assert server._server is not None

        reader, writer = await asyncio.open_unix_connection(socket_path, limit=MAX_MESSAGE_BYTES)
        writer.write(
            encode_message(
                {
                    "type": "hello",
                    "client_id": str(uuid4()),
                    "gateway_id": None,
                    "after_cursor": 1,
                }
            )
        )
        await writer.drain()
        reset_hello = await read_message(reader)
        reset_backfill = await read_message(reader)
        assert reset_hello is not None
        assert reset_hello["history_reset"] is True
        assert reset_backfill is not None
        assert reset_backfill["cursor"] == 1
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
        await history.stop()
        await bus.close()
    assert not socket_path.exists()
    socket_path.parent.rmdir()


async def test_server_rejects_an_insecure_existing_parent_without_changing_it(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    server = GatewayServer(
        FakeRuntime(EventHistory(EventBus(), {"alpha": 1})),
        parent / "g.sock",
    )  # type: ignore[arg-type]

    try:
        with pytest.raises(RuntimeError, match="mode 0700"):
            await server.start()
        assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    finally:
        await server.stop()


async def test_failed_bind_does_not_unlink_another_gateway_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = Path("/tmp") / f"tfr-test-{uuid4().hex[:8]}" / "gateway.sock"
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 1})
    first = GatewayServer(FakeRuntime(history), socket_path)  # type: ignore[arg-type]
    second = GatewayServer(FakeRuntime(history), socket_path)  # type: ignore[arg-type]
    await first.start()
    monkeypatch.setattr(os.path, "lexists", lambda _path: False)
    try:
        with pytest.raises(OSError):
            await second.start()
        assert socket_path.is_socket()
    finally:
        await second.stop()
        await first.stop()
        await bus.close()
    socket_path.parent.rmdir()


async def test_gateway_rejects_multiline_commands_before_submission() -> None:
    class RecordingBus:
        def __init__(self) -> None:
            self.requests: list[object] = []

        async def submit(self, request: object) -> None:
            self.requests.append(request)

    session = SimpleNamespace(world="alpha", session_id=uuid4())
    command_bus = RecordingBus()
    runtime = GatewayRuntime(
        event_bus=EventBus(),
        command_bus=command_bus,  # type: ignore[arg-type]
        sessions=[session],  # type: ignore[list-item]
        manager=SimpleNamespace(sessions={"alpha": session}),  # type: ignore[arg-type]
        plugins=None,  # type: ignore[arg-type]
        agents=SimpleNamespace(controllers={}),  # type: ignore[arg-type]
        history=EventHistory(EventBus(), {"alpha": 1}),
    )

    with pytest.raises(ValueError, match="CR, LF, or NUL"):
        await runtime.submit_command(
            world="alpha",
            text="look\nsay hidden",
            client_id=str(uuid4()),
            request_id=uuid4(),
        )
    assert command_bus.requests == []


async def test_gateway_rejects_stale_connection_generation() -> None:
    session = SimpleNamespace(world="alpha", session_id=uuid4(), connection_generation=2)
    runtime = GatewayRuntime(
        event_bus=EventBus(),
        command_bus=SimpleNamespace(),  # type: ignore[arg-type]
        sessions=[session],  # type: ignore[list-item]
        manager=SimpleNamespace(sessions={"alpha": session}),  # type: ignore[arg-type]
        plugins=None,  # type: ignore[arg-type]
        agents=SimpleNamespace(controllers={}),  # type: ignore[arg-type]
        history=EventHistory(EventBus(), {"alpha": 1}),
    )

    with pytest.raises(ValueError, match="connection changed"):
        runtime._command_request(
            world="alpha",
            text="look",
            client_id=str(uuid4()),
            request_id=uuid4(),
            expected_connection_generation=1,
        )


async def test_gateway_preserves_sensitive_human_commands() -> None:
    class RecordingBus:
        def __init__(self) -> None:
            self.requests: list[object] = []

        async def submit(self, request: object) -> None:
            self.requests.append(request)

    session = SimpleNamespace(world="alpha", session_id=uuid4())
    command_bus = RecordingBus()
    runtime = GatewayRuntime(
        event_bus=EventBus(),
        command_bus=command_bus,  # type: ignore[arg-type]
        sessions=[session],  # type: ignore[list-item]
        manager=SimpleNamespace(sessions={"alpha": session}),  # type: ignore[arg-type]
        plugins=None,  # type: ignore[arg-type]
        agents=SimpleNamespace(controllers={}),  # type: ignore[arg-type]
        history=EventHistory(EventBus(), {"alpha": 1}),
    )

    await runtime.submit_command(
        world="alpha",
        text="custom secret command",
        client_id=str(uuid4()),
        request_id=uuid4(),
        sensitive=True,
    )

    assert command_bus.requests[0].sensitive is True  # type: ignore[union-attr]


async def test_gateway_serializes_concurrent_world_controls() -> None:
    class RacySession:
        world = "alpha"
        session_id = uuid4()

        def __init__(self) -> None:
            self.state = SessionState.STOPPED
            self.starting = False

        async def start(self) -> None:
            if self.starting:
                raise RuntimeError("concurrent start")
            self.starting = True
            await asyncio.sleep(0)
            self.state = SessionState.CONNECTED
            self.starting = False

        async def stop(self) -> None:
            self.state = SessionState.STOPPED

    session = RacySession()
    runtime = GatewayRuntime(
        event_bus=EventBus(),
        command_bus=SimpleNamespace(),  # type: ignore[arg-type]
        sessions=[session],  # type: ignore[list-item]
        manager=SimpleNamespace(sessions={"alpha": session}),  # type: ignore[arg-type]
        plugins=None,  # type: ignore[arg-type]
        agents=SimpleNamespace(controllers={}),  # type: ignore[arg-type]
        history=EventHistory(EventBus(), {"alpha": 1}),
    )

    results = await asyncio.gather(
        runtime.control(world="alpha", action="connect"),
        runtime.control(world="alpha", action="connect"),
        return_exceptions=True,
    )

    assert results[0] is None
    assert isinstance(results[1], ValueError)
    assert not isinstance(results[1], RuntimeError)
