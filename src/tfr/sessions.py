from __future__ import annotations

import asyncio
import codecs
import contextlib
import ssl
import time
from collections.abc import Sequence
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from tfr.adapters import ServerAdapter, adapter_for
from tfr.config import TlsConfig, WorldConfig, WorldDefaults
from tfr.core import CommandBus, EventBus
from tfr.events import (
    Actor,
    ActorType,
    CommandRequest,
    Confidence,
    Direction,
    Event,
    EventKind,
    Provenance,
    outbound_audit_event,
)
from tfr.telnet import TelnetCodec, TelnetEvent, escape_iac


class SessionState(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECT_WAIT = "reconnect_wait"
    DISCONNECTED = "disconnected"


class ConnectionEnded(ConnectionError):
    pass


class TextFramer:
    """Frames decoded text on line endings while retaining prompt fragments."""

    def __init__(self, *, maximum_frame_characters: int = 16_384) -> None:
        if maximum_frame_characters < 1:
            raise ValueError("maximum frame size must be positive")
        self.maximum_frame_characters = maximum_frame_characters
        self._pending = ""

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def feed(self, text: str) -> tuple[str, ...]:
        self._pending += text
        frames: list[str] = []
        start = 0
        index = 0
        while index < len(self._pending):
            if index - start >= self.maximum_frame_characters:
                frames.append(self._pending[start:index])
                start = index
            character = self._pending[index]
            if character == "\n":
                frames.append(self._pending[start : index + 1])
                start = index + 1
            elif character == "\r":
                if index + 1 == len(self._pending):
                    break
                end = index + 2 if self._pending[index + 1] == "\n" else index + 1
                frames.append(self._pending[start:end])
                start = end
                index = end - 1
            index += 1
        self._pending = self._pending[start:]
        return tuple(frames)

    def flush(self) -> str | None:
        if not self._pending:
            return None
        pending = self._pending
        self._pending = ""
        return pending


def create_tls_context(config: TlsConfig) -> ssl.SSLContext | None:
    if not config.enabled:
        return None
    if config.verify:
        ca_file = str(config.ca_file) if config.ca_file is not None else None
        return ssl.create_default_context(cafile=ca_file)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class WorldSession:
    def __init__(
        self,
        *,
        world: str,
        config: WorldConfig,
        defaults: WorldDefaults,
        event_bus: EventBus,
        command_bus: CommandBus,
        session_id: UUID | None = None,
        reconnect_delay: float = 2.0,
        prompt_flush_seconds: float = 0.05,
        show_nospoof_prefix: bool = False,
        adapter: ServerAdapter | None = None,
        command_queue_size: int = 1_000,
    ) -> None:
        if reconnect_delay < 0:
            raise ValueError("reconnect delay cannot be negative")
        if prompt_flush_seconds <= 0:
            raise ValueError("prompt flush interval must be positive")
        if command_queue_size < 1:
            raise ValueError("command queue size must be positive")
        self.world = world
        self.config = config
        self.defaults = defaults
        self.event_bus = event_bus
        self.command_bus = command_bus
        self.session_id = session_id or uuid4()
        self.reconnect_delay = reconnect_delay
        self.prompt_flush_seconds = prompt_flush_seconds
        self.command_queue_size = command_queue_size
        self.show_nospoof_prefix = (
            config.provenance.show_prefix
            if config.provenance.show_prefix is not None
            else show_nospoof_prefix
        )
        self.adapter = adapter or adapter_for(config.server)
        self.state = SessionState.STOPPED
        self.connection_generation = 0
        self._sequence = 0
        self._event_lock = asyncio.Lock()
        self._wire_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._command_queue: asyncio.Queue[CommandRequest] | None = None
        self._runner: asyncio.Task[None] | None = None
        self._last_command_activity = 0.0
        self._quit_requested = False

    @property
    def encoding(self) -> str:
        return self.config.encoding or self.defaults.encoding

    @property
    def reconnect(self) -> bool:
        if self.config.reconnect is None:
            return self.defaults.reconnect
        return self.config.reconnect

    async def start(self) -> None:
        if self._runner is not None and not self._runner.done():
            raise RuntimeError(f"world {self.world!r} is already running")
        self._stop_event.clear()
        self._quit_requested = False
        self._command_queue = self.command_bus.register(
            self.session_id,
            maxsize=self.command_queue_size,
        )
        self._runner = asyncio.create_task(self._run(), name=f"tfr-world-{self.world}")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._runner is None:
            return
        self._runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._runner
        self._runner = None

    async def wait_connected(self, *, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)

    async def wait_closed(self, *, timeout: float = 5.0) -> None:
        if self._runner is not None:
            await asyncio.wait_for(asyncio.shield(self._runner), timeout=timeout)

    async def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._set_state(SessionState.CONNECTING)
                error: Exception | None = None
                try:
                    await self._connect_and_run()
                    if not self._quit_requested:
                        error = ConnectionEnded("remote closed the connection")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = exc
                finally:
                    self._connected_event.clear()

                await self._set_state(
                    SessionState.DISCONNECTED,
                    error_type=type(error).__name__ if error else None,
                    error=str(error) if error else None,
                    intentional=self._quit_requested,
                )
                if self._stop_event.is_set() or self._quit_requested or not self.reconnect:
                    break

                await self._set_state(
                    SessionState.RECONNECT_WAIT,
                    delay_seconds=self.reconnect_delay,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.reconnect_delay)
        finally:
            self._connected_event.clear()
            self.command_bus.unregister(self.session_id)
            await self._set_state(SessionState.STOPPED)

    async def _connect_and_run(self) -> None:
        tls_context = create_tls_context(self.config.tls)
        connect_options: dict[str, Any] = {}
        if tls_context is not None:
            connect_options["ssl"] = tls_context
            connect_options["server_hostname"] = self.config.tls.server_hostname or self.config.host

        reader, writer = await asyncio.open_connection(
            self.config.host,
            self.config.port,
            **connect_options,
        )
        self.connection_generation += 1
        self._connected_event.set()
        self._last_command_activity = asyncio.get_running_loop().time()
        await self._set_state(
            SessionState.CONNECTED,
            tls=self.config.tls.enabled,
            tls_verified=self.config.tls.enabled and self.config.tls.verify,
        )

        codec = TelnetCodec(encoding=self.encoding)
        try:
            await self._send_startup(writer)
            tasks = {
                asyncio.create_task(
                    self._read_loop(reader, writer, codec),
                    name=f"tfr-reader-{self.world}",
                ),
                asyncio.create_task(
                    self._write_loop(writer),
                    name=f"tfr-writer-{self.world}",
                ),
            }
            if self.config.idle is not None:
                tasks.add(
                    asyncio.create_task(
                        self._idle_loop(),
                        name=f"tfr-idle-{self.world}",
                    )
                )
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exception = task.exception()
                if exception is not None:
                    raise exception
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        codec: TelnetCodec,
    ) -> None:
        decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        framer = TextFramer()
        while True:
            try:
                if framer.has_pending:
                    chunk = await asyncio.wait_for(
                        reader.read(65_536), timeout=self.prompt_flush_seconds
                    )
                else:
                    chunk = await reader.read(65_536)
            except TimeoutError:
                pending = framer.flush()
                if pending is not None:
                    await self._publish_inbound(pending)
                continue

            if not chunk:
                final_text = decoder.decode(b"", final=True)
                for frame in framer.feed(final_text):
                    await self._publish_inbound(frame)
                pending = framer.flush()
                if pending is not None:
                    await self._publish_inbound(pending)
                return

            result = codec.feed(chunk)
            if result.responses:
                async with self._wire_lock:
                    for response in result.responses:
                        writer.write(response)
                    await writer.drain()
            for telnet_event in result.events:
                await self._publish_telnet(telnet_event)
            if result.data:
                text = decoder.decode(result.data, final=False)
                for frame in framer.feed(text):
                    await self._publish_inbound(frame)

    async def _write_loop(self, writer: asyncio.StreamWriter) -> None:
        assert self._command_queue is not None
        while True:
            request = await self._command_queue.get()
            if (
                request.expected_connection_generation is not None
                and request.expected_connection_generation != self.connection_generation
            ):
                continue
            await self._send_command(writer, request)
            if self._quit_requested:
                return

    async def _idle_loop(self) -> None:
        assert self.config.idle is not None
        interval = float(self.config.idle.after_seconds)
        while True:
            remaining = interval - (asyncio.get_running_loop().time() - self._last_command_activity)
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            request = CommandRequest(
                session_id=self.session_id,
                world=self.world,
                actor=Actor(ActorType.IDLE, "idle-command"),
                text=self.config.idle.command,
            )
            await self.command_bus.submit(request)
            self._last_command_activity = asyncio.get_running_loop().time()

    async def _send_startup(self, writer: asyncio.StreamWriter) -> None:
        if self.config.login is not None:
            character = self.config.login.character
            if any(character.isspace() for character in character):
                character = character.replace("\\", "\\\\").replace('"', '\\"')
                character = f'"{character}"'
            await self._send_command(
                writer,
                CommandRequest(
                    session_id=self.session_id,
                    world=self.world,
                    actor=Actor(ActorType.STARTUP, "automatic-login"),
                    text=(f"connect {character} {self.config.login.password.get_secret_value()}"),
                    sensitive=True,
                ),
            )
            if self.config.provenance.nospoof and self.config.server in {
                "rhost",
                "tinymush",
                "tinymux",
            }:
                await self._send_command(
                    writer,
                    CommandRequest(
                        session_id=self.session_id,
                        world=self.world,
                        actor=Actor(ActorType.STARTUP, "enable-nospoof"),
                        text="@set me=NOSPOOF",
                    ),
                )

        for command in self.config.startup_commands:
            await self._send_command(
                writer,
                CommandRequest(
                    session_id=self.session_id,
                    world=self.world,
                    actor=Actor(ActorType.STARTUP, "startup-command"),
                    text=command,
                ),
            )

    async def _send_command(self, writer: asyncio.StreamWriter, request: CommandRequest) -> None:
        if request.session_id != self.session_id or request.world != self.world:
            raise ValueError("command request does not belong to this session")
        async with self._event_lock:
            event = outbound_audit_event(
                request,
                connection_generation=self.connection_generation,
                sequence=self._sequence,
                monotonic_ns=time.monotonic_ns(),
            )
            self._sequence += 1
            await self.event_bus.publish(event)

        if request.actor.type is ActorType.HUMAN and request.text.strip().casefold() == "quit":
            self._quit_requested = True
        encoded = request.text.encode(self.encoding, errors="strict")
        async with self._wire_lock:
            writer.write(escape_iac(encoded) + b"\r\n")
            await writer.drain()
        self._last_command_activity = asyncio.get_running_loop().time()

    async def _publish_inbound(self, text: str) -> None:
        parsed = self.adapter.parse(text, show_prefix=self.show_nospoof_prefix)
        await self._publish(
            direction=Direction.INBOUND,
            kind=parsed.kind,
            canonical_text=parsed.canonical_text,
            plain_text=parsed.plain_text,
            display_text=parsed.display_text,
            provenance=parsed.provenance,
            parser_name=parsed.parser_name,
            parser_version=parsed.parser_version,
            confidence=parsed.confidence,
            metadata={"message_text": parsed.message_text},
        )

    async def _publish_telnet(self, event: TelnetEvent) -> None:
        await self._publish(
            direction=Direction.INTERNAL,
            kind=EventKind.TELNET,
            metadata={
                "telnet_kind": event.kind.value,
                "command": event.command,
                "option": event.option,
                "payload_hex": event.payload.hex() if event.payload else None,
            },
        )

    async def _set_state(self, state: SessionState, **metadata: Any) -> None:
        self.state = state
        await self._publish(
            direction=Direction.INTERNAL,
            kind=EventKind.CONNECTION,
            actor=Actor(ActorType.SYSTEM, "world-session"),
            metadata={"state": state.value, **metadata},
        )

    async def _publish(
        self,
        *,
        direction: Direction,
        kind: EventKind,
        canonical_text: str | None = None,
        plain_text: str | None = None,
        display_text: str | None = None,
        actor: Actor | None = None,
        provenance: Provenance | None = None,
        parser_name: str | None = None,
        parser_version: str | None = None,
        confidence: Confidence = Confidence.UNKNOWN,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._event_lock:
            event = Event(
                session_id=self.session_id,
                world=self.world,
                connection_generation=self.connection_generation,
                sequence=self._sequence,
                direction=direction,
                kind=kind,
                monotonic_ns=time.monotonic_ns(),
                canonical_text=canonical_text,
                plain_text=plain_text,
                display_text=display_text,
                actor=actor,
                provenance=provenance,
                parser_name=parser_name,
                parser_version=parser_version,
                confidence=confidence,
                metadata=metadata or {},
            )
            self._sequence += 1
            await self.event_bus.publish(event)


class SessionManager:
    def __init__(self, sessions: Sequence[WorldSession]) -> None:
        aliases = [session.world for session in sessions]
        if len(aliases) != len(set(aliases)):
            raise ValueError("world session aliases must be unique")
        self.sessions = {session.world: session for session in sessions}

    async def start_autoconnect(self) -> None:
        for session in self.sessions.values():
            if session.config.autoconnect:
                await session.start()

    async def stop_all(self) -> None:
        await asyncio.gather(*(session.stop() for session in self.sessions.values()))
