from __future__ import annotations

from uuid import uuid4

import pytest

from tfr.core import CommandBus, DuplicateSessionError, EventBus, UnknownSessionError
from tfr.events import Actor, ActorType, CommandRequest, Direction, Event, EventKind


class MemorySink:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.closed = False

    async def write(self, event: Event) -> None:
        self.events.append(event)

    async def close(self) -> None:
        self.closed = True


def make_event(sequence: int) -> Event:
    return Event(
        session_id=uuid4(),
        world="example",
        connection_generation=0,
        sequence=sequence,
        direction=Direction.INTERNAL,
        kind=EventKind.SYSTEM,
    )


async def test_event_bus_persists_before_publishing() -> None:
    sink = MemorySink()
    bus = EventBus([sink])
    queue = bus.subscribe()
    event = make_event(1)

    await bus.publish(event)

    assert sink.events == [event]
    assert await queue.get() is event
    await bus.close()
    assert sink.closed is True


async def test_bounded_subscriber_drops_oldest_event() -> None:
    bus = EventBus()
    queue = bus.subscribe(maxsize=1)
    first = make_event(1)
    second = make_event(2)

    await bus.publish(first)
    await bus.publish(second)

    assert await queue.get() is second


async def test_command_bus_routes_by_session() -> None:
    bus = CommandBus()
    session_id = uuid4()
    queue = bus.register(session_id)
    request = CommandRequest(
        session_id=session_id,
        world="example",
        actor=Actor(ActorType.HUMAN, "operator"),
        text="look",
    )

    await bus.submit(request)

    assert await queue.get() is request


async def test_command_bus_rejects_unknown_session() -> None:
    bus = CommandBus()
    request = CommandRequest(
        session_id=uuid4(),
        world="example",
        actor=Actor(ActorType.HUMAN, "operator"),
        text="look",
    )

    with pytest.raises(UnknownSessionError):
        await bus.submit(request)


def test_command_bus_rejects_duplicate_registration() -> None:
    bus = CommandBus()
    session_id = uuid4()
    bus.register(session_id)

    with pytest.raises(DuplicateSessionError):
        bus.register(session_id)
