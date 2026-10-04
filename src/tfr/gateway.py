from __future__ import annotations

import asyncio
import contextlib
import heapq
import hmac
import os
import signal
import socket
import ssl
import stat
import sys
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID, uuid4

from tfr.agents import AgentRuntime
from tfr.config import ConfigurationBundle
from tfr.core import CommandBus, EventBus, UnknownSessionError
from tfr.eventlog import EventSink, JsonlEventSink
from tfr.events import Actor, ActorType, CommandRequest, Event, EventKind
from tfr.gateway_protocol import (
    MAX_MESSAGE_BYTES,
    MAX_SNAPSHOT_EVENTS,
    PROTOCOL_VERSION,
    GatewayProtocolError,
    encode_message,
    event_message,
    read_message,
    write_message,
)
from tfr.gateway_transport import (
    DEFAULT_GATEWAY_PORT,
    create_gateway_server_tls_context,
    load_gateway_token,
    validate_gateway_token,
    validate_tcp_endpoint,
)
from tfr.installations import InstallationError, managed_restart_command
from tfr.managed_updates import (
    StagedManagedUpdate,
    activate_managed_update,
    stage_managed_update,
)
from tfr.plugin_sources import PluginSourceNotice, PluginUpdateChecker, load_plugin_sources
from tfr.plugins import PluginLifecycleEvent, PluginManager, PluginWorldInfo
from tfr.sessions import SessionManager, SessionState, WorldSession
from tfr.updates import (
    BuildIdentity,
    UpdateChecker,
    UpdateError,
    UpdateResult,
    current_build,
    format_update_status,
)

MAX_COMMAND_CHARACTERS = 65_536
MAX_ACTOR_ID_CHARACTERS = 256
MAX_HELLO_BYTES = 16_384
HELLO_TIMEOUT_SECONDS = 5
UPDATE_PREPARE_TIMEOUT_SECONDS = 900
UPDATE_COMMIT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class SequencedEvent:
    cursor: int
    event: Event


@dataclass(slots=True)
class _GatewayUpdateParticipant:
    connection_id: str
    writer: asyncio.StreamWriter
    write_lock: asyncio.Lock
    build: BuildIdentity | None
    managed_updates: bool
    update_checks: bool
    pending: dict[UUID, asyncio.Future[dict[str, Any] | None]]


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    events: tuple[SequencedEvent, ...]
    cursor: int
    oldest_cursor: int
    truncated: bool
    available_counts: Mapping[tuple[str, int], int]
    after_cursor_gaps: frozenset[tuple[str, int]]
    resumed: bool


@dataclass(eq=False, slots=True)
class HistorySubscription:
    snapshot: HistorySnapshot
    queue: asyncio.Queue[SequencedEvent | None]
    worlds: frozenset[str] | None = None


class EventHistory:
    def __init__(
        self,
        event_bus: EventBus,
        limits: Mapping[str, int],
        *,
        default_limit: int = 1_000,
        subscriber_queue_size: int = 1_000,
        ingress_queue_size: int = 1_000,
    ) -> None:
        if default_limit < 1 or subscriber_queue_size < 1 or ingress_queue_size < 1:
            raise ValueError("history and subscriber limits must be positive")
        if any(limit < 1 for limit in limits.values()):
            raise ValueError("world history limits must be positive")
        self.event_bus = event_bus
        self.limits = dict(limits)
        self.default_limit = default_limit
        self.subscriber_queue_size = subscriber_queue_size
        self.ingress_queue_size = ingress_queue_size
        self._history: dict[str, deque[SequencedEvent]] = {}
        self._generation_counts: dict[
            str, tuple[int, Counter[tuple[EventKind, ActorType | None]]]
        ] = {}
        self._generation_dropped_through: dict[
            str, tuple[int, dict[tuple[EventKind, ActorType | None], int]]
        ] = {}
        self._subscribers: set[HistorySubscription] = set()
        self._queue: asyncio.Queue[Event] | None = None
        self._pump: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._cursor = 0
        self._dropped_through = 0

    def start(self) -> None:
        if self._pump is not None and not self._pump.done():
            return
        self._queue = self.event_bus.subscribe(
            maxsize=self.ingress_queue_size,
            backpressure=True,
        )
        self._pump = asyncio.create_task(self._run(), name="tfr-gateway-history")

    async def stop(self) -> None:
        if self._queue is not None:
            await self._queue.join()
            self.event_bus.unsubscribe(self._queue)
            self._queue = None
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None
        async with self._lock:
            for subscription in self._subscribers:
                self._close_subscription(subscription)
            self._subscribers.clear()

    async def flush(self) -> None:
        if self._queue is not None:
            await self._queue.join()

    async def subscribe(
        self,
        after_cursor: int | None,
        *,
        include_history: bool = True,
        worlds: frozenset[str] | None = None,
        maximum_events: int | None = None,
        maximum_events_per_world: int | None = None,
        event_filter: Callable[[Event], bool] | None = None,
        event_class_filter: Callable[[EventKind, ActorType | None], bool] | None = None,
        maximum_source_bytes: int | None = None,
        event_source_bytes: Callable[[Event], int] | None = None,
    ) -> HistorySubscription:
        if maximum_events is not None and maximum_events < 1:
            raise ValueError("history snapshot limit must be positive")
        if maximum_events_per_world is not None and maximum_events_per_world < 1:
            raise ValueError("per-world history snapshot limit must be positive")
        if maximum_source_bytes is not None and maximum_source_bytes < 1:
            raise ValueError("history snapshot byte limit must be positive")
        if (maximum_source_bytes is None) != (event_source_bytes is None):
            raise ValueError("history snapshot byte limit and estimator must be used together")
        async with self._lock:
            if after_cursor is not None and (after_cursor < 0 or after_cursor > self._cursor):
                raise ValueError("history cursor is outside the available range")
            candidate_count = 0
            candidate_counts: Counter[tuple[str, int]] = Counter()
            for world, history in self._history.items():
                if worlds is not None and world not in worlds:
                    continue
                for item in history:
                    if event_filter is not None and not event_filter(item.event):
                        continue
                    if after_cursor is None or item.cursor > after_cursor:
                        candidate_count += 1
                        candidate_counts[(world, item.event.connection_generation)] += 1
            available_counts = {
                (world, generation): sum(
                    count
                    for (kind, actor_type), count in counts.items()
                    if event_class_filter is None or event_class_filter(kind, actor_type)
                )
                for world, (generation, counts) in self._generation_counts.items()
                if worlds is None or world in worlds
            }
            candidates = (
                item
                for world, history in self._history.items()
                if worlds is None or world in worlds
                for item in history
                if (event_filter is None or event_filter(item.event))
                and (after_cursor is None or item.cursor > after_cursor)
            )
            if not include_history:
                retained: list[SequencedEvent] = []
                available_count = 0
            elif maximum_events_per_world is not None:
                iterators: dict[str, Iterator[SequencedEvent]] = {}
                newest: list[tuple[int, str, SequencedEvent]] = []
                for world, history in self._history.items():
                    if worlds is not None and world not in worlds:
                        continue
                    iterator = (
                        item
                        for item in reversed(history)
                        if (event_filter is None or event_filter(item.event))
                        and (after_cursor is None or item.cursor > after_cursor)
                    )
                    first = next(iterator, None)
                    if first is not None:
                        iterators[world] = iterator
                        heapq.heappush(newest, (-first.cursor, world, first))
                retained = []
                retained_by_world: Counter[str] = Counter()
                retained_source_bytes = 0
                while newest:
                    if maximum_events is not None and len(retained) >= maximum_events:
                        break
                    _cursor, world, item = heapq.heappop(newest)
                    source_bytes = event_source_bytes(item.event) if event_source_bytes else 0
                    if (
                        maximum_source_bytes is not None
                        and retained_source_bytes + source_bytes > maximum_source_bytes
                    ):
                        break
                    retained.append(item)
                    retained_source_bytes += source_bytes
                    retained_by_world[world] += 1
                    if retained_by_world[world] < maximum_events_per_world:
                        following = next(iterators[world], None)
                        if following is not None:
                            heapq.heappush(newest, (-following.cursor, world, following))
                retained.reverse()
                available_count = candidate_count
            elif maximum_events is None:
                retained = sorted(candidates, key=lambda item: item.cursor)
                available_count = candidate_count
            else:
                retained = sorted(
                    heapq.nlargest(maximum_events, candidates, key=lambda item: item.cursor),
                    key=lambda item: item.cursor,
                )
                available_count = candidate_count
            oldest = min(
                (
                    item.cursor
                    for world, history in self._history.items()
                    if worlds is None or world in worlds
                    for item in history
                ),
                default=self._cursor + 1,
            )
            retained_counts = Counter(
                (item.event.world, item.event.connection_generation) for item in retained
            )
            after_cursor_gaps = {
                key for key, count in candidate_counts.items() if count > retained_counts[key]
            }
            if after_cursor is not None:
                for world, (generation, dropped) in self._generation_dropped_through.items():
                    if worlds is not None and world not in worlds:
                        continue
                    if any(
                        cursor > after_cursor
                        and (
                            event_class_filter is None
                            or event_class_filter(kind, actor_type)
                        )
                        for (kind, actor_type), cursor in dropped.items()
                    ):
                        after_cursor_gaps.add((world, generation))
            else:
                after_cursor_gaps.clear()
            snapshot = HistorySnapshot(
                events=tuple(retained),
                cursor=self._cursor,
                oldest_cursor=oldest,
                truncated=(
                    include_history
                    and (
                        available_count > len(retained)
                        or (
                            after_cursor is not None
                            and after_cursor < self._dropped_through
                        )
                    )
                ),
                available_counts=MappingProxyType(available_counts),
                after_cursor_gaps=frozenset(after_cursor_gaps),
                resumed=after_cursor is not None,
            )
            subscription = HistorySubscription(
                snapshot=snapshot,
                queue=asyncio.Queue(maxsize=self.subscriber_queue_size),
                worlds=worlds,
            )
            self._subscribers.add(subscription)
            return subscription

    async def unsubscribe(self, subscription: HistorySubscription) -> None:
        async with self._lock:
            self._subscribers.discard(subscription)

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            try:
                async with self._lock:
                    self._cursor += 1
                    item = SequencedEvent(self._cursor, self._bounded_event(self._cursor, event))
                    bounded_event = item.event
                    generation_counts = self._generation_counts.get(bounded_event.world)
                    if (
                        generation_counts is None
                        or bounded_event.connection_generation > generation_counts[0]
                    ):
                        generation_counts = (bounded_event.connection_generation, Counter())
                        self._generation_counts[bounded_event.world] = generation_counts
                    if bounded_event.connection_generation == generation_counts[0]:
                        actor_type = (
                            bounded_event.actor.type
                            if bounded_event.actor is not None
                            else None
                        )
                        generation_counts[1][
                            (bounded_event.kind, actor_type)
                        ] += 1
                    history = self._history.get(event.world)
                    if history is None:
                        history = deque(maxlen=self.limits.get(event.world, self.default_limit))
                        self._history[event.world] = history
                    if len(history) == history.maxlen:
                        dropped_item = history[0]
                        self._dropped_through = max(self._dropped_through, history[0].cursor)
                        dropped_event = dropped_item.event
                        dropped = self._generation_dropped_through.get(dropped_event.world)
                        if dropped is None or dropped_event.connection_generation > dropped[0]:
                            dropped = (dropped_event.connection_generation, {})
                            self._generation_dropped_through[dropped_event.world] = dropped
                        if dropped_event.connection_generation == dropped[0]:
                            dropped_actor_type = (
                                dropped_event.actor.type
                                if dropped_event.actor is not None
                                else None
                            )
                            event_class = (dropped_event.kind, dropped_actor_type)
                            dropped[1][event_class] = dropped_item.cursor
                    history.append(item)
                    for subscription in tuple(self._subscribers):
                        if (
                            subscription.worlds is not None
                            and event.world not in subscription.worlds
                        ):
                            continue
                        if subscription.queue.full():
                            self._subscribers.discard(subscription)
                            self._close_subscription(subscription)
                        else:
                            subscription.queue.put_nowait(item)
            finally:
                self._queue.task_done()
            # Let active subscribers drain bursts before applying the slow-client bound.
            await asyncio.sleep(0)

    @staticmethod
    def _close_subscription(subscription: HistorySubscription) -> None:
        while not subscription.queue.empty():
            subscription.queue.get_nowait()
        subscription.queue.put_nowait(None)

    @staticmethod
    def _bounded_event(cursor: int, event: Event) -> Event:
        try:
            encode_message(event_message(cursor, event))
            return event
        except GatewayProtocolError:
            notice = "[gateway omitted oversized event content]"
            replacement = replace(
                event,
                canonical_text=notice,
                plain_text=notice,
                display_text=notice,
                spoof=None,
                metadata={"gateway_omitted": "oversized_event"},
            )
            try:
                encode_message(event_message(cursor, replacement))
                return replacement
            except GatewayProtocolError:
                return Event(
                    session_id=event.session_id,
                    world=event.world[:256],
                    connection_generation=event.connection_generation,
                    sequence=event.sequence,
                    direction=event.direction,
                    kind=event.kind,
                    event_id=event.event_id,
                    timestamp=event.timestamp,
                    monotonic_ns=event.monotonic_ns,
                    canonical_text=notice,
                    plain_text=notice,
                    display_text=notice,
                    redacted=event.redacted,
                    metadata={"gateway_omitted": "oversized_event"},
                )


def default_gateway_socket() -> Path:
    runtime_directory = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_directory:
        return Path(runtime_directory).expanduser() / "tfr" / "gateway.sock"
    return Path.home() / ".local" / "state" / "tfr" / "run" / "gateway.sock"


def _event_log_path(directory: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory.expanduser() / f"tfr-{timestamp}-{os.getpid()}.jsonl"


def scrollback_for(bundle: ConfigurationBundle, alias: str) -> int:
    world = bundle.worlds.worlds[alias]
    if world.scrollback_lines is not None:
        return world.scrollback_lines
    if "scrollback_lines" in bundle.worlds.defaults.model_fields_set:
        return bundle.worlds.defaults.scrollback_lines
    return bundle.main.ui.scrollback_lines


class GatewayRuntime:
    def __init__(
        self,
        *,
        event_bus: EventBus,
        command_bus: CommandBus,
        sessions: list[WorldSession],
        manager: SessionManager,
        plugins: PluginManager,
        agents: AgentRuntime,
        history: EventHistory,
        build: BuildIdentity | None = None,
        update_checker: UpdateChecker | None = None,
        plugin_update_checker: PluginUpdateChecker | None = None,
        plugin_source_messages: tuple[str, ...] = (),
        plugin_source_notices: tuple[PluginSourceNotice, ...] = (),
    ) -> None:
        self.event_bus = event_bus
        self.command_bus = command_bus
        self.sessions = sessions
        self.manager = manager
        self.plugins = plugins
        self.agents = agents
        self.history = history
        self.build = build or current_build()
        self.update_checker = update_checker
        self.plugin_update_checker = plugin_update_checker
        self.plugin_source_messages = plugin_source_messages
        self.plugin_source_notices = plugin_source_notices
        self.gateway_id = uuid4()
        self._started = False
        self._update_task: asyncio.Task[None] | None = None
        self._control_locks = {session.world: asyncio.Lock() for session in sessions}
        self._agent_locks = {name: asyncio.Lock() for name in agents.controllers}

    @classmethod
    async def from_configuration(
        cls,
        bundle: ConfigurationBundle,
        *,
        plugin_scope: Literal["all", "gateway"] = "gateway",
    ) -> GatewayRuntime:
        sinks: list[EventSink] = []
        if bundle.main.logging.enabled:
            sinks.append(JsonlEventSink(_event_log_path(bundle.main.logging.directory)))
        event_bus = EventBus(sinks)
        command_bus = CommandBus()
        sessions = [
            WorldSession(
                world=alias,
                config=config,
                defaults=bundle.worlds.defaults,
                event_bus=event_bus,
                command_bus=command_bus,
                show_nospoof_prefix=bundle.main.ui.show_nospoof_prefix,
            )
            for alias, config in bundle.worlds.worlds.items()
        ]
        manager = SessionManager(sessions)
        extra_plugins, plugin_source_failures, plugin_source_notices = await load_plugin_sources(
            bundle.main.plugins.sources,
            plugins_directory=bundle.main.plugins.state_directory,
        )
        for failure in plugin_source_failures:
            print(f"tfr: plugin source {failure.source_id}: {failure.error}", file=sys.stderr)
        for notice in plugin_source_notices:
            print(f"tfr: plugin source {notice.source_id}: {notice.message}", file=sys.stderr)
        plugin_source_messages = tuple(
            [
                f"Plugin source {failure.source_id}: {failure.error}"
                for failure in plugin_source_failures
            ]
            + [
                f"Plugin source {notice.source_id}: {notice.message}"
                for notice in plugin_source_notices
            ]
        )
        plugins = await PluginManager.load(
            enabled=bundle.main.plugins.enabled,
            config=bundle.main.plugins.config,
            event_bus=event_bus,
            command_bus=command_bus,
            targets={session.world: session.session_id for session in sessions},
            worlds={
                session.world: PluginWorldInfo(
                    server=session.config.server,
                    encoding=session.encoding,
                )
                for session in sessions
            },
            extra_discovered=extra_plugins,
            scope=plugin_scope,
        )
        event_bus.add_processor(plugins.process_event)
        agents = AgentRuntime.from_configuration(bundle, sessions, event_bus, command_bus)
        history = EventHistory(
            event_bus,
            {alias: scrollback_for(bundle, alias) for alias in bundle.worlds.worlds},
        )
        return cls(
            event_bus=event_bus,
            command_bus=command_bus,
            sessions=sessions,
            manager=manager,
            plugins=plugins,
            agents=agents,
            history=history,
            update_checker=(
                UpdateChecker(bundle.main.updates) if plugin_scope == "gateway" else None
            ),
            plugin_update_checker=(
                PluginUpdateChecker(
                    bundle.main.plugins.sources,
                    plugins_directory=bundle.main.plugins.state_directory,
                    config=bundle.main.updates,
                    notified_versions={
                        notice.source_id: notice.available_version
                        for notice in plugin_source_notices
                        if notice.available_version is not None
                    },
                )
                if plugin_scope == "gateway"
                else None
            ),
            plugin_source_messages=plugin_source_messages,
            plugin_source_notices=plugin_source_notices,
        )

    async def start(self) -> None:
        if self._started:
            return
        self.history.start()
        try:
            await self.plugins.lifecycle(PluginLifecycleEvent(kind="application_start"))
            self.agents.start()
            await self.manager.start_autoconnect()
        except BaseException:
            await self.stop()
            raise
        self._started = True
        if self.update_checker is not None and self.update_checker.config.enabled:
            self._update_task = asyncio.create_task(
                self.update_checker.run_periodically(self._notify_update),
                name="tfr-gateway-updates",
            )

    async def stop(self) -> None:
        if not self._started and self.history._pump is None:
            await self.event_bus.close()
            return
        self._started = False
        if self._update_task is not None:
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
            self._update_task = None
        await self.agents.stop()
        await self.manager.stop_all()
        await self.plugins.drain()
        await self.plugins.lifecycle(PluginLifecycleEvent(kind="application_stop"))
        await self.plugins.drain()
        await self.history.stop()
        await self.event_bus.close()

    def _notify_update(self, result: UpdateResult) -> None:
        print(format_update_status("Gateway", self.build, result), file=sys.stderr, flush=True)

    def world_descriptors(self) -> list[dict[str, Any]]:
        agent_worlds = {controller.session.world for controller in self.agents.controllers.values()}
        return [
            {
                "world": session.world,
                "session_id": str(session.session_id),
                "state": session.state.value,
                "connection_generation": session.connection_generation,
                "server": session.config.server,
                "encoding": session.encoding,
                "character": session.character_name,
                "capabilities": {
                    "unicode": session.config.capabilities.unicode,
                },
                "aliases": list(session.config.aliases),
                "agent": session.world in agent_worlds,
                "scrollback_lines": self.history.limits[session.world],
                "show_nospoof_prefix": session.show_nospoof_prefix,
            }
            for session in self.sessions
        ]

    def agent_descriptors(self) -> list[dict[str, Any]]:
        return [
            {
                "name": controller.name,
                "world": controller.session.world,
                "state": controller.inspection.state,
                "paused": controller.paused,
                "provider": controller.provider.name,
                "endpoint": controller.provider.endpoint,
                "model": controller.config.model,
            }
            for controller in self.agents.controllers.values()
        ]

    def session_for(self, world: str) -> WorldSession:
        try:
            return self.manager.sessions[world]
        except KeyError as exc:
            raise ValueError(f"unknown world: {world}") from exc

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
        expected_connection_generation: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        request = self._command_request(
            world=world,
            text=text,
            client_id=client_id,
            request_id=request_id,
            sensitive=sensitive,
            actor=actor,
            correlation_id=correlation_id,
            causation_id=causation_id,
            expected_connection_generation=expected_connection_generation,
            metadata=metadata,
        )
        await self.command_bus.submit(request)

    def submit_command_nowait(
        self,
        *,
        world: str,
        text: str,
        client_id: str,
        request_id: UUID,
    ) -> None:
        request = self._command_request(
            world=world,
            text=text,
            client_id=client_id,
            request_id=request_id,
        )
        self.command_bus.submit_nowait(request)

    def _command_request(
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
        expected_connection_generation: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> CommandRequest:
        session = self.session_for(world)
        if len(text) > MAX_COMMAND_CHARACTERS:
            raise ValueError("command text exceeds the gateway limit")
        if any(character in text for character in "\r\n\0"):
            raise ValueError("command text cannot contain CR, LF, or NUL")
        if (
            expected_connection_generation is not None
            and expected_connection_generation != session.connection_generation
        ):
            raise ValueError("world connection changed before command submission")
        request_actor = actor or Actor(ActorType.HUMAN, f"ui:{client_id}")
        if len(request_actor.id) > MAX_ACTOR_ID_CHARACTERS:
            raise ValueError("remote actor ID exceeds the gateway limit")
        if request_actor.type not in {ActorType.HUMAN, ActorType.PLUGIN}:
            raise ValueError("remote commands must have a human or plugin actor")
        if request_actor.type is ActorType.HUMAN:
            request_actor = Actor(ActorType.HUMAN, f"ui:{client_id}")
        return CommandRequest(
            session_id=session.session_id,
            world=world,
            actor=request_actor,
            text=text,
            request_id=request_id,
            sensitive=sensitive,
            correlation_id=correlation_id,
            causation_id=causation_id,
            expected_connection_generation=expected_connection_generation,
            metadata={**dict(metadata or {}), "gateway_connection_id": client_id},
        )

    async def control(self, *, world: str, action: str) -> None:
        session = self.session_for(world)
        async with self._control_locks[world]:
            if action == "connect":
                if session.state is not SessionState.STOPPED:
                    raise ValueError(f"world {world!r} is already {session.state.value}")
                await session.start()
            elif action == "disconnect":
                await session.stop()
            elif action == "reconnect":
                await session.stop()
                await session.start()
            else:
                raise ValueError(f"unknown control action: {action}")

    async def agent_control(self, *, name: str, action: str) -> None:
        try:
            controller = self.agents.controllers[name]
            lock = self._agent_locks[name]
        except KeyError as exc:
            raise ValueError(f"unknown agent: {name}") from exc
        async with lock:
            if action == "pause":
                await controller.pause()
            elif action == "resume":
                controller.resume()
            elif action == "trigger":
                if not controller.manual_turn():
                    raise ValueError(f"agent {name!r} cannot be triggered")
            else:
                raise ValueError(f"unknown agent control action: {action}")


class GatewayServer:
    def __init__(
        self,
        runtime: GatewayRuntime,
        path: Path | str,
        *,
        maximum_clients: int = 16,
        maximum_tcp_clients: int = 8,
        maximum_pending_tcp_clients: int = 16,
        maximum_pending_tcp_clients_per_host: int = 2,
        tcp_host: str | None = None,
        tcp_port: int = DEFAULT_GATEWAY_PORT,
        tcp_auth_token: str | None = None,
        tcp_ssl_context: ssl.SSLContext | None = None,
        pairing_url_factory: Callable[[str], str] | None = None,
        device_list_factory: Callable[[], list[dict[str, Any]]] | None = None,
        device_revoke_factory: Callable[[UUID], Awaitable[bool]] | None = None,
        update_notice: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        if (
            maximum_clients < 1
            or maximum_tcp_clients < 1
            or maximum_pending_tcp_clients < 1
            or maximum_pending_tcp_clients_per_host < 1
        ):
            raise ValueError("client limits must be positive")
        self.runtime = runtime
        self.path = Path(path).expanduser()
        self.maximum_clients = maximum_clients
        self.maximum_tcp_clients = maximum_tcp_clients
        self.maximum_pending_tcp_clients = maximum_pending_tcp_clients
        self.maximum_pending_tcp_clients_per_host = maximum_pending_tcp_clients_per_host
        if tcp_host is None:
            if tcp_auth_token is not None or tcp_ssl_context is not None:
                raise ValueError("gateway TCP authentication and TLS require a TCP host")
            self.tcp_host = None
            self.tcp_port = tcp_port
        else:
            self.tcp_host, self.tcp_port = validate_tcp_endpoint(
                tcp_host,
                tcp_port,
                allow_zero=True,
            )
            if tcp_auth_token is None:
                raise ValueError("gateway TCP listener requires an authentication token")
            if tcp_ssl_context is None:
                raise ValueError("gateway TCP listener requires TLS")
            validate_gateway_token(tcp_auth_token)
        self.tcp_auth_token = tcp_auth_token
        self.tcp_ssl_context = tcp_ssl_context
        self.pairing_url_factory = pairing_url_factory
        self.device_list_factory = device_list_factory
        self.device_revoke_factory = device_revoke_factory
        self.update_notice = update_notice
        self._server: asyncio.Server | None = None
        self._tcp_server: asyncio.Server | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._clients: set[asyncio.Task[Any]] = set()
        self._tcp_clients: set[asyncio.Task[Any]] = set()
        self._pending_tcp_clients: dict[asyncio.Task[Any], str] = {}
        self._update_participants: dict[str, _GatewayUpdateParticipant] = {}
        self._update_participants_lock = asyncio.Lock()
        self._update_lock = asyncio.Lock()
        self._accepting_update_participants = True
        self._staged_update: StagedManagedUpdate | None = None
        self._update_transaction_participants: tuple[_GatewayUpdateParticipant, ...] = ()
        self._update_gateway_restart = False
        self._update_task: asyncio.Task[None] | None = None
        self._update_restart = asyncio.Event()

    async def start(self, *, start_serving: bool = True) -> None:
        if self._server is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._validate_socket_parent()
        if os.path.lexists(self.path):
            raise RuntimeError(f"gateway socket already exists: {self.path}")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            socket_stat = self.path.stat(follow_symlinks=False)
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            os.chmod(self.path, 0o600)
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
            self._server = await asyncio.start_unix_server(
                self._accept_unix_client,
                sock=listener,
                limit=MAX_MESSAGE_BYTES,
                start_serving=start_serving,
            )
            if self.tcp_host is not None:
                self._tcp_server = await asyncio.start_server(
                    self._accept_tcp_client,
                    self.tcp_host,
                    self.tcp_port,
                    ssl=self.tcp_ssl_context,
                    ssl_handshake_timeout=5,
                    limit=MAX_MESSAGE_BYTES,
                    start_serving=start_serving,
                )
        except BaseException:
            listener.close()
            await self.stop()
            raise

    async def start_serving(self) -> None:
        if self._server is None:
            raise RuntimeError("gateway server has not been started")
        await self._server.start_serving()
        if self._tcp_server is not None:
            await self._tcp_server.start_serving()

    async def stop(self) -> None:
        if (
            self._update_task is not None
            and self._update_task is not asyncio.current_task()
            and not self._update_task.done()
        ):
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in tuple(self._clients):
            task.cancel()
        if self._clients:
            await asyncio.gather(*tuple(self._clients), return_exceptions=True)
        self._clients.clear()
        self._tcp_clients.clear()
        self._pending_tcp_clients.clear()
        self._remove_owned_socket()

    async def wait_update_restart(self) -> None:
        await self._update_restart.wait()

    def _validate_socket_parent(self) -> None:
        if os.name != "posix":
            return
        parent = self.path.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(parent.st_mode):
            raise RuntimeError(f"gateway socket parent is not a directory: {self.path.parent}")
        if parent.st_uid != os.geteuid():
            raise RuntimeError(
                f"gateway socket parent is not owned by this user: {self.path.parent}"
            )
        if stat.S_IMODE(parent.st_mode) & 0o077:
            raise RuntimeError(f"gateway socket parent must have mode 0700: {self.path.parent}")

    def _remove_owned_socket(self) -> None:
        if self._socket_identity is not None:
            with contextlib.suppress(FileNotFoundError):
                current = self.path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == self._socket_identity and stat.S_ISSOCK(
                    current.st_mode
                ):
                    self.path.unlink()
        self._socket_identity = None

    @property
    def tcp_addresses(self) -> tuple[tuple[Any, ...], ...]:
        if self._tcp_server is None:
            return ()
        return tuple(sock.getsockname() for sock in self._tcp_server.sockets or ())

    def _accept_unix_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._accept_client(reader, writer, auth_token=None)

    def _accept_tcp_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_host = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"
        if (
            len(self._pending_tcp_clients) >= self.maximum_pending_tcp_clients
            or sum(pending_host == peer_host for pending_host in self._pending_tcp_clients.values())
            >= self.maximum_pending_tcp_clients_per_host
        ):
            writer.close()
            return
        assert self.tcp_auth_token is not None
        task = self._accept_client(reader, writer, auth_token=self.tcp_auth_token)
        if task is not None:
            self._pending_tcp_clients[task] = peer_host

    def _accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        auth_token: str | None,
    ) -> asyncio.Task[Any] | None:
        unix_clients = len(self._clients) - len(self._tcp_clients) - len(self._pending_tcp_clients)
        if auth_token is None and unix_clients >= self.maximum_clients:
            writer.close()
            return None
        task = asyncio.create_task(
            self._handle_client(reader, writer, auth_token=auth_token),
            name="tfr-gateway-client",
        )
        self._clients.add(task)
        task.add_done_callback(self._client_done)
        return task

    def _client_done(self, task: asyncio.Task[Any]) -> None:
        self._clients.discard(task)
        self._tcp_clients.discard(task)
        self._pending_tcp_clients.pop(task, None)
        if not task.cancelled():
            task.exception()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        auth_token: str | None,
    ) -> None:
        subscription: HistorySubscription | None = None
        write_lock = asyncio.Lock()
        sender: asyncio.Task[None] | None = None
        receiver: asyncio.Task[None] | None = None
        participant: _GatewayUpdateParticipant | None = None
        try:
            hello = await asyncio.wait_for(
                read_message(reader, maximum_bytes=MAX_HELLO_BYTES),
                timeout=HELLO_TIMEOUT_SECONDS,
            )
            if hello is None or hello.get("type") != "hello":
                raise GatewayProtocolError("first message must be hello")
            if auth_token is not None:
                supplied_token = hello.get("auth_token")
                if not isinstance(supplied_token, str) or not hmac.compare_digest(
                    supplied_token,
                    auth_token,
                ):
                    raise GatewayProtocolError("gateway authentication failed")
                task = asyncio.current_task()
                if task is None or task not in self._pending_tcp_clients:
                    raise GatewayProtocolError("gateway authentication state is invalid")
                if len(self._tcp_clients) >= self.maximum_tcp_clients:
                    raise GatewayProtocolError("gateway remote client limit reached")
                self._pending_tcp_clients.pop(task)
                self._tcp_clients.add(task)
            client_id = self._client_id(hello.get("client_id"))
            admin = hello.get("admin", False)
            if not isinstance(admin, bool):
                raise GatewayProtocolError("admin must be a boolean")
            if admin and auth_token is not None:
                raise GatewayProtocolError("gateway administration requires the local socket")
            connection_id = str(uuid4())
            client_build: BuildIdentity | None = None
            if "build" in hello:
                try:
                    client_build = BuildIdentity.from_mapping(hello["build"])
                except UpdateError as exc:
                    raise GatewayProtocolError(f"invalid client build identity: {exc}") from exc
            managed_updates = hello.get("managed_updates", False)
            if not isinstance(managed_updates, bool):
                raise GatewayProtocolError("managed_updates must be a boolean")
            update_checks = hello.get("update_checks", False)
            if not isinstance(update_checks, bool):
                raise GatewayProtocolError("update_checks must be a boolean")
            requested_gateway = hello.get("gateway_id")
            if requested_gateway is not None and not isinstance(requested_gateway, str):
                raise GatewayProtocolError("gateway_id must be a string or null")
            after_cursor = hello.get("after_cursor")
            if after_cursor is not None and (
                not isinstance(after_cursor, int)
                or isinstance(after_cursor, bool)
                or after_cursor < 0
            ):
                raise GatewayProtocolError("after_cursor must be a non-negative integer or null")
            history_reset = (
                requested_gateway not in {None, str(self.runtime.gateway_id)}
                or (requested_gateway is None and after_cursor is not None)
            )
            subscription = await self.runtime.history.subscribe(
                None if history_reset else after_cursor,
                include_history=not admin,
            )
            snapshot = subscription.snapshot
            if len(snapshot.events) > MAX_SNAPSHOT_EVENTS:
                raise GatewayProtocolError("retained history exceeds the protocol snapshot limit")
            await write_message(
                writer,
                {
                    "type": "hello",
                    "protocol": PROTOCOL_VERSION,
                    "gateway_id": str(self.runtime.gateway_id),
                    "client_id": client_id,
                    "connection_id": connection_id,
                    "cursor": snapshot.cursor,
                    "oldest_cursor": snapshot.oldest_cursor,
                    "snapshot_count": len(snapshot.events),
                    "history_truncated": snapshot.truncated,
                    "history_reset": history_reset,
                    "worlds": self.runtime.world_descriptors(),
                    "agents": self.runtime.agent_descriptors(),
                    "build": getattr(self.runtime, "build", current_build()).as_dict(),
                },
                lock=write_lock,
            )
            for item in snapshot.events:
                await write_message(writer, event_message(item.cursor, item.event), lock=write_lock)
            if not admin:
                participant = _GatewayUpdateParticipant(
                    connection_id=connection_id,
                    writer=writer,
                    write_lock=write_lock,
                    build=client_build,
                    managed_updates=managed_updates,
                    update_checks=update_checks,
                    pending={},
                )
                async with self._update_participants_lock:
                    if not self._accepting_update_participants:
                        raise GatewayProtocolError("Gateway update in progress; retry shortly")
                    self._update_participants[connection_id] = participant
            sender = asyncio.create_task(
                self._send_events(subscription, writer, write_lock),
                name=f"tfr-gateway-events-{client_id}",
            )
            receiver = asyncio.create_task(
                self._receive_requests(
                    reader,
                    connection_id,
                    writer,
                    write_lock,
                    allow_admin=auth_token is None,
                ),
                name=f"tfr-gateway-requests-{connection_id}",
            )
            done, pending = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except (GatewayProtocolError, TimeoutError, ValueError) as exc:
            with contextlib.suppress(Exception):
                await write_message(
                    writer,
                    {"type": "error", "message": str(exc)},
                    lock=write_lock,
                )
        finally:
            if participant is not None:
                async with self._update_participants_lock:
                    self._update_participants.pop(participant.connection_id, None)
                for future in participant.pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("UI disconnected during update"))
            for task in (sender, receiver):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (sender, receiver) if task is not None),
                return_exceptions=True,
            )
            if subscription is not None:
                await self.runtime.history.unsubscribe(subscription)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _send_events(
        self,
        subscription: HistorySubscription,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
    ) -> None:
        while True:
            item = await subscription.queue.get()
            if item is None:
                raise GatewayProtocolError("event stream overflowed; reconnect for backfill")
            await write_message(writer, event_message(item.cursor, item.event), lock=write_lock)

    async def _receive_requests(
        self,
        reader: asyncio.StreamReader,
        client_id: str,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
        *,
        allow_admin: bool,
    ) -> None:
        update_requests: set[asyncio.Task[None]] = set()
        try:
            while message := await read_message(reader):
                request = self._handle_request(
                    message,
                    client_id,
                    writer,
                    write_lock,
                    allow_admin=allow_admin,
                )
                if message.get("type") != "update":
                    await request
                    continue
                task = asyncio.create_task(request, name="tfr-gateway-update-request")
                update_requests.add(task)
                task.add_done_callback(self._update_request_done)
                task.add_done_callback(update_requests.discard)
        finally:
            for task in update_requests:
                task.cancel()
            await asyncio.gather(*update_requests, return_exceptions=True)

    @staticmethod
    def _update_request_done(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _handle_request(
        self,
        message: dict[str, Any],
        client_id: str,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
        *,
        allow_admin: bool,
    ) -> None:
        request_id_value = message.get("request_id")
        result: dict[str, Any] | None = None
        start_update = False
        try:
            request_id = UUID(str(request_id_value))
        except (TypeError, ValueError, AttributeError):
            raise GatewayProtocolError("request_id must be a UUID") from None
        try:
            message_type = message["type"]
            if message_type == "ack":
                self._handle_update_ack(client_id, request_id, message)
                return
            if message_type == "command":
                world = self._world(message)
                text = message.get("text")
                sensitive = message.get("sensitive", False)
                if not isinstance(text, str) or not text:
                    raise ValueError("command text must be a non-empty string")
                if not isinstance(sensitive, bool):
                    raise ValueError("sensitive must be a boolean")
                actor_value = message.get("actor")
                if actor_value is None:
                    actor = None
                elif isinstance(actor_value, dict):
                    actor = Actor(
                        ActorType(actor_value.get("type")), str(actor_value.get("id", ""))
                    )
                else:
                    raise ValueError("actor must be an object or null")
                correlation_id = self._optional_uuid(
                    message.get("correlation_id"), "correlation_id"
                )
                causation_id = self._optional_uuid(message.get("causation_id"), "causation_id")
                expected_generation = message.get("expected_connection_generation")
                if expected_generation is not None and (
                    not isinstance(expected_generation, int)
                    or isinstance(expected_generation, bool)
                    or expected_generation < 0
                ):
                    raise ValueError(
                        "expected_connection_generation must be a non-negative integer or null"
                    )
                metadata = message.get("metadata", {})
                if not isinstance(metadata, dict):
                    raise ValueError("metadata must be an object")
                await self.runtime.submit_command(
                    world=world,
                    text=text,
                    client_id=client_id,
                    request_id=request_id,
                    sensitive=sensitive,
                    actor=actor,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    expected_connection_generation=expected_generation,
                    metadata=metadata,
                )
            elif message_type == "control":
                world = self._world(message)
                action = message.get("action")
                if not isinstance(action, str):
                    raise ValueError("control action must be a string")
                await self.runtime.control(world=world, action=action)
            elif message_type == "agent_control":
                name = message.get("agent")
                action = message.get("action")
                if not isinstance(name, str) or not name:
                    raise ValueError("agent must be a non-empty string")
                if not isinstance(action, str):
                    raise ValueError("agent control action must be a string")
                await self.runtime.agent_control(name=name, action=action)
            elif message_type == "ping":
                pass
            elif message_type == "update":
                if set(message) != {"type", "protocol", "request_id"}:
                    raise ValueError("update has invalid fields")
                staged = await self._stage_gateway_update()
                if staged is None:
                    result = {"updated": False}
                else:
                    result = {
                        "updated": True,
                        "version": staged.version,
                        "commit": staged.commit,
                        "release_url": staged.release_url,
                        "restart_required": any(
                            participant.connection_id == client_id
                            for participant in self._update_transaction_participants
                        ),
                    }
                    start_update = True
            elif message_type == "pair_device":
                if not allow_admin or self.pairing_url_factory is None:
                    raise ValueError("web device pairing is unavailable")
                if set(message) != {"type", "protocol", "request_id", "label"}:
                    raise ValueError("pair_device has invalid fields")
                label = message.get("label")
                if not isinstance(label, str):
                    raise ValueError("device label must be a string")
                result = {"pairing_url": self.pairing_url_factory(label)}
            elif message_type == "list_devices":
                if not allow_admin or self.device_list_factory is None:
                    raise ValueError("web device administration is unavailable")
                if set(message) != {"type", "protocol", "request_id"}:
                    raise ValueError("list_devices has invalid fields")
                result = {"devices": self.device_list_factory()}
            elif message_type == "revoke_device":
                if not allow_admin or self.device_revoke_factory is None:
                    raise ValueError("web device administration is unavailable")
                if set(message) != {"type", "protocol", "request_id", "device_id"}:
                    raise ValueError("revoke_device has invalid fields")
                try:
                    device_id = UUID(str(message.get("device_id")))
                except (TypeError, ValueError, AttributeError):
                    raise ValueError("device_id must be a UUID") from None
                if not await self.device_revoke_factory(device_id):
                    raise ValueError(f"unknown web device: {device_id}")
                result = {"device_id": str(device_id)}
            else:
                raise ValueError(f"unsupported request type: {message_type}")
        except (TypeError, UnknownSessionError, RuntimeError, ValueError) as exc:
            await write_message(
                writer,
                {
                    "type": "ack",
                    "request_id": str(request_id),
                    "ok": False,
                    "error": str(exc),
                },
                lock=write_lock,
            )
            return
        acknowledgement: dict[str, Any] = {
            "type": "ack",
            "request_id": str(request_id),
            "ok": True,
        }
        if result is not None:
            acknowledgement["result"] = result
        await write_message(writer, acknowledgement, lock=write_lock)
        if start_update:
            self._update_task = asyncio.create_task(
                self._coordinate_update(), name="tfr-gateway-coordinated-update"
            )

    async def _stage_gateway_update(self) -> StagedManagedUpdate | None:
        async with self._update_lock:
            if self._staged_update is not None or (
                self._update_task is not None and not self._update_task.done()
            ):
                raise ValueError("a coordinated update is already in progress")
            self._update_gateway_restart = False
            if self.runtime.update_checker is None:
                raise ValueError("stable updates are unavailable")
            async with self._update_participants_lock:
                self._accepting_update_participants = False
                participants = tuple(self._update_participants.values())
                self._update_transaction_participants = participants
            incompatible = [
                participant for participant in participants if participant.build is None
            ]
            if incompatible:
                async with self._update_participants_lock:
                    self._accepting_update_participants = True
                    self._update_transaction_participants = ()
                raise ValueError(
                    "all connected native UIs must report their build; "
                    f"{len(incompatible)} UI(s) cannot be evaluated"
                )
            try:
                update_result = await self.runtime.update_checker.check()
                if update_result.error is not None:
                    raise ValueError(f"cannot check stable TFR updates: {update_result.error}")
                manifest = update_result.manifest
                if manifest is None:
                    raise ValueError("stable TFR update check returned no release manifest")
                gateway_build = getattr(self.runtime, "build", current_build())
                gateway_core_update = update_result.available_for(gateway_build)
                participant_core_updates = {
                    participant.connection_id: update_result.available_for(participant.build)
                    for participant in participants
                    if participant.build is not None
                }
                current_participants = tuple(
                    participant
                    for participant in participants
                    if not participant_core_updates[participant.connection_id]
                )
                unsupported = [
                    participant
                    for participant in current_participants
                    if not participant.update_checks
                ]
                if unsupported:
                    raise ValueError(
                        "all current native UIs must support update availability checks; "
                        f"{len(unsupported)} UI(s) cannot be checked"
                    )
                plugin_checker = getattr(self.runtime, "plugin_update_checker", None)
                gateway_plugin_check: Awaitable[bool] | None = (
                    plugin_checker.stable_auto_update_available()
                    if plugin_checker is not None and not gateway_core_update
                    else None
                )
                participant_plugin_checks = tuple(
                    self._request_update_participant(
                        participant,
                        "update_check",
                        timeout=UPDATE_PREPARE_TIMEOUT_SECONDS,
                    )
                    for participant in current_participants
                )
                plugin_results = await asyncio.gather(
                    *(
                        ((gateway_plugin_check,) if gateway_plugin_check is not None else ())
                        + participant_plugin_checks
                    )
                )
                result_offset = 1 if gateway_plugin_check is not None else 0
                gateway_plugin_update = (
                    bool(plugin_results[0]) if gateway_plugin_check is not None else False
                )
                participant_plugin_updates = {
                    participant.connection_id: (
                        isinstance(result, dict) and result.get("available") is True
                    )
                    for participant, result in zip(
                        current_participants,
                        plugin_results[result_offset:],
                        strict=True,
                    )
                }
                selected_participants = tuple(
                    participant
                    for participant in participants
                    if participant_core_updates[participant.connection_id]
                    or participant_plugin_updates.get(participant.connection_id, False)
                )
                unmanaged = [
                    participant
                    for participant in selected_participants
                    if not participant.managed_updates
                ]
                if unmanaged:
                    raise ValueError(
                        "all connected native UIs must support managed updates when they require "
                        "an update; "
                        f"{len(unmanaged)} UI(s) cannot participate"
                    )
                update_gateway = gateway_core_update or gateway_plugin_update
                if not update_gateway and not selected_participants:
                    async with self._update_participants_lock:
                        self._accepting_update_participants = True
                        self._update_transaction_participants = ()
                    self._update_gateway_restart = False
                    return None
                staged = await stage_managed_update(
                    self.runtime.update_checker.config,
                    expected_manifest=manifest,
                )
            except BaseException:
                async with self._update_participants_lock:
                    self._accepting_update_participants = True
                    self._update_transaction_participants = ()
                raise
            incompatible = [
                participant
                for participant in selected_participants
                if participant.build is None
                or not staged.manifest.supports(participant.build)
            ]
            if incompatible:
                async with self._update_participants_lock:
                    self._accepting_update_participants = True
                    self._update_transaction_participants = ()
                raise ValueError(
                    "all connected native UIs must be protocol-compatible; "
                    f"{len(incompatible)} UI(s) cannot participate"
                )
            self._staged_update = staged
            self._update_transaction_participants = selected_participants
            self._update_gateway_restart = update_gateway
            return staged

    async def _coordinate_update(self) -> None:
        staged = self._staged_update
        if staged is None:
            return
        participants = self._update_transaction_participants
        if self.update_notice is not None:
            await self.update_notice(
                f"Gateway update {staged.version} is being prepared. {staged.release_url}"
            )
        try:
            await asyncio.gather(
                *(
                    self._request_update_participant(
                        participant,
                        "update_prepare",
                        timeout=UPDATE_PREPARE_TIMEOUT_SECONDS,
                        manifest=staged.manifest.as_dict(),
                    )
                    for participant in participants
                )
            )
        except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
            await self._broadcast_update_message(
                participants, "update_abort", error=f"Update aborted: {exc}"
            )
            if self.update_notice is not None:
                await self.update_notice(f"Gateway update aborted: {exc}")
            print(f"tfr: coordinated update aborted: {exc}", file=sys.stderr, flush=True)
            self._staged_update = None
            self._update_gateway_restart = False
            async with self._update_participants_lock:
                self._accepting_update_participants = True
                self._update_transaction_participants = ()
            return

        commit_results = await asyncio.gather(
            *(
                self._request_update_participant(
                    participant,
                    "update_commit",
                    timeout=UPDATE_COMMIT_TIMEOUT_SECONDS,
                    release_id=staged.release_id,
                )
                for participant in participants
            ),
            return_exceptions=True,
        )
        for result in commit_results:
            if isinstance(result, BaseException):
                print(
                    f"tfr: UI update commit acknowledgement failed: {result}",
                    file=sys.stderr,
                    flush=True,
                )
        if self._update_gateway_restart:
            try:
                await asyncio.to_thread(activate_managed_update, staged.release_id)
            except (InstallationError, OSError) as exc:
                print(f"tfr: Gateway update activation failed: {exc}", file=sys.stderr, flush=True)
                if self.update_notice is not None:
                    await self.update_notice(f"Gateway update activation failed: {exc}")
                async with self._update_participants_lock:
                    self._accepting_update_participants = True
                    self._update_transaction_participants = ()
                return
            print(
                f"tfr: activated Gateway {staged.version}. {staged.release_url}",
                file=sys.stderr,
                flush=True,
            )
            self._update_restart.set()
            return

        self._staged_update = None
        self._update_gateway_restart = False
        async with self._update_participants_lock:
            self._accepting_update_participants = True
            self._update_transaction_participants = ()

    async def _request_update_participant(
        self,
        participant: _GatewayUpdateParticipant,
        message_type: str,
        *,
        timeout: float,
        **values: Any,
    ) -> dict[str, Any] | None:
        request_id = uuid4()
        future: asyncio.Future[dict[str, Any] | None] = (
            asyncio.get_running_loop().create_future()
        )
        participant.pending[request_id] = future
        try:
            await write_message(
                participant.writer,
                {"type": message_type, "request_id": str(request_id), **values},
                lock=participant.write_lock,
            )
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            participant.pending.pop(request_id, None)

    def _handle_update_ack(
        self, connection_id: str, request_id: UUID, message: Mapping[str, Any]
    ) -> None:
        participant = self._update_participants.get(connection_id)
        future = participant.pending.get(request_id) if participant is not None else None
        if future is None or future.done():
            return
        if message.get("ok") is not True:
            future.set_exception(ValueError(str(message.get("error", "UI update failed"))))
            return
        result = message.get("result")
        if result is not None and not isinstance(result, dict):
            future.set_exception(ValueError("UI update result must be an object"))
            return
        future.set_result(result)

    async def _broadcast_update_message(
        self,
        participants: tuple[_GatewayUpdateParticipant, ...],
        message_type: str,
        **values: Any,
    ) -> None:
        await asyncio.gather(
            *(
                write_message(
                    participant.writer,
                    {"type": message_type, **values},
                    lock=participant.write_lock,
                )
                for participant in participants
            ),
            return_exceptions=True,
        )

    @staticmethod
    def _client_id(value: Any) -> str:
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            raise GatewayProtocolError("client_id must be a UUID") from None

    @staticmethod
    def _optional_uuid(value: Any, name: str) -> UUID | None:
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise ValueError(f"{name} must be a UUID or null") from None

    @staticmethod
    def _world(message: Mapping[str, Any]) -> str:
        world = message.get("world")
        if not isinstance(world, str) or not world:
            raise ValueError("world must be a non-empty string")
        return world


async def run_gateway(
    bundle: ConfigurationBundle,
    path: Path | str | None = None,
    *,
    listen_host: str | None = None,
    listen_port: int = DEFAULT_GATEWAY_PORT,
    token_file: Path | str | None = None,
    tls_certificate: Path | str | None = None,
    tls_private_key: Path | str | None = None,
) -> int:
    runtime = await GatewayRuntime.from_configuration(bundle)
    tcp_auth_token: str | None = None
    tcp_ssl_context: ssl.SSLContext | None = None
    if listen_host is not None:
        validate_tcp_endpoint(listen_host, listen_port)
        if token_file is None or tls_certificate is None or tls_private_key is None:
            raise ValueError("network gateway requires --token-file, --tls-cert, and --tls-key")
        tcp_auth_token = load_gateway_token(token_file)
        tcp_ssl_context = create_gateway_server_tls_context(tls_certificate, tls_private_key)
    web_server = None
    if bundle.main.web_gateway.enabled:
        from tfr.gateway_web import WebGatewayServer

        assert bundle.main.web_gateway.canonical_origin is not None
        web_server = WebGatewayServer(
            runtime,
            origin=bundle.main.web_gateway.canonical_origin,
            host=bundle.main.web_gateway.listen_host,
            port=bundle.main.web_gateway.listen_port,
            state_directory=bundle.main.web_gateway.state_directory,
            snapshot_events=bundle.main.web_gateway.snapshot_events,
        )
    server = GatewayServer(
        runtime,
        path or default_gateway_socket(),
        tcp_host=listen_host,
        tcp_port=listen_port,
        tcp_auth_token=tcp_auth_token,
        tcp_ssl_context=tcp_ssl_context,
        pairing_url_factory=(web_server.create_pairing_url if web_server is not None else None),
        device_list_factory=(web_server.device_descriptors if web_server is not None else None),
        device_revoke_factory=(web_server.revoke_device if web_server is not None else None),
        update_notice=(web_server.notify_update if web_server is not None else None),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    restart_requested = False
    try:
        await server.start(start_serving=False)
        await runtime.start()
        if web_server is not None:
            await web_server.start()
        await server.start_serving()
        print(f"TFR Gateway listening on {server.path}", flush=True)
        for address in server.tcp_addresses:
            print(f"TFR Gateway listening with TLS on {address[0]}:{address[1]}", flush=True)
        if web_server is not None:
            print(
                "TFR Web Gateway listening on "
                f"http://{web_server.host}:{web_server.port} for {web_server.origin}",
                flush=True,
            )
        print("Press Ctrl-C to stop the gateway.", flush=True)
        for handled_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                loop.add_signal_handler(handled_signal, stop.set)
                installed_signals.append(handled_signal)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        signal_stop = asyncio.create_task(stop.wait(), name="tfr-gateway-signal-stop")
        update_stop = asyncio.create_task(
            server.wait_update_restart(), name="tfr-gateway-update-stop"
        )
        done, pending = await asyncio.wait(
            {signal_stop, update_stop}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
        restart_requested = update_stop in done
    finally:
        for handled_signal in installed_signals:
            loop.remove_signal_handler(handled_signal)
        if web_server is not None:
            await web_server.stop()
        await server.stop()
        await runtime.stop()
    if restart_requested:
        restart = managed_restart_command(sys.argv[1:])
        if restart is None:
            raise RuntimeError("activated Gateway release cannot be restarted")
        os.execv(restart[0], restart)
    return 0
