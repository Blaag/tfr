from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from uuid import UUID

from tfr.eventlog import EventSink
from tfr.events import CommandRequest, Event


class UnknownSessionError(LookupError):
    pass


class DuplicateSessionError(ValueError):
    pass


class EventBus:
    def __init__(
        self,
        sinks: Iterable[EventSink] = (),
        processors: Iterable[Callable[[Event], Awaitable[Event]]] = (),
    ) -> None:
        self._sinks = tuple(sinks)
        self._processors = list(processors)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._publish_lock = asyncio.Lock()
        self._closed = False

    def subscribe(self, *, maxsize: int = 0) -> asyncio.Queue[Event]:
        if self._closed:
            raise RuntimeError("event bus is closed")
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def add_processor(self, processor: Callable[[Event], Awaitable[Event]]) -> None:
        if self._closed:
            raise RuntimeError("event bus is closed")
        self._processors.append(processor)

    async def publish(self, event: Event) -> None:
        for processor in self._processors:
            event = await processor(event)
        async with self._publish_lock:
            if self._closed:
                raise RuntimeError("event bus is closed")
            for sink in self._sinks:
                await sink.write(event)
            for queue in tuple(self._subscribers):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(event)

    async def close(self) -> None:
        async with self._publish_lock:
            if self._closed:
                return
            self._closed = True
            for sink in self._sinks:
                await sink.close()
            self._subscribers.clear()


class CommandBus:
    def __init__(self) -> None:
        self._queues: dict[UUID, asyncio.Queue[CommandRequest]] = {}

    def register(self, session_id: UUID, *, maxsize: int = 0) -> asyncio.Queue[CommandRequest]:
        if session_id in self._queues:
            raise DuplicateSessionError(f"session {session_id} is already registered")
        queue: asyncio.Queue[CommandRequest] = asyncio.Queue(maxsize=maxsize)
        self._queues[session_id] = queue
        return queue

    def unregister(self, session_id: UUID) -> None:
        self._queues.pop(session_id, None)

    async def submit(self, request: CommandRequest) -> None:
        try:
            queue = self._queues[request.session_id]
        except KeyError as exc:
            raise UnknownSessionError(f"session {request.session_id} is not registered") from exc
        await queue.put(request)
