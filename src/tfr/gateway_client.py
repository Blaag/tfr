from __future__ import annotations

import asyncio
import contextlib
import os
import ssl
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from tfr.agents import AgentInspection
from tfr.config import GatewayReconnectConfig, UiConfiguration, WorldConfig, WorldDefaults
from tfr.core import EventBus
from tfr.events import CommandRequest, Event
from tfr.gateway import default_gateway_socket
from tfr.gateway_protocol import (
    MAX_MESSAGE_BYTES,
    MAX_SNAPSHOT_EVENTS,
    GatewayProtocolError,
    event_from_message,
    read_message,
    write_message,
)
from tfr.gateway_transport import (
    DEFAULT_GATEWAY_PORT,
    create_gateway_client_tls_context,
    load_gateway_token,
    validate_gateway_client_tls_context,
    validate_gateway_token,
    validate_tcp_endpoint,
)
from tfr.plugin_sources import load_plugin_sources
from tfr.plugins import PluginLifecycleEvent, PluginManager, PluginWorldInfo
from tfr.sessions import SessionManager, SessionState

_RESTART_GATEWAY_ID = "TFR_RESTART_GATEWAY_ID"
_RESTART_CURSOR = "TFR_RESTART_CURSOR"
_RESTART_WORLD = "TFR_RESTART_WORLD"


class GatewayDisconnectedError(ConnectionError):
    pass


ReconnectFactory = Callable[[UUID, int], Awaitable["GatewayClient"]]


class RemoteWorldSession:
    def __init__(
        self,
        client: GatewayClient,
        descriptor: Mapping[str, Any],
        *,
        show_nospoof_prefix: bool,
    ) -> None:
        self._client = client
        self.world = str(descriptor["world"])
        self.session_id = UUID(str(descriptor["session_id"]))
        self.state = SessionState(str(descriptor["state"]))
        self.config = WorldConfig(
            host="gateway.invalid",
            port=1,
            server=str(descriptor["server"]),
            encoding=str(descriptor["encoding"]),
            reconnect=False,
            autoconnect=False,
        )
        self.defaults = WorldDefaults()
        configured_visibility = descriptor.get("show_nospoof_prefix", show_nospoof_prefix)
        if not isinstance(configured_visibility, bool):
            raise ValueError("show_nospoof_prefix must be a boolean")
        self.show_nospoof_prefix = configured_visibility
        scrollback_lines = descriptor.get("scrollback_lines", 20_000)
        if (
            not isinstance(scrollback_lines, int)
            or isinstance(scrollback_lines, bool)
            or scrollback_lines < 1
        ):
            raise ValueError("scrollback_lines must be a positive integer")
        self.scrollback_lines = scrollback_lines

    @property
    def encoding(self) -> str:
        assert self.config.encoding is not None
        return self.config.encoding

    async def start(self) -> None:
        await self._client.control(self.world, "connect")

    async def stop(self) -> None:
        await self._client.control(self.world, "disconnect")


class RemoteCommandBus:
    def __init__(self, client: GatewayClient) -> None:
        self.client = client

    async def submit(self, request: CommandRequest) -> None:
        await self.client.submit(request)


class RemoteAgentController:
    def __init__(
        self,
        session: RemoteWorldSession,
        descriptor: Mapping[str, Any],
    ) -> None:
        from types import SimpleNamespace

        self.name = str(descriptor["name"])
        self.session = session
        self.paused = bool(descriptor.get("paused", False))
        self.inspection = AgentInspection(state=str(descriptor.get("state", "unknown")))
        self.provider = SimpleNamespace(
            name=str(descriptor.get("provider", "unknown")),
            endpoint=str(descriptor.get("endpoint", "unknown")),
        )
        self.config = SimpleNamespace(model=str(descriptor.get("model", "unknown")))


class RemoteAgentRuntime:
    def __init__(self, client: GatewayClient, descriptors: list[Mapping[str, Any]]) -> None:
        self.client = client
        sessions = {session.world: session for session in client.sessions}
        self.controllers = {
            str(descriptor["name"]): RemoteAgentController(
                sessions[str(descriptor["world"])],
                descriptor,
            )
            for descriptor in descriptors
        }

    def for_world(self, world: str) -> RemoteAgentController | None:
        return next(
            (
                controller
                for controller in self.controllers.values()
                if controller.session.world == world
            ),
            None,
        )

    async def pause(self, name: str) -> bool:
        controller = self.controllers.get(name)
        if controller is None:
            return False
        await self.client.agent_control(name, "pause")
        controller.paused = True
        controller.inspection = AgentInspection(state="paused")
        return True

    async def resume(self, name: str) -> bool:
        controller = self.controllers.get(name)
        if controller is None:
            return False
        await self.client.agent_control(name, "resume")
        controller.paused = False
        controller.inspection = AgentInspection(state="idle")
        return True

    async def trigger(self, name: str) -> bool:
        if name not in self.controllers:
            return False
        await self.client.agent_control(name, "trigger")
        return True


class GatewayClient:
    def __init__(
        self,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_id: UUID,
        gateway_id: UUID,
        cursor: int,
        oldest_cursor: int,
        history_truncated: bool,
        history_reset: bool,
        event_bus: EventBus,
        sessions: list[RemoteWorldSession],
        initial_events: tuple[Event, ...],
        agent_worlds: set[str],
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.client_id = client_id
        self.gateway_id = gateway_id
        self.cursor = cursor
        self.oldest_cursor = oldest_cursor
        self.history_truncated = history_truncated
        self.history_reset = history_reset
        self.event_bus = event_bus
        self.sessions = sessions
        self.initial_events = initial_events
        self.agent_worlds = agent_worlds
        self.command_bus = RemoteCommandBus(self)
        self.agents: RemoteAgentRuntime | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[UUID, asyncio.Future[None]] = {}
        self._pump: asyncio.Task[None] | None = None
        self._reconnect_factory: ReconnectFactory | None = None
        self._reconnect_lock = asyncio.Lock()

    @classmethod
    async def connect(
        cls,
        path: Path | str,
        *,
        show_nospoof_prefix: bool = False,
        client_id: UUID | None = None,
        gateway_id: UUID | None = None,
        after_cursor: int | None = None,
    ) -> GatewayClient:
        resolved_client_id = client_id or uuid4()
        socket_path = Path(path).expanduser()
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(socket_path),
                limit=MAX_MESSAGE_BYTES,
            )
        except FileNotFoundError:
            raise GatewayDisconnectedError(
                f"gateway socket not found: {socket_path}; start `tfr gateway` first"
            ) from None
        except ConnectionRefusedError:
            raise GatewayDisconnectedError(
                f"gateway is not accepting connections at {socket_path}"
            ) from None
        client = await cls._connect_stream(
            reader,
            writer,
            show_nospoof_prefix=show_nospoof_prefix,
            client_id=resolved_client_id,
            gateway_id=gateway_id,
            after_cursor=after_cursor,
        )

        async def reconnect_factory(gateway: UUID, cursor: int) -> GatewayClient:
            return await cls.connect(
                socket_path,
                show_nospoof_prefix=show_nospoof_prefix,
                client_id=resolved_client_id,
                gateway_id=gateway,
                after_cursor=cursor,
            )

        client._reconnect_factory = reconnect_factory
        return client

    @classmethod
    async def connect_tcp(
        cls,
        host: str,
        port: int,
        *,
        auth_token: str,
        tls_context: ssl.SSLContext,
        server_hostname: str | None = None,
        show_nospoof_prefix: bool = False,
        client_id: UUID | None = None,
        gateway_id: UUID | None = None,
        after_cursor: int | None = None,
    ) -> GatewayClient:
        host, port = validate_tcp_endpoint(host, port)
        validate_gateway_token(auth_token)
        validate_gateway_client_tls_context(tls_context)
        resolved_client_id = client_id or uuid4()
        try:
            reader, writer = await asyncio.open_connection(
                host,
                port,
                ssl=tls_context,
                server_hostname=server_hostname or host,
                limit=MAX_MESSAGE_BYTES,
            )
        except (ConnectionError, OSError) as exc:
            raise GatewayDisconnectedError(
                f"cannot establish a verified gateway connection to {host}:{port}"
            ) from exc
        client = await cls._connect_stream(
            reader,
            writer,
            show_nospoof_prefix=show_nospoof_prefix,
            client_id=resolved_client_id,
            gateway_id=gateway_id,
            after_cursor=after_cursor,
            auth_token=auth_token,
        )

        async def reconnect_factory(gateway: UUID, cursor: int) -> GatewayClient:
            return await cls.connect_tcp(
                host,
                port,
                auth_token=auth_token,
                tls_context=tls_context,
                server_hostname=server_hostname,
                show_nospoof_prefix=show_nospoof_prefix,
                client_id=resolved_client_id,
                gateway_id=gateway,
                after_cursor=cursor,
            )

        client._reconnect_factory = reconnect_factory
        return client

    @classmethod
    async def _connect_stream(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        show_nospoof_prefix: bool,
        client_id: UUID,
        gateway_id: UUID | None,
        after_cursor: int | None,
        auth_token: str | None = None,
    ) -> GatewayClient:
        try:
            hello_request: dict[str, Any] = {
                "type": "hello",
                "client_id": str(client_id),
                "gateway_id": str(gateway_id) if gateway_id is not None else None,
                "after_cursor": after_cursor,
            }
            if auth_token is not None:
                hello_request["auth_token"] = auth_token
            await write_message(
                writer,
                hello_request,
            )
            hello = await asyncio.wait_for(read_message(reader), timeout=10)
            if hello is None:
                raise GatewayDisconnectedError("gateway disconnected during handshake")
            if hello.get("type") == "error":
                raise GatewayProtocolError(str(hello.get("message", "gateway rejected handshake")))
            if hello.get("type") != "hello":
                raise GatewayProtocolError("gateway did not send a hello response")
            gateway_uuid = UUID(str(hello["gateway_id"]))
            cursor = cls._non_negative_integer(hello.get("cursor"), "cursor")
            oldest_cursor = cls._non_negative_integer(hello.get("oldest_cursor"), "oldest_cursor")
            snapshot_count = cls._non_negative_integer(
                hello.get("snapshot_count"), "snapshot_count"
            )
            if snapshot_count > MAX_SNAPSHOT_EVENTS:
                raise GatewayProtocolError("gateway snapshot count exceeds the client limit")
            history_truncated = hello.get("history_truncated")
            history_reset = hello.get("history_reset")
            if not isinstance(history_truncated, bool) or not isinstance(history_reset, bool):
                raise GatewayProtocolError("gateway history flags must be booleans")
            worlds = hello.get("worlds")
            agents = hello.get("agents")
            if not isinstance(worlds, list) or not worlds:
                raise GatewayProtocolError("gateway hello contains no worlds")
            if not isinstance(agents, list):
                raise GatewayProtocolError("gateway agents must be a list")
            event_bus = EventBus()
            client = cls(
                reader=reader,
                writer=writer,
                client_id=client_id,
                gateway_id=gateway_uuid,
                cursor=cursor,
                oldest_cursor=oldest_cursor,
                history_truncated=history_truncated,
                history_reset=history_reset,
                event_bus=event_bus,
                sessions=[],
                initial_events=(),
                agent_worlds={
                    str(agent["world"])
                    for agent in agents
                    if isinstance(agent, dict) and "world" in agent
                },
            )
            try:
                client.sessions.extend(
                    RemoteWorldSession(
                        client,
                        descriptor,
                        show_nospoof_prefix=show_nospoof_prefix,
                    )
                    for descriptor in worlds
                    if isinstance(descriptor, dict)
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise GatewayProtocolError(f"invalid world descriptor: {exc}") from exc
            if len(client.sessions) != len(worlds):
                raise GatewayProtocolError("invalid world descriptor")
            try:
                client.agents = RemoteAgentRuntime(
                    client,
                    [descriptor for descriptor in agents if isinstance(descriptor, dict)],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise GatewayProtocolError(f"invalid agent descriptor: {exc}") from exc
            client.initial_events = await asyncio.wait_for(
                cls._read_snapshot(reader, snapshot_count, cursor),
                timeout=30,
            )
            return client
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise

    def start(self) -> None:
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._run(), name="tfr-gateway-client-events")

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None
        self._fail_pending(GatewayDisconnectedError("gateway client stopped"))
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()

    async def reconnect(self) -> int:
        if self._reconnect_factory is None:
            raise GatewayDisconnectedError("gateway connection cannot be re-established")
        async with self._reconnect_lock:
            if self._pump is not None and not self._pump.done():
                self._pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._pump
            self._pump = None
            self._fail_pending(GatewayDisconnectedError("gateway connection interrupted"))
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()

            replacement = await self._reconnect_factory(self.gateway_id, self.cursor)
            current_sessions = {session.world: session for session in self.sessions}
            replacement_sessions = {session.world: session for session in replacement.sessions}
            session_identity_changed = (
                current_sessions.keys() != replacement_sessions.keys()
                or any(
                    current_sessions[world].session_id != replacement_sessions[world].session_id
                    for world in current_sessions.keys() & replacement_sessions.keys()
                )
            )
            current_agents = set(self.agents.controllers) if self.agents is not None else set()
            replacement_agents = (
                set(replacement.agents.controllers) if replacement.agents is not None else set()
            )
            if (
                replacement.history_reset
                or session_identity_changed
                or current_agents != replacement_agents
            ):
                await replacement.stop()
                await replacement.event_bus.close()
                raise GatewayDisconnectedError(
                    "gateway state changed; use /reload to rebuild the UI"
                )

            for world, session in current_sessions.items():
                updated = replacement_sessions[world]
                session.state = updated.state
                session.config = updated.config
                session.defaults = updated.defaults
                session.scrollback_lines = updated.scrollback_lines
            if self.agents is not None and replacement.agents is not None:
                for name, controller in self.agents.controllers.items():
                    updated = replacement.agents.controllers[name]
                    controller.paused = updated.paused
                    controller.inspection = updated.inspection

            self.reader = replacement.reader
            self.writer = replacement.writer
            self.gateway_id = replacement.gateway_id
            self.cursor = replacement.cursor
            self.oldest_cursor = replacement.oldest_cursor
            self.history_truncated = replacement.history_truncated
            self.history_reset = False
            self.initial_events = replacement.initial_events
            self.agent_worlds = replacement.agent_worlds
            self._write_lock = asyncio.Lock()
            await replacement.event_bus.close()
            for event in self.initial_events:
                self._update_session(event)
                await self.event_bus.publish(event)
            self.start()
            return len(self.initial_events)

    async def submit(self, request: CommandRequest) -> None:
        session = self._session_for(request.world)
        if request.session_id != session.session_id:
            raise ValueError("command request does not belong to the remote session")
        await self._request(
            {
                "type": "command",
                "request_id": str(request.request_id),
                "world": request.world,
                "text": request.text,
                "sensitive": request.sensitive,
                "actor": {"type": request.actor.type.value, "id": request.actor.id},
                "correlation_id": (
                    str(request.correlation_id) if request.correlation_id is not None else None
                ),
                "causation_id": (
                    str(request.causation_id) if request.causation_id is not None else None
                ),
                "metadata": dict(request.metadata),
            },
            request.request_id,
        )

    async def control(self, world: str, action: str) -> None:
        request_id = uuid4()
        await self._request(
            {
                "type": "control",
                "request_id": str(request_id),
                "world": world,
                "action": action,
            },
            request_id,
        )

    async def agent_control(self, name: str, action: str) -> None:
        request_id = uuid4()
        await self._request(
            {
                "type": "agent_control",
                "request_id": str(request_id),
                "agent": name,
                "action": action,
            },
            request_id,
        )

    async def ping(self) -> None:
        request_id = uuid4()
        await self._request({"type": "ping", "request_id": str(request_id)}, request_id)

    async def verify_connection(self, *, timeout: float) -> bool:
        """Actively confirm the connection is alive with a bounded ping round trip.

        Unlike waiting for a read error, this detects a connection left
        silently stale (for example, after the UI machine sleeps and wakes
        with a socket that never reports an error on its own).
        """
        if self._pump is None or self._pump.done():
            return False
        try:
            await asyncio.wait_for(self.ping(), timeout=timeout)
        except (TimeoutError, ConnectionError, OSError, GatewayProtocolError, ValueError):
            return False
        return True

    async def _request(self, message: dict[str, Any], request_id: UUID) -> None:
        if self._pump is None or self._pump.done():
            raise GatewayDisconnectedError("gateway client is not running")
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await write_message(self.writer, message, lock=self._write_lock)
            await future
        finally:
            self._pending.pop(request_id, None)

    async def _run(self) -> None:
        error: Exception = GatewayDisconnectedError("gateway disconnected")
        try:
            while message := await read_message(self.reader):
                message_type = message["type"]
                if message_type == "event":
                    cursor, event = event_from_message(message)
                    if cursor <= self.cursor:
                        continue
                    if cursor != self.cursor + 1:
                        raise GatewayProtocolError(
                            f"gateway event cursor jumped from {self.cursor} to {cursor}"
                        )
                    self.cursor = cursor
                    self._update_session(event)
                    await self.event_bus.publish(event)
                elif message_type == "ack":
                    self._handle_ack(message)
                elif message_type == "error":
                    raise GatewayProtocolError(str(message.get("message", "gateway error")))
                else:
                    raise GatewayProtocolError(f"unexpected gateway message: {message_type}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            self._fail_pending(error)

    def _handle_ack(self, message: Mapping[str, Any]) -> None:
        try:
            request_id = UUID(str(message["request_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayProtocolError("gateway acknowledgement has an invalid request_id") from exc
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if message.get("ok") is True:
            future.set_result(None)
        else:
            future.set_exception(ValueError(str(message.get("error", "gateway request failed"))))

    def _update_session(self, event: Event) -> None:
        if event.kind.value != "connection":
            return
        state = event.metadata.get("state")
        with contextlib.suppress(ValueError, KeyError):
            self._session_for(event.world).state = SessionState(str(state))

    def _session_for(self, world: str) -> RemoteWorldSession:
        try:
            return next(session for session in self.sessions if session.world == world)
        except StopIteration as exc:
            raise ValueError(f"unknown world: {world}") from exc

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    @staticmethod
    def _non_negative_integer(value: Any, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise GatewayProtocolError(f"gateway {name} must be a non-negative integer")
        return value

    @staticmethod
    async def _read_snapshot(
        reader: asyncio.StreamReader,
        count: int,
        maximum_cursor: int,
    ) -> tuple[Event, ...]:
        events: list[Event] = []
        previous_cursor = 0
        for _ in range(count):
            message = await read_message(reader)
            if message is None:
                raise GatewayDisconnectedError("gateway disconnected during history snapshot")
            event_cursor, event = event_from_message(message)
            if event_cursor <= previous_cursor or event_cursor > maximum_cursor:
                raise GatewayProtocolError("snapshot event cursors are not ordered")
            previous_cursor = event_cursor
            events.append(event)
        return tuple(events)


class GatewayUiRuntime:
    def __init__(
        self,
        client: GatewayClient,
        plugins: PluginManager,
        *,
        reconnect_config: GatewayReconnectConfig | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.plugins = plugins
        self.reconnect_config = reconnect_config or GatewayReconnectConfig()
        self.notify = notify or (lambda _text: None)
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        await self.plugins.lifecycle(PluginLifecycleEvent(kind="application_start"))
        self.client.start()
        if self.reconnect_config.enabled:
            self._supervisor_task = asyncio.create_task(
                self._supervise(), name="tfr-gateway-reconnect-supervisor"
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor_task
            self._supervisor_task = None
        await self.plugins.lifecycle(PluginLifecycleEvent(kind="application_stop"))
        await self.plugins.drain()
        await self.client.stop()
        await self.client.event_bus.close()

    async def reconnect(self) -> str:
        restored = await self.client.reconnect()
        return self._format_reconnect_message(restored)

    def _format_reconnect_message(self, restored: int) -> str:
        if self.client.history_truncated:
            return (
                f"Reconnected to Gateway; restored {restored} retained events after a history gap"
            )
        if restored:
            return f"Reconnected to Gateway; restored {restored} missed events"
        return "Reconnected to Gateway"

    async def _supervise(self) -> None:
        """Periodically verify the Gateway connection and recover from silence.

        A read error alone cannot detect a connection left stale by the UI
        machine sleeping and waking: the underlying socket may never report
        an error, so `_run`'s read loop would simply hang. This task instead
        actively pings the Gateway on a fixed interval; a failed or timed-out
        ping is treated the same as a hard disconnect and triggers a bounded
        automatic reconnect, reusing the same `GatewayClient.reconnect()`
        logic `/gateway reconnect` already uses.
        """
        config = self.reconnect_config
        try:
            while True:
                await asyncio.sleep(config.heartbeat_seconds)
                if self._stopping:
                    return
                alive = await self.client.verify_connection(timeout=config.ping_timeout_seconds)
                if alive or self._stopping:
                    continue
                await self._recover_connection()
        except asyncio.CancelledError:
            pass

    async def _recover_connection(self) -> None:
        config = self.reconnect_config
        for attempt in range(1, config.max_attempts + 1):
            if self._stopping:
                return
            self.notify(
                f"Gateway connection lost; reconnecting (attempt "
                f"{attempt}/{config.max_attempts})..."
            )
            try:
                restored = await self.client.reconnect()
            except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                if attempt == config.max_attempts:
                    self.notify(
                        f"Gateway reconnect failed after {config.max_attempts} attempts: {exc}. "
                        "Use /gateway reconnect to retry manually."
                    )
                    return
                await asyncio.sleep(config.retry_interval_seconds)
            else:
                self.notify(self._format_reconnect_message(restored))
                return


async def run_gateway_ui(
    configuration: UiConfiguration,
    path: Path | str | None = None,
    *,
    gateway_host: str | None = None,
    gateway_port: int = DEFAULT_GATEWAY_PORT,
    token_file: Path | str | None = None,
    tls_ca: Path | str | None = None,
    tls_server_name: str | None = None,
) -> int:
    from tfr.tui import TfrTui

    resume_gateway_id, resume_world = _restart_state_from_environment()
    connection_options = {
        "show_nospoof_prefix": configuration.main.ui.show_nospoof_prefix,
        "gateway_id": resume_gateway_id,
        # A replacement UI needs retained history to rebuild its in-memory display.
        "after_cursor": None,
    }
    if gateway_host is None:
        client = await GatewayClient.connect(
            path or default_gateway_socket(),
            **connection_options,
        )
    else:
        validate_tcp_endpoint(gateway_host, gateway_port)
        if token_file is None:
            raise ValueError("network gateway connection requires --token-file")
        client = await GatewayClient.connect_tcp(
            gateway_host,
            gateway_port,
            auth_token=load_gateway_token(token_file),
            tls_context=create_gateway_client_tls_context(tls_ca),
            server_hostname=tls_server_name,
            **connection_options,
        )
    manager = SessionManager(client.sessions)  # type: ignore[arg-type]
    extra_plugins, plugin_source_failures = await load_plugin_sources(
        configuration.main.plugins.sources,
        plugins_directory=configuration.main.plugins.state_directory,
    )
    for failure in plugin_source_failures:
        print(f"tfr: plugin source {failure.repo}: {failure.error}", file=sys.stderr)
    plugins = await PluginManager.load(
        enabled=configuration.main.plugins.enabled,
        config=configuration.main.plugins.config,
        event_bus=client.event_bus,
        command_bus=client.command_bus,  # type: ignore[arg-type]
        targets={session.world: session.session_id for session in client.sessions},
        worlds={
            session.world: PluginWorldInfo(
                server=session.config.server,
                encoding=session.encoding,
            )
            for session in client.sessions
        },
        extra_discovered=extra_plugins,
        scope="ui",
    )
    runtime = GatewayUiRuntime(
        client,
        plugins,
        reconnect_config=configuration.main.ui.gateway_reconnect,
    )
    tui = TfrTui(
        sessions=client.sessions,  # type: ignore[arg-type]
        manager=manager,
        event_bus=client.event_bus,
        command_bus=client.command_bus,  # type: ignore[arg-type]
        scrollback_lines={session.world: session.scrollback_lines for session in client.sessions},
        agent_worlds=client.agent_worlds,
        pager_enabled=configuration.main.ui.pager.enabled,
        pager_overlap=configuration.main.ui.pager.overlap_lines,
        recent_input_lines=configuration.main.ui.recent_input_lines,
        animations_enabled=configuration.main.ui.animations_enabled,
        low_bandwidth=configuration.main.ui.low_bandwidth,
        output_color=configuration.main.ui.output_color,
        screen_clear_mode=configuration.main.ui.screen_clear.mode,
        screen_clear_effect=configuration.main.ui.screen_clear.effect,
        plugins=plugins,
        agents=client.agents,  # type: ignore[arg-type]
        service_runtime=runtime,
        gateway_reconnect=runtime.reconnect,
        restart_supported=True,
        initial_events=client.initial_events,
        initial_scroll_to_end=True,
    )
    runtime.notify = lambda text: tui.add_notice(tui.active_alias, text)
    if resume_world in tui.views:
        tui.switch_world(resume_world)
    if client.history_reset:
        tui.add_notice(tui.active_alias, "Gateway restarted; loaded retained history")
    elif client.history_truncated:
        tui.add_notice(tui.active_alias, "Earlier gateway history is no longer retained")
    result = await tui.run()
    if tui.restart_requested:
        os.environ[_RESTART_GATEWAY_ID] = str(client.gateway_id)
        os.environ[_RESTART_WORLD] = tui.active_alias
        os.execv(sys.executable, [sys.executable, "-m", "tfr", *sys.argv[1:]])
    return result


def _restart_state_from_environment() -> tuple[UUID | None, str | None]:
    gateway_value = os.environ.pop(_RESTART_GATEWAY_ID, None)
    os.environ.pop(_RESTART_CURSOR, None)
    world = os.environ.pop(_RESTART_WORLD, None)
    if gateway_value is None:
        return None, None
    try:
        gateway_id = UUID(gateway_value)
    except ValueError:
        return None, None
    return gateway_id, world
