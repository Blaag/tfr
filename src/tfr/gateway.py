from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import signal
import socket
import ssl
import stat
import sys
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from tfr.agents import AgentRuntime
from tfr.config import ConfigurationBundle
from tfr.core import CommandBus, EventBus, UnknownSessionError
from tfr.eventlog import EventSink, JsonlEventSink
from tfr.events import Actor, ActorType, CommandRequest, Event
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
from tfr.plugin_sources import PluginSourceNotice, load_plugin_sources
from tfr.plugins import PluginLifecycleEvent, PluginManager, PluginWorldInfo
from tfr.sessions import SessionManager, SessionState, WorldSession
from tfr.updates import (
    BuildIdentity,
    UpdateChecker,
    UpdateResult,
    current_build,
    format_update_status,
)

MAX_COMMAND_CHARACTERS = 65_536
MAX_ACTOR_ID_CHARACTERS = 256
MAX_HELLO_BYTES = 16_384
HELLO_TIMEOUT_SECONDS = 5


@dataclass(frozen=True, slots=True)
class SequencedEvent:
    cursor: int
    event: Event


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    events: tuple[SequencedEvent, ...]
    cursor: int
    oldest_cursor: int
    truncated: bool


@dataclass(eq=False, slots=True)
class HistorySubscription:
    snapshot: HistorySnapshot
    queue: asyncio.Queue[SequencedEvent | None]


class EventHistory:
    def __init__(
        self,
        event_bus: EventBus,
        limits: Mapping[str, int],
        *,
        default_limit: int = 1_000,
        subscriber_queue_size: int = 1_000,
    ) -> None:
        if default_limit < 1 or subscriber_queue_size < 1:
            raise ValueError("history and subscriber limits must be positive")
        if any(limit < 1 for limit in limits.values()):
            raise ValueError("world history limits must be positive")
        self.event_bus = event_bus
        self.limits = dict(limits)
        self.default_limit = default_limit
        self.subscriber_queue_size = subscriber_queue_size
        self._history: dict[str, deque[SequencedEvent]] = {}
        self._subscribers: set[HistorySubscription] = set()
        self._queue: asyncio.Queue[Event] | None = None
        self._pump: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._cursor = 0
        self._dropped_through = 0

    def start(self) -> None:
        if self._pump is not None and not self._pump.done():
            return
        self._queue = self.event_bus.subscribe()
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

    async def subscribe(self, after_cursor: int | None) -> HistorySubscription:
        async with self._lock:
            if after_cursor is not None and (after_cursor < 0 or after_cursor > self._cursor):
                raise ValueError("history cursor is outside the available range")
            retained = sorted(
                (
                    item
                    for history in self._history.values()
                    for item in history
                    if after_cursor is None or item.cursor > after_cursor
                ),
                key=lambda item: item.cursor,
            )
            oldest = min(
                (item.cursor for history in self._history.values() for item in history),
                default=self._cursor + 1,
            )
            snapshot = HistorySnapshot(
                events=tuple(retained),
                cursor=self._cursor,
                oldest_cursor=oldest,
                truncated=after_cursor is not None and after_cursor < self._dropped_through,
            )
            subscription = HistorySubscription(
                snapshot=snapshot,
                queue=asyncio.Queue(maxsize=self.subscriber_queue_size),
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
                    history = self._history.get(event.world)
                    if history is None:
                        history = deque(maxlen=self.limits.get(event.world, self.default_limit))
                        self._history[event.world] = history
                    if len(history) == history.maxlen:
                        self._dropped_through = max(self._dropped_through, history[0].cursor)
                    history.append(item)
                    for subscription in tuple(self._subscribers):
                        if subscription.queue.full():
                            self._subscribers.discard(subscription)
                            self._close_subscription(subscription)
                        else:
                            subscription.queue.put_nowait(item)
            finally:
                self._queue.task_done()

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
                "server": session.config.server,
                "encoding": session.encoding,
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
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        session = self.session_for(world)
        if len(text) > MAX_COMMAND_CHARACTERS:
            raise ValueError("command text exceeds the gateway limit")
        if any(character in text for character in "\r\n\0"):
            raise ValueError("command text cannot contain CR, LF, or NUL")
        request_actor = actor or Actor(ActorType.HUMAN, f"ui:{client_id}")
        if len(request_actor.id) > MAX_ACTOR_ID_CHARACTERS:
            raise ValueError("remote actor ID exceeds the gateway limit")
        if request_actor.type not in {ActorType.HUMAN, ActorType.PLUGIN}:
            raise ValueError("remote commands must have a human or plugin actor")
        if request_actor.type is ActorType.HUMAN:
            request_actor = Actor(ActorType.HUMAN, f"ui:{client_id}")
        request = CommandRequest(
            session_id=session.session_id,
            world=world,
            actor=request_actor,
            text=text,
            request_id=request_id,
            sensitive=sensitive,
            correlation_id=correlation_id,
            causation_id=causation_id,
            metadata={**dict(metadata or {}), "gateway_connection_id": client_id},
        )
        await self.command_bus.submit(request)

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
        self._server: asyncio.Server | None = None
        self._tcp_server: asyncio.Server | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._clients: set[asyncio.Task[Any]] = set()
        self._tcp_clients: set[asyncio.Task[Any]] = set()
        self._pending_tcp_clients: dict[asyncio.Task[Any], str] = {}

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
            connection_id = str(uuid4())
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
            history_reset = requested_gateway not in {None, str(self.runtime.gateway_id)}
            subscription = await self.runtime.history.subscribe(
                None if history_reset else after_cursor
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
            sender = asyncio.create_task(
                self._send_events(subscription, writer, write_lock),
                name=f"tfr-gateway-events-{client_id}",
            )
            receiver = asyncio.create_task(
                self._receive_requests(reader, connection_id, writer, write_lock),
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
    ) -> None:
        while message := await read_message(reader):
            await self._handle_request(message, client_id, writer, write_lock)

    async def _handle_request(
        self,
        message: dict[str, Any],
        client_id: str,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
    ) -> None:
        request_id_value = message.get("request_id")
        try:
            request_id = UUID(str(request_id_value))
        except (TypeError, ValueError, AttributeError):
            raise GatewayProtocolError("request_id must be a UUID") from None
        try:
            message_type = message["type"]
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
            else:
                raise ValueError(f"unsupported request type: {message_type}")
        except (UnknownSessionError, RuntimeError, ValueError) as exc:
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
        await write_message(
            writer,
            {"type": "ack", "request_id": str(request_id), "ok": True},
            lock=write_lock,
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
    server = GatewayServer(
        runtime,
        path or default_gateway_socket(),
        tcp_host=listen_host,
        tcp_port=listen_port,
        tcp_auth_token=tcp_auth_token,
        tcp_ssl_context=tcp_ssl_context,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    try:
        await server.start(start_serving=False)
        await runtime.start()
        await server.start_serving()
        print(f"TFR Gateway listening on {server.path}", flush=True)
        for address in server.tcp_addresses:
            print(f"TFR Gateway listening with TLS on {address[0]}:{address[1]}", flush=True)
        print("Press Ctrl-C to stop the gateway.", flush=True)
        for handled_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                loop.add_signal_handler(handled_signal, stop.set)
                installed_signals.append(handled_signal)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        await stop.wait()
        return 0
    finally:
        for handled_signal in installed_signals:
            loop.remove_signal_handler(handled_signal)
        await server.stop()
        await runtime.stop()
