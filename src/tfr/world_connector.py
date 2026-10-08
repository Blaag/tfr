from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import hashlib
import json
import os
import re
import signal
import socket
import ssl
import stat
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tfr.telnet import TelnetCodec, escape_iac

CONNECTOR_PROTOCOL_VERSION = 1
CONNECTOR_GRACE_SECONDS = 30 * 60
DEFAULT_PER_WORLD_BUFFER_BYTES = 1024 * 1024
DEFAULT_GLOBAL_BUFFER_BYTES = 16 * 1024 * 1024
MAXIMUM_MESSAGE_BYTES = 1024 * 1024


class ProtocolError(ValueError):
    """Raised when a connector control message is malformed."""


class ConnectorReplacementRequired(ProtocolError):
    def __init__(self, reason: str, existing_build_id: str) -> None:
        super().__init__(f"world connector replacement required: {reason}")
        self.reason = reason
        self.existing_build_id = existing_build_id


@dataclass(frozen=True, slots=True)
class BufferedFrame:
    world: str
    sequence: int
    payload: bytes
    insertion: int


@dataclass(frozen=True, slots=True)
class OverflowNotice:
    notice_id: int
    world: str
    first_dropped_sequence: int
    last_dropped_sequence: int
    dropped_frames: int
    dropped_bytes: int


class ConnectorLease:
    def __init__(self, *, now: float, grace_seconds: float = CONNECTOR_GRACE_SECONDS) -> None:
        if now < 0:
            raise ValueError("lease time cannot be negative")
        if grace_seconds <= 0:
            raise ValueError("grace period must be positive")
        self.grace_seconds = grace_seconds
        self.deadline: float | None = None

    def attach(self, *, now: float) -> None:
        if now < 0:
            raise ValueError("lease time cannot be negative")
        self.deadline = None

    def detach(self, *, now: float) -> None:
        if now < 0:
            raise ValueError("lease time cannot be negative")
        if self.deadline is None:
            self.deadline = now + self.grace_seconds

    def expired(self, *, now: float) -> bool:
        return self.deadline is not None and now >= self.deadline


class ReplayBuffer:
    def __init__(self, *, per_world_bytes: int, global_bytes: int) -> None:
        if per_world_bytes < 1 or global_bytes < 1:
            raise ValueError("buffer limits must be positive")
        self.per_world_bytes = per_world_bytes
        self.global_bytes = global_bytes
        self._frames: dict[str, deque[BufferedFrame]] = defaultdict(deque)
        self._world_bytes: dict[str, int] = defaultdict(int)
        self._next_sequence: dict[str, int] = defaultdict(lambda: 1)
        self._total_bytes = 0
        self._next_insertion = 1
        self._next_notice_id = 1
        self._notices: dict[int, OverflowNotice] = {}

    def append(self, world: str, payload: bytes) -> BufferedFrame:
        if not world:
            raise ValueError("world cannot be empty")
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("frame payload must be non-empty bytes")
        frame = BufferedFrame(
            world=world,
            sequence=self._next_sequence[world],
            payload=payload,
            insertion=self._next_insertion,
        )
        self._next_sequence[world] += 1
        self._next_insertion += 1
        self._frames[world].append(frame)
        self._world_bytes[world] += len(payload)
        self._total_bytes += len(payload)
        dropped: list[BufferedFrame] = []
        while self._world_bytes[world] > self.per_world_bytes and self._frames[world]:
            dropped.append(self._drop(self._frames[world][0]))
        while self._total_bytes > self.global_bytes:
            oldest = min(
                (frames[0] for frames in self._frames.values() if frames),
                key=lambda candidate: candidate.insertion,
            )
            dropped.append(self._drop(oldest))
        for dropped_world in dict.fromkeys(item.world for item in dropped):
            self._record_overflow(
                dropped_world,
                [item for item in dropped if item.world == dropped_world],
            )
        return frame

    def frames(self, world: str) -> tuple[BufferedFrame, ...]:
        return tuple(self._frames.get(world, ()))

    def acknowledge(self, world: str, sequence: int) -> None:
        latest = self._next_sequence.get(world, 1) - 1
        if sequence > latest:
            raise ValueError("acknowledgement is beyond latest sequence")
        frames = self._frames.get(world)
        while frames and frames[0].sequence <= sequence:
            frame = frames.popleft()
            self._world_bytes[world] -= len(frame.payload)
            self._total_bytes -= len(frame.payload)

    def overflow_notices(self) -> tuple[OverflowNotice, ...]:
        return tuple(self._notices.values())

    def acknowledge_overflow(self, notice_id: int) -> None:
        if self._notices.pop(notice_id, None) is None:
            raise ValueError("unknown overflow notice")

    def _drop(self, frame: BufferedFrame) -> BufferedFrame:
        frames = self._frames[frame.world]
        if not frames or frames[0] is not frame:
            raise RuntimeError("replay buffer ordering is corrupt")
        frames.popleft()
        size = len(frame.payload)
        self._world_bytes[frame.world] -= size
        self._total_bytes -= size
        return frame

    def _record_overflow(self, world: str, frames: list[BufferedFrame]) -> None:
        if not frames:
            return
        notice = OverflowNotice(
            notice_id=self._next_notice_id,
            world=world,
            first_dropped_sequence=min(frame.sequence for frame in frames),
            last_dropped_sequence=max(frame.sequence for frame in frames),
            dropped_frames=len(frames),
            dropped_bytes=sum(len(frame.payload) for frame in frames),
        )
        self._next_notice_id += 1
        self._notices[notice.notice_id] = notice


def encode_message(message: dict[str, Any]) -> bytes:
    if not isinstance(message, dict) or not all(isinstance(key, str) for key in message):
        raise ProtocolError("connector message must be a JSON object with string keys")
    encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if len(encoded) > MAXIMUM_MESSAGE_BYTES:
        raise ProtocolError("connector message exceeds the size limit")
    return encoded


class FrameDecoder:
    def __init__(self, *, maximum_bytes: int = MAXIMUM_MESSAGE_BYTES) -> None:
        if maximum_bytes < 2:
            raise ValueError("maximum message size is too small")
        self.maximum_bytes = maximum_bytes
        self._pending = bytearray()

    def feed(self, content: bytes) -> tuple[dict[str, Any], ...]:
        self._pending.extend(content)
        if len(self._pending) > self.maximum_bytes and b"\n" not in self._pending:
            raise ProtocolError("connector message exceeds the size limit")
        messages: list[dict[str, Any]] = []
        while (newline := self._pending.find(b"\n")) >= 0:
            if newline + 1 > self.maximum_bytes:
                raise ProtocolError("connector message exceeds the size limit")
            raw = bytes(self._pending[:newline])
            del self._pending[: newline + 1]
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError("connector message is not valid JSON") from exc
            if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
                raise ProtocolError("connector message must be a JSON object")
            messages.append(value)
        return tuple(messages)


def connector_build_id() -> str:
    digest = hashlib.sha256(Path(__file__).read_bytes())
    digest.update(Path(TelnetCodec.__module__.replace(".", "/") + ".py").name.encode())
    digest.update((Path(__file__).with_name("telnet.py")).read_bytes())
    return digest.hexdigest()


class _TextFramer:
    def __init__(self, maximum: int = 16_384) -> None:
        self.maximum = maximum
        self.pending = ""

    def feed(self, text: str) -> tuple[str, ...]:
        self.pending += text
        frames: list[str] = []
        start = 0
        index = 0
        while index < len(self.pending):
            if index - start >= self.maximum:
                frames.append(self.pending[start:index])
                start = index
            character = self.pending[index]
            if character == "\n":
                frames.append(self.pending[start : index + 1])
                start = index + 1
            elif character == "\r":
                if index + 1 == len(self.pending):
                    break
                end = index + 2 if self.pending[index + 1] == "\n" else index + 1
                frames.append(self.pending[start:end])
                start = end
                index = end - 1
            index += 1
        self.pending = self.pending[start:]
        return tuple(frames)

    def flush(self) -> str | None:
        if not self.pending:
            return None
        pending = self.pending
        self.pending = ""
        return pending


class _WorldConnection:
    def __init__(
        self,
        world: str,
        config: dict[str, Any],
        *,
        publish: Any,
    ) -> None:
        expected = {
            "host",
            "port",
            "encoding",
            "reconnect",
            "tls",
            "startup_commands",
            "login",
            "idle",
        }
        if not set(config) <= expected:
            raise ValueError("world configuration contains unknown fields")
        host = config.get("host")
        port = config.get("port")
        if not isinstance(host, str) or not host:
            raise ValueError("world host is invalid")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535:
            raise ValueError("world port is invalid")
        encoding = config.get("encoding", "utf-8")
        if not isinstance(encoding, str):
            raise ValueError("world encoding is invalid")
        codecs.lookup(encoding)
        reconnect = config.get("reconnect", True)
        if not isinstance(reconnect, bool):
            raise ValueError("world reconnect setting is invalid")
        startup = config.get("startup_commands", [])
        if not isinstance(startup, list) or not all(isinstance(item, str) for item in startup):
            raise ValueError("world startup commands are invalid")
        tls = config.get("tls", {"enabled": False})
        if not isinstance(tls, dict) or not isinstance(tls.get("enabled", False), bool):
            raise ValueError("world TLS configuration is invalid")
        self.world = world
        self.host = host
        self.port = port
        self.encoding = encoding
        self.reconnect = reconnect
        self.startup_commands = tuple(startup)
        self.login = config.get("login")
        self.idle = config.get("idle")
        self.tls = tls
        self.publish = publish
        self.generation = 0
        self._runner: asyncio.Task[None] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._commands: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1_000)
        self._last_command_activity = 0.0
        self._quit_requested = False
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._runner is not None and not self._runner.done():
            return
        self._stop.clear()
        self._quit_requested = False
        self._runner = asyncio.create_task(self._run(), name=f"tfr-connector-{self.world}")

    async def stop(self) -> None:
        self._stop.set()
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None

    async def send(self, payload: bytes, *, quit: bool = False) -> None:
        if not payload or len(payload) > 16_384:
            raise ValueError("world command payload size is invalid")
        if quit:
            self._quit_requested = True
        await self._commands.put(payload)

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                await self._state("connecting")
                error: Exception | None = None
                try:
                    await self._connect()
                    error = ConnectionError("remote closed the connection")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = exc
                await self._state(
                    "disconnected",
                    error_type=type(error).__name__ if error else None,
                    error=str(error) if error else None,
                )
                if self._stop.is_set() or self._quit_requested or not self.reconnect:
                    break
                await self._state("reconnect_wait", delay_seconds=2.0)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=2.0)
        finally:
            await self._state("stopped")

    async def _connect(self) -> None:
        options: dict[str, Any] = {}
        if self.tls.get("enabled", False):
            verify = self.tls.get("verify", True)
            if verify:
                context = ssl.create_default_context(cafile=self.tls.get("ca_file"))
            else:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            options["ssl"] = context
            options["server_hostname"] = self.tls.get("server_hostname") or self.host
        reader, writer = await asyncio.open_connection(self.host, self.port, **options)
        self._writer = writer
        self._last_command_activity = time.monotonic()
        self.generation += 1
        await self._state(
            "connected",
            tls=bool(self.tls.get("enabled", False)),
            tls_verified=bool(self.tls.get("enabled", False) and self.tls.get("verify", True)),
        )
        decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        framer = _TextFramer()
        codec = TelnetCodec(encoding=self.encoding)
        try:
            await self._send_startup()
            tasks = {
                asyncio.create_task(
                    self._read_connection(reader, writer, decoder, framer, codec),
                    name=f"tfr-connector-reader-{self.world}",
                ),
                asyncio.create_task(
                    self._write_connection(writer),
                    name=f"tfr-connector-writer-{self.world}",
                ),
            }
            if isinstance(self.idle, dict):
                tasks.add(
                    asyncio.create_task(
                        self._idle_connection(),
                        name=f"tfr-connector-idle-{self.world}",
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
            self._writer = None
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _read_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        decoder: Any,
        framer: _TextFramer,
        codec: TelnetCodec,
    ) -> None:
        while True:
            try:
                if framer.pending:
                    chunk = await asyncio.wait_for(reader.read(65_536), timeout=0.05)
                else:
                    chunk = await reader.read(65_536)
            except TimeoutError:
                pending = framer.flush()
                if pending is not None:
                    await self._data(pending)
                continue
            if not chunk:
                final = decoder.decode(b"", final=True)
                for frame in framer.feed(final):
                    await self._data(frame)
                pending = framer.flush()
                if pending is not None:
                    await self._data(pending)
                return
            result = codec.feed(chunk)
            if result.responses:
                async with self._write_lock:
                    for response in result.responses:
                        writer.write(response)
                    await writer.drain()
            for event in result.events:
                await self.publish(
                    self.world,
                    {
                        "kind": "telnet",
                        "generation": self.generation,
                        "telnet_kind": event.kind.value,
                        "command": event.command,
                        "option": event.option,
                        "payload_hex": event.payload.hex() if event.payload else None,
                    },
                )
            if result.data:
                for frame in framer.feed(decoder.decode(result.data, final=False)):
                    await self._data(frame)

    async def _write_connection(self, writer: asyncio.StreamWriter) -> None:
        while True:
            payload = await self._commands.get()
            async with self._write_lock:
                writer.write(escape_iac(payload) + b"\r\n")
                await writer.drain()
            self._last_command_activity = time.monotonic()

    async def _idle_connection(self) -> None:
        assert isinstance(self.idle, dict)
        after = self.idle.get("after_seconds")
        command = self.idle.get("command")
        if (
            not isinstance(after, (int, float))
            or isinstance(after, bool)
            or after <= 0
            or not isinstance(command, str)
        ):
            raise ValueError("world idle configuration is invalid")
        while True:
            remaining = after - (time.monotonic() - self._last_command_activity)
            if remaining > 0:
                await asyncio.sleep(remaining)
            else:
                await self.send(command.encode(self.encoding, errors="strict"))
                self._last_command_activity = time.monotonic()

    async def _send_startup(self) -> None:
        commands: list[str] = []
        if isinstance(self.login, dict):
            character = self.login.get("character")
            password = self.login.get("password")
            if not isinstance(character, str) or not isinstance(password, str):
                raise ValueError("world login configuration is invalid")
            if any(character.isspace() for character in character):
                character = character.replace("\\", "\\\\").replace('"', '\\"')
                character = f'"{character}"'
            commands.append(f"connect {character} {password}")
        commands.extend(self.startup_commands)
        for command in commands:
            await self.send(command.encode(self.encoding, errors="strict"))

    async def _data(self, text: str) -> None:
        await self.publish(
            self.world,
            {"kind": "data", "generation": self.generation, "text": text},
        )

    async def _state(self, state: str, **values: Any) -> None:
        await self.publish(
            self.world,
            {"kind": "state", "generation": self.generation, "state": state, **values},
        )


class WorldConnectorServer:
    def __init__(
        self,
        path: Path,
        *,
        build_id: str | None = None,
        grace_seconds: float = CONNECTOR_GRACE_SECONDS,
        per_world_bytes: int = DEFAULT_PER_WORLD_BUFFER_BYTES,
        global_bytes: int = DEFAULT_GLOBAL_BUFFER_BYTES,
    ) -> None:
        if build_id is None:
            build_id = connector_build_id()
        if re.fullmatch(r"[0-9a-f]{64}", build_id) is None:
            raise ValueError("connector build ID must be a lowercase SHA-256 digest")
        self.path = path
        self.build_id = build_id
        self.grace_seconds = grace_seconds
        self.replay = ReplayBuffer(
            per_world_bytes=per_world_bytes,
            global_bytes=global_bytes,
        )
        self._server: asyncio.Server | None = None
        self._gateway_writer: asyncio.StreamWriter | None = None
        self._gateway_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
        self._lease = ConnectorLease(now=time.monotonic(), grace_seconds=grace_seconds)
        self._detached = asyncio.Event()
        self._detached.set()
        self._expired = asyncio.Event()
        self._expiry_task: asyncio.Task[None] | None = None
        self._acknowledgements: dict[str, int] = {}
        self._ack_changed = asyncio.Condition()
        self._overflow_acks: set[int] = set()
        self._world_configuration: dict[str, Any] | None = None
        self._worlds: dict[str, _WorldConnection] = {}

    @property
    def expired(self) -> bool:
        return self._expired.is_set()

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("connector server is already started")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._validate_socket_parent()
        if os.path.lexists(self.path):
            raise RuntimeError(f"connector socket already exists: {self.path}")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
            self._server = await asyncio.start_unix_server(
                self._accept_gateway,
                sock=listener,
                limit=MAXIMUM_MESSAGE_BYTES,
            )
        except BaseException:
            listener.close()
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            raise
        self._lease.detach(now=time.monotonic())
        self._sync_expiry_task()

    def _validate_socket_parent(self) -> None:
        if os.name != "posix":
            return
        parent = self.path.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(parent.st_mode):
            raise RuntimeError("connector socket parent is not a directory")
        if parent.st_uid != os.geteuid():
            raise RuntimeError("connector socket parent is not owned by this user")
        if stat.S_IMODE(parent.st_mode) & 0o077:
            raise RuntimeError("connector socket parent must have mode 0700")

    async def stop(self) -> None:
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._expiry_task
            self._expiry_task = None
        writer = self._gateway_writer
        self._gateway_writer = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        await asyncio.gather(
            *(world.stop() for world in self._worlds.values()),
            return_exceptions=True,
        )
        self._worlds.clear()

    async def publish(self, world: str, payload: bytes) -> BufferedFrame:
        async with self._delivery_lock:
            existing_notices = {notice.notice_id for notice in self.replay.overflow_notices()}
            frame = self.replay.append(world, payload)
            writer = self._gateway_writer
            if writer is not None:
                for notice in self.replay.overflow_notices():
                    if notice.notice_id not in existing_notices:
                        await self._write_overflow(writer, notice)
                await self._write(
                    writer,
                    {
                        "type": "frame",
                        "world": world,
                        "sequence": frame.sequence,
                        "payload": base64.b64encode(payload).decode("ascii"),
                    },
                )
            return frame

    async def wait_detached(self) -> None:
        await self._detached.wait()

    async def wait_expired(self) -> None:
        await self._expired.wait()

    async def wait_for_ack(self, world: str, sequence: int) -> None:
        async with self._ack_changed:
            await self._ack_changed.wait_for(
                lambda: self._acknowledgements.get(world, 0) >= sequence
            )

    async def wait_for_overflow_ack(self, notice_id: int) -> None:
        async with self._ack_changed:
            await self._ack_changed.wait_for(lambda: notice_id in self._overflow_acks)

    async def _accept_gateway(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            message = await self._read(reader)
            if message.get("type") != "attach":
                if message.get("type") == "replace":
                    if (
                        message.get("protocol") == CONNECTOR_PROTOCOL_VERSION
                        and message.get("existing_build_id") == self.build_id
                    ):
                        await self._write(
                            writer,
                            {"type": "replacing", "build_id": self.build_id},
                        )
                        self._expired.set()
                    else:
                        await self._write(
                            writer,
                            {"type": "rejected", "reason": "replacement_identity"},
                        )
                    return
                await self._write(writer, {"type": "rejected", "reason": "handshake"})
                return
            if message.get("protocol") != CONNECTOR_PROTOCOL_VERSION:
                await self._write(writer, {"type": "rejected", "reason": "protocol"})
                return
            if message.get("build_id") != self.build_id:
                await self._write(
                    writer,
                    {
                        "type": "rejected",
                        "reason": "build_id",
                        "build_id": self.build_id,
                    },
                )
                return
            worlds = message.get("worlds")
            if not isinstance(worlds, dict):
                await self._write(writer, {"type": "rejected", "reason": "configuration"})
                return
            if self._world_configuration is None:
                try:
                    self._configure_worlds(worlds)
                except (TypeError, ValueError):
                    await self._write(
                        writer,
                        {"type": "rejected", "reason": "configuration"},
                    )
                    return
            elif worlds != self._world_configuration:
                await self._write(
                    writer,
                    {
                        "type": "rejected",
                        "reason": "configuration",
                        "build_id": self.build_id,
                    },
                )
                return
            async with self._gateway_lock:
                if self._gateway_writer is not None:
                    await self._write(
                        writer,
                        {"type": "rejected", "reason": "already_attached"},
                    )
                    return
                self._gateway_writer = writer
                self._detached.clear()
                self._lease.attach(now=time.monotonic())
                self._sync_expiry_task()
            async with self._delivery_lock:
                await self._write(
                    writer,
                    {
                        "type": "attached",
                        "protocol": CONNECTOR_PROTOCOL_VERSION,
                        "build_id": self.build_id,
                    },
                )
                await self._send_replay(writer)
            while True:
                message = await self._read(reader)
                await self._handle_gateway_message(message)
        except (ConnectionError, EOFError, OSError, ProtocolError, ValueError):
            pass
        finally:
            async with self._gateway_lock:
                if self._gateway_writer is writer:
                    self._gateway_writer = None
                    self._detached.set()
                    self._lease.detach(now=time.monotonic())
                    self._sync_expiry_task()
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _handle_gateway_message(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "ack":
            world = message.get("world")
            sequence = message.get("sequence")
            if not isinstance(world, str) or not isinstance(sequence, int):
                raise ProtocolError("invalid frame acknowledgement")
            self.replay.acknowledge(world, sequence)
            async with self._ack_changed:
                self._acknowledgements[world] = max(
                    sequence, self._acknowledgements.get(world, 0)
                )
                self._ack_changed.notify_all()
        elif message_type == "ack_overflow":
            notice_id = message.get("notice_id")
            if not isinstance(notice_id, int):
                raise ProtocolError("invalid overflow acknowledgement")
            self.replay.acknowledge_overflow(notice_id)
            async with self._ack_changed:
                self._overflow_acks.add(notice_id)
                self._ack_changed.notify_all()
        elif message_type == "shutdown":
            self._expired.set()
        elif message_type == "start_world":
            world = self._message_world(message)
            await self._worlds[world].start()
        elif message_type == "stop_world":
            world = self._message_world(message)
            await self._worlds[world].stop()
        elif message_type == "send_world":
            world = self._message_world(message)
            payload = message.get("payload")
            if not isinstance(payload, str):
                raise ProtocolError("world payload is invalid")
            try:
                content = base64.b64decode(payload, validate=True)
            except ValueError as exc:
                raise ProtocolError("world payload is not valid base64") from exc
            quit_requested = message.get("quit", False)
            if not isinstance(quit_requested, bool):
                raise ProtocolError("world quit marker is invalid")
            await self._worlds[world].send(content, quit=quit_requested)
        else:
            raise ProtocolError("unknown connector message")

    def _message_world(self, message: dict[str, Any]) -> str:
        world = message.get("world")
        if not isinstance(world, str) or world not in self._worlds:
            raise ProtocolError("unknown connector world")
        return world

    def _configure_worlds(self, worlds: dict[str, Any]) -> None:
        configured: dict[str, _WorldConnection] = {}
        for name, value in worlds.items():
            if not isinstance(name, str) or not name or not isinstance(value, dict):
                raise ValueError("world configuration is invalid")
            configured[name] = _WorldConnection(name, value, publish=self._publish_world_event)
        self._world_configuration = worlds
        self._worlds = configured

    async def _publish_world_event(self, world: str, event: dict[str, Any]) -> None:
        await self.publish(
            world,
            json.dumps(event, separators=(",", ":"), ensure_ascii=True).encode(),
        )

    async def _send_replay(self, writer: asyncio.StreamWriter) -> None:
        for notice in self.replay.overflow_notices():
            await self._write_overflow(writer, notice)
        frames = sorted(
            (frame for world in self.replay._frames for frame in self.replay.frames(world)),
            key=lambda frame: frame.insertion,
        )
        for frame in frames:
            await self._write(
                writer,
                {
                    "type": "frame",
                    "world": frame.world,
                    "sequence": frame.sequence,
                    "payload": base64.b64encode(frame.payload).decode("ascii"),
                },
            )

    async def _write_overflow(
        self,
        writer: asyncio.StreamWriter,
        notice: OverflowNotice,
    ) -> None:
        await self._write(
            writer,
            {
                "type": "overflow",
                "notice_id": notice.notice_id,
                "world": notice.world,
                "first_dropped_sequence": notice.first_dropped_sequence,
                "last_dropped_sequence": notice.last_dropped_sequence,
                "dropped_frames": notice.dropped_frames,
                "dropped_bytes": notice.dropped_bytes,
            },
        )

    async def _read(self, reader: asyncio.StreamReader) -> dict[str, Any]:
        content = await reader.readline()
        if not content:
            raise EOFError
        decoder = FrameDecoder(maximum_bytes=MAXIMUM_MESSAGE_BYTES)
        messages = decoder.feed(content)
        if len(messages) != 1:
            raise ProtocolError("expected exactly one connector message")
        return messages[0]

    async def _write(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        async with self._write_lock:
            writer.write(encode_message(message))
            await writer.drain()

    def _sync_expiry_task(self) -> None:
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            self._expiry_task = None
        if self._lease.deadline is not None:
            self._expiry_task = asyncio.create_task(
                self._wait_for_expiry(self._lease.deadline),
                name="tfr-world-connector-expiry",
            )

    async def _wait_for_expiry(self, deadline: float) -> None:
        delay = max(0.0, deadline - time.monotonic())
        await asyncio.sleep(delay)
        if self._lease.deadline == deadline and self._lease.expired(now=time.monotonic()):
            self._expired.set()


class WorldConnectorClient:
    def __init__(
        self,
        path: Path,
        *,
        build_id: str | None = None,
        worlds: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.build_id = build_id or connector_build_id()
        self.worlds = worlds or {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        if self._writer is not None:
            raise RuntimeError("connector client is already connected")
        reader, writer = await asyncio.open_unix_connection(self.path, limit=MAXIMUM_MESSAGE_BYTES)
        self._reader = reader
        self._writer = writer
        try:
            await self._write(
                {
                    "type": "attach",
                    "protocol": CONNECTOR_PROTOCOL_VERSION,
                    "build_id": self.build_id,
                    "worlds": self.worlds,
                }
            )
            response = await self._read()
            if response != {
                "type": "attached",
                "protocol": CONNECTOR_PROTOCOL_VERSION,
                "build_id": self.build_id,
            }:
                reason = response.get("reason", "invalid handshake")
                existing = response.get("build_id")
                if reason in {"build_id", "configuration"} and isinstance(existing, str):
                    raise ConnectorReplacementRequired(reason, existing)
                raise ProtocolError(f"connector rejected Gateway attachment: {reason}")
        except BaseException:
            await self.close()
            raise

    async def request_replacement(self) -> str:
        if self._writer is not None:
            raise RuntimeError("connector client is already connected")
        reader, writer = await asyncio.open_unix_connection(self.path, limit=MAXIMUM_MESSAGE_BYTES)
        self._reader = reader
        self._writer = writer
        try:
            await self._write(
                {
                    "type": "attach",
                    "protocol": CONNECTOR_PROTOCOL_VERSION,
                    "build_id": self.build_id,
                }
            )
            rejection = await self._read()
            existing = rejection.get("build_id")
            if rejection.get("reason") not in {
                "build_id",
                "configuration",
            } or not isinstance(existing, str):
                raise ProtocolError("connector did not report an incompatible build")
        finally:
            await self.close()
        reader, writer = await asyncio.open_unix_connection(self.path, limit=MAXIMUM_MESSAGE_BYTES)
        self._reader = reader
        self._writer = writer
        try:
            await self._write(
                {
                    "type": "replace",
                    "protocol": CONNECTOR_PROTOCOL_VERSION,
                    "existing_build_id": existing,
                }
            )
            response = await self._read()
            if response != {"type": "replacing", "build_id": existing}:
                raise ProtocolError("connector rejected replacement request")
            return existing
        finally:
            await self.close()

    async def close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def receive(self) -> dict[str, Any]:
        message = await self._read()
        if message.get("type") == "frame":
            payload = message.get("payload")
            if not isinstance(payload, str):
                raise ProtocolError("connector frame payload is invalid")
            try:
                decoded = base64.b64decode(payload, validate=True)
            except ValueError as exc:
                raise ProtocolError("connector frame payload is not valid base64") from exc
            try:
                event = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError):
                event = None
            if isinstance(event, dict) and isinstance(event.get("kind"), str):
                message = {
                    "type": "world_event",
                    "world": message.get("world"),
                    "sequence": message.get("sequence"),
                    **event,
                }
            else:
                message = {**message, "payload": decoded}
        return message

    async def acknowledge(self, world: str, sequence: int) -> None:
        await self._write(
            {"type": "ack", "world": world, "sequence": sequence}
        )

    async def acknowledge_overflow(self, notice_id: int) -> None:
        await self._write({"type": "ack_overflow", "notice_id": notice_id})

    async def shutdown(self) -> None:
        await self._write({"type": "shutdown"})

    async def start_world(self, world: str) -> None:
        await self._write({"type": "start_world", "world": world})

    async def stop_world(self, world: str) -> None:
        await self._write({"type": "stop_world", "world": world})

    async def send_world(self, world: str, payload: bytes, *, quit: bool = False) -> None:
        await self._write(
            {
                "type": "send_world",
                "world": world,
                "payload": base64.b64encode(payload).decode("ascii"),
                "quit": quit,
            }
        )

    async def _read(self) -> dict[str, Any]:
        if self._reader is None:
            raise RuntimeError("connector client is not connected")
        content = await self._reader.readline()
        if not content:
            raise ConnectionError("world connector disconnected")
        decoder = FrameDecoder(maximum_bytes=MAXIMUM_MESSAGE_BYTES)
        messages = decoder.feed(content)
        if len(messages) != 1:
            raise ProtocolError("expected exactly one connector message")
        return messages[0]

    async def _write(self, message: dict[str, Any]) -> None:
        if self._writer is None:
            raise RuntimeError("connector client is not connected")
        self._writer.write(encode_message(message))
        await self._writer.drain()


def connector_worlds(bundle: Any) -> dict[str, Any]:
    defaults = bundle.worlds.defaults
    worlds: dict[str, Any] = {}
    for name, config in bundle.worlds.worlds.items():
        encoding = config.encoding or defaults.encoding
        reconnect = defaults.reconnect if config.reconnect is None else config.reconnect
        login = None
        if config.login is not None:
            login = {
                "character": config.login.character,
                "password": config.login.password.get_secret_value(),
            }
        idle = None
        if config.idle is not None:
            idle = {
                "after_seconds": config.idle.after_seconds,
                "command": config.idle.command,
            }
        worlds[name] = {
            "host": config.host,
            "port": config.port,
            "encoding": encoding,
            "reconnect": reconnect,
            "tls": {
                "enabled": config.tls.enabled,
                "verify": config.tls.verify,
                "ca_file": str(config.tls.ca_file) if config.tls.ca_file is not None else None,
                "server_hostname": config.tls.server_hostname,
            },
            "startup_commands": list(config.startup_commands),
            "login": login,
            "idle": idle,
        }
    return worlds


async def connect_or_spawn_connector(
    path: Path,
    worlds: dict[str, Any],
    *,
    timeout: float = 5.0,
) -> WorldConnectorClient:
    import subprocess
    import sys

    deadline = time.monotonic() + timeout
    replacement_attempted = False
    spawned = False
    while True:
        client = WorldConnectorClient(path, worlds=worlds)
        try:
            await client.connect()
            return client
        except ConnectorReplacementRequired:
            if replacement_attempted:
                raise
            replacement_attempted = True
            replacement = WorldConnectorClient(path, worlds=worlds)
            await replacement.request_replacement()
        except (ConnectionRefusedError, FileNotFoundError):
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out connecting to world connector") from None
            if os.path.lexists(path) and not spawned:
                path_stat = path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISSOCK(path_stat.st_mode)
                    or (os.name == "posix" and path_stat.st_uid != os.geteuid())
                ):
                    raise RuntimeError("unsafe stale world connector socket") from None
                path.unlink()
            if not os.path.lexists(path) and not spawned:
                subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "tfr.world_connector",
                        "--socket",
                        str(path),
                    ],
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                )
                spawned = True
            await asyncio.sleep(0.05)
            continue
        while os.path.lexists(path) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        spawned = False
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out replacing world connector")


async def run_connector(path: Path) -> int:
    server = WorldConnectorServer(path)
    await server.start()
    try:
        await server.wait_expired()
    finally:
        await server.stop()
    return 0


def _current_python(initial_python: str) -> str:
    managed_root = os.environ.get("TFR_MANAGED_ROOT")
    if managed_root is None:
        return initial_python
    executable = "python.exe" if os.name == "nt" else "python"
    candidate = (
        Path(managed_root)
        / "current"
        / ("Scripts" if os.name == "nt" else "bin")
        / executable
    )
    return str(candidate) if candidate.is_file() else initial_python


async def supervise_gateway(
    connector_path: Path,
    gateway_arguments: list[str],
    *,
    initial_python: str,
) -> int:
    import subprocess

    server = WorldConnectorServer(connector_path)
    await server.start()
    gateway_socket = connector_path.with_name(
        connector_path.name.removesuffix(".worlds")
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(handled_signal, stop.set)
            installed_signals.append(handled_signal)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    try:
        while not stop.is_set() and not server.expired:
            environment = dict(os.environ)
            environment["TFR_GATEWAY_APP"] = "1"
            process = subprocess.Popen(
                [
                    _current_python(initial_python),
                    "-I",
                    "-m",
                    "tfr",
                    *gateway_arguments,
                ],
                env=environment,
                stdin=None,
                close_fds=True,
            )
            child_done = asyncio.create_task(
                asyncio.to_thread(process.wait), name="tfr-gateway-app-wait"
            )
            stop_wait = asyncio.create_task(stop.wait(), name="tfr-gateway-supervisor-stop")
            expiry_wait = asyncio.create_task(
                server.wait_expired(), name="tfr-gateway-connector-expiry"
            )
            done, pending = await asyncio.wait(
                {child_done, stop_wait, expiry_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if child_done not in done:
                process.terminate()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
                if process.poll() is None:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            if stop.is_set():
                return 0
            if server.expired:
                return 75
            if child_done.result() == 0:
                return 0
            with contextlib.suppress(FileNotFoundError):
                socket_stat = gateway_socket.stat(follow_symlinks=False)
                if socket_stat.st_uid == os.geteuid() and stat.S_ISSOCK(socket_stat.st_mode):
                    gateway_socket.unlink()
            await asyncio.sleep(2)
        return 75 if server.expired else 0
    finally:
        for handled_signal in installed_signals:
            loop.remove_signal_handler(handled_signal)
        await server.stop()


def launch_gateway_supervisor(
    connector_path: Path,
    gateway_arguments: list[str],
) -> None:
    environment = dict(os.environ)
    arguments = [
        sys.executable,
        "-I",
        "-m",
        "tfr.world_connector",
        "--supervise",
        "--socket",
        str(connector_path),
        "--initial-python",
        sys.executable,
        "--",
        *gateway_arguments,
    ]
    os.execve(sys.executable, arguments, environment)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Internal TFR world connector process.")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--supervise", action="store_true")
    parser.add_argument("--initial-python", default=sys.executable)
    parser.add_argument("gateway_arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.supervise:
            arguments = list(args.gateway_arguments)
            if arguments[:1] == ["--"]:
                arguments = arguments[1:]
            result = asyncio.run(
                supervise_gateway(
                    args.socket,
                    arguments,
                    initial_python=args.initial_python,
                )
            )
            if result == 75:
                replacement = [
                    _current_python(args.initial_python),
                    "-I",
                    "-m",
                    "tfr.world_connector",
                    "--supervise",
                    "--socket",
                    str(args.socket),
                    "--initial-python",
                    _current_python(args.initial_python),
                    "--",
                    *arguments,
                ]
                os.execve(replacement[0], replacement, dict(os.environ))
            raise SystemExit(result)
        raise SystemExit(asyncio.run(run_connector(args.socket)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
