from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Mapping
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from aiohttp import WSMsgType, web

from tfr.ansi import terminal_plain_text
from tfr.core import UnknownSessionError
from tfr.events import ActorType, Event, EventKind
from tfr.gateway import GatewayRuntime, HistorySubscription, SequencedEvent
from tfr.gateway_devices import DeviceRecord, DeviceStore

WEB_PROTOCOL_VERSION = 1
SESSION_COOKIE = "__Host-tfr_session"
TAILSCALE_LOGIN_HEADER = "Tailscale-User-Login"
MAX_WEB_MESSAGE_BYTES = 65_536
MAX_COMMANDS_PER_MINUTE = 30
MAX_PAIRING_ATTEMPTS_PER_MINUTE = 20
MAX_GLOBAL_PAIRING_ATTEMPTS_PER_MINUTE = 100
MAX_IDEMPOTENCY_RECORDS = 256
MAX_WEB_CLIENTS = 8
MAX_WEB_CLIENTS_PER_DEVICE = 2
MAX_WEB_EVENT_TEXT_BYTES = 8_192
MAX_WEB_EVENT_SOURCE_BYTES = 32_768
MAX_WEB_PROVENANCE_BYTES = 256
MAX_WEB_PROVENANCE_SOURCE_BYTES = 4_096
MAX_WEB_SNAPSHOT_BYTES = 2_097_152
MAX_WEB_SNAPSHOT_SOURCE_BYTES = 8_388_608
MAX_WEB_FRAMES_PER_MINUTE = 120
MAX_WEB_CONNECTIONS_PER_MINUTE = 12
WEB_WRITE_TIMEOUT_SECONDS = 5

_VISIBLE_EVENT_KINDS = {
    EventKind.RAW_OUTPUT,
    EventKind.COMMAND,
    EventKind.SPEECH,
    EventKind.SAY,
    EventKind.POSE,
    EventKind.PAGE,
    EventKind.CHANNEL,
    EventKind.EMIT,
    EventKind.SYSTEM,
    EventKind.CONNECTION,
}

_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


def browser_event(cursor: int, event: Event) -> dict[str, Any] | None:
    if not _browser_event_allowed(event):
        return None
    raw_text = event.display_text or event.plain_text or event.canonical_text or ""
    source_text, source_truncated = _bounded_utf8(raw_text, MAX_WEB_EVENT_SOURCE_BYTES)
    text = terminal_plain_text(source_text)
    encoded_text = text.encode("utf-8")
    text_truncated = source_truncated or len(encoded_text) > MAX_WEB_EVENT_TEXT_BYTES
    if text_truncated:
        text = _truncate_with_ellipsis(encoded_text, MAX_WEB_EVENT_TEXT_BYTES)
    value: dict[str, Any] = {
        "type": "event",
        "protocol": WEB_PROTOCOL_VERSION,
        "cursor": str(cursor),
        "event": {
            "id": str(event.event_id),
            "timestamp": event.timestamp.isoformat().replace("+00:00", "Z"),
            "world": event.world,
            "direction": event.direction.value,
            "kind": event.kind.value,
            "text": text,
            "redacted": event.redacted,
            "text_truncated": text_truncated,
        },
    }
    if event.provenance is not None:
        provenance = {
            key: _bounded_visible_text(item, MAX_WEB_PROVENANCE_BYTES)
            for key, item in {
                "sender_name": event.provenance.sender_name,
                "owner_name": event.provenance.owner_name,
                "server_source": event.provenance.server_source,
                "confidence": event.provenance.confidence.value,
            }.items()
            if item is not None
        }
        if provenance:
            value["event"]["provenance"] = provenance
    if event.kind is EventKind.CONNECTION:
        state = event.metadata.get("state")
        if isinstance(state, str):
            value["event"]["connection_state"] = state
    return value


def _bounded_visible_text(value: str, maximum_bytes: int) -> str:
    source, source_truncated = _bounded_utf8(value, MAX_WEB_PROVENANCE_SOURCE_BYTES)
    text = terminal_plain_text(source)
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes and not source_truncated:
        return text
    return _truncate_with_ellipsis(encoded, maximum_bytes)


def _bounded_utf8(value: str, maximum_bytes: int) -> tuple[str, bool]:
    maximum_characters = maximum_bytes
    candidate = value[:maximum_characters]
    encoded = candidate.encode("utf-8")
    if len(encoded) <= maximum_bytes and len(candidate) == len(value):
        return value, False
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore"), True


def _truncate_with_ellipsis(encoded: bytes, maximum_bytes: int) -> str:
    ellipsis = "…"
    prefix_bytes = max(0, maximum_bytes - len(ellipsis.encode("utf-8")))
    return encoded[:prefix_bytes].decode("utf-8", errors="ignore") + ellipsis


def _browser_source_bytes(event: Event) -> int:
    raw_text = event.display_text or event.plain_text or event.canonical_text or ""
    bounded_text, _truncated = _bounded_utf8(raw_text, MAX_WEB_EVENT_SOURCE_BYTES)
    total = len(bounded_text.encode("utf-8"))
    if event.provenance is not None:
        total += sum(
            len(_bounded_utf8(value, MAX_WEB_PROVENANCE_SOURCE_BYTES)[0].encode("utf-8"))
            for value in (
                event.provenance.sender_name,
                event.provenance.owner_name,
                event.provenance.server_source,
            )
            if value is not None
        )
    return total


def _browser_event_allowed(event: Event) -> bool:
    return event.kind in _VISIBLE_EVENT_KINDS and not (
        event.kind is EventKind.COMMAND
        and (event.actor is None or event.actor.type is not ActorType.HUMAN)
    )


class WebGatewayServer:
    def __init__(
        self,
        runtime: GatewayRuntime,
        *,
        origin: str,
        host: str,
        port: int,
        state_directory: Any,
        snapshot_events: int,
    ) -> None:
        if host not in {"127.0.0.1", "::1"}:
            raise ValueError("web gateway must listen only on loopback")
        if not 1 <= port <= 65_535:
            raise ValueError("web gateway port must be between 1 and 65535")
        parsed_origin = urlsplit(origin)
        if parsed_origin.scheme != "https" or not parsed_origin.netloc:
            raise ValueError("web gateway origin must be an HTTPS origin")
        self.runtime = runtime
        self.origin = origin.rstrip("/")
        self.expected_host = parsed_origin.netloc
        self.websocket_origin = f"wss://{parsed_origin.netloc}"
        self.host = host
        self.port = port
        self.snapshot_events = snapshot_events
        self.devices = DeviceStore(state_directory, self.origin)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._active_sockets: dict[UUID, set[web.WebSocketResponse]] = defaultdict(set)
        self._connection_counts: dict[UUID, int] = defaultdict(int)
        self._command_history: dict[
            UUID, OrderedDict[UUID, tuple[str, dict[str, Any]]]
        ] = defaultdict(OrderedDict)
        self._command_times: dict[UUID, deque[float]] = defaultdict(deque)
        self._command_locks: dict[UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._frame_times: dict[UUID, deque[float]] = defaultdict(deque)
        self._connection_times: dict[UUID, deque[float]] = defaultdict(deque)
        self._pairing_attempts: dict[str, deque[float]] = defaultdict(deque)
        self._global_pairing_attempts: deque[float] = deque()
        self._session_lock = asyncio.Lock()

    def create_pairing_url(self, label: str) -> str:
        allowed_worlds = tuple(self._non_agent_worlds())
        return self.devices.create_pairing_url(label, allowed_worlds)

    def _non_agent_worlds(self) -> frozenset[str]:
        agent_worlds = {
            str(descriptor["world"])
            for descriptor in self.runtime.agent_descriptors()
            if "world" in descriptor
        }
        return frozenset(
            str(descriptor["world"])
            for descriptor in self.runtime.world_descriptors()
            if str(descriptor["world"]) not in agent_worlds
        )

    def _authorized_worlds(self, device: DeviceRecord) -> frozenset[str]:
        return frozenset(device.allowed_worlds) & self._non_agent_worlds()

    def device_descriptors(self) -> list[dict[str, object]]:
        return self.devices.device_descriptors()

    async def revoke_device(self, device_id: UUID) -> bool:
        async with self._session_lock:
            revoked = await self.devices.revoke(device_id)
            sockets = tuple(self._active_sockets.get(device_id, ()))
        if revoked:
            for socket in sockets:
                await self._close_bounded(socket, code=1008, message=b"Device session revoked")
        return revoked

    async def start(self) -> None:
        if self._runner is not None:
            return
        application = self.application()
        runner = web.AppRunner(application, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        self._site = site

    def application(self) -> web.Application:
        application = web.Application(
            client_max_size=MAX_WEB_MESSAGE_BYTES,
            middlewares=[self._security_headers],
        )
        application.add_routes(
            [
                web.get("/api/session", self._session),
                web.post("/api/pair", self._pair),
                web.post("/api/logout", self._logout),
                web.get("/ws", self._websocket),
                web.get(
                    "/{asset:index.html|app.js|styles.css|manifest.webmanifest|sw.js|"
                    "icon.svg|icon-512.png|apple-touch-icon.png}",
                    self._asset,
                ),
                web.get("/", self._asset),
            ]
        )
        return application

    async def stop(self) -> None:
        await asyncio.gather(
            *(
                self._close_bounded(socket, code=1001, message=b"Gateway stopping")
                for sockets in tuple(self._active_sockets.values())
                for socket in tuple(sockets)
            )
        )
        self._active_sockets.clear()
        if self._runner is not None:
            await self._runner.cleanup()
        self._site = None
        self._runner = None

    @web.middleware
    async def _security_headers(
        self,
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        response = await handler(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            f"connect-src 'self' {self.websocket_origin}; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        return response

    def _validate_public_request(self, request: web.Request, *, require_origin: bool) -> None:
        hosts = request.headers.getall("Host", [])
        if hosts != [self.expected_host]:
            raise web.HTTPForbidden(text="Unexpected host")
        origins = request.headers.getall("Origin", [])
        if require_origin and origins != [self.origin]:
            raise web.HTTPForbidden(text="Unexpected origin")
        self._tailscale_login(request)

    @staticmethod
    def _tailscale_login(request: web.Request) -> str:
        values = request.headers.getall(TAILSCALE_LOGIN_HEADER, [])
        if len(values) != 1 or not 1 <= len(values[0]) <= 320 or not values[0].isprintable():
            raise web.HTTPForbidden(text="Tailscale Serve identity is required")
        return values[0]

    def _authenticate(self, request: web.Request) -> DeviceRecord | None:
        device = self.devices.authenticate(request.cookies.get(SESSION_COOKIE))
        if device is None or device.tailscale_login != self._tailscale_login(request):
            return None
        return device

    @staticmethod
    def _json_response(value: Mapping[str, Any], *, status: int = 200) -> web.Response:
        return web.json_response(
            dict(value),
            status=status,
            headers={"Cache-Control": "no-store"},
        )

    async def _session(self, request: web.Request) -> web.Response:
        self._validate_public_request(request, require_origin=False)
        device = self._authenticate(request)
        return self._json_response(
            {
                "paired": device is not None,
                "device": (
                    {"id": str(device.device_id), "label": device.label, "scope": device.scope}
                    if device is not None
                    else None
                ),
            }
        )

    async def _pair(self, request: web.Request) -> web.Response:
        self._validate_public_request(request, require_origin=True)
        tailscale_login = self._tailscale_login(request)
        now = time.monotonic()
        attempts = self._pairing_attempts[tailscale_login]
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        while (
            self._global_pairing_attempts and self._global_pairing_attempts[0] <= now - 60
        ):
            self._global_pairing_attempts.popleft()
        if (
            len(attempts) >= MAX_PAIRING_ATTEMPTS_PER_MINUTE
            or len(self._global_pairing_attempts) >= MAX_GLOBAL_PAIRING_ATTEMPTS_PER_MINUTE
        ):
            return self._json_response({"error": "Pairing rate limit exceeded"}, status=429)
        attempts.append(now)
        self._global_pairing_attempts.append(now)
        try:
            value = await request.json(loads=json.loads)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json_response({"error": "Invalid JSON"}, status=400)
        if not isinstance(value, dict) or set(value) != {"code"} or not isinstance(
            value.get("code"), str
        ):
            return self._json_response({"error": "Invalid pairing request"}, status=400)
        try:
            device, token = await self.devices.redeem(
                value["code"],
                tailscale_login,
            )
        except ValueError as exc:
            return self._json_response({"error": str(exc)}, status=401)
        response = self._json_response(
            {"paired": True, "device": {"id": str(device.device_id), "label": device.label}}
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return response

    async def _logout(self, request: web.Request) -> web.Response:
        self._validate_public_request(request, require_origin=True)
        device = self._authenticate(request)
        if device is not None:
            await self.revoke_device(device.device_id)
        response = self._json_response({"ok": True})
        response.headers["Clear-Site-Data"] = '"cache", "storage"'
        response.set_cookie(
            SESSION_COOKIE,
            "",
            max_age=0,
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return response

    async def _asset(self, request: web.Request) -> web.Response:
        self._validate_public_request(request, require_origin=False)
        asset = _ASSETS.get(request.path)
        if asset is None:
            raise web.HTTPNotFound()
        name, content_type = asset
        try:
            body = files("tfr").joinpath("web", name).read_bytes()
        except (FileNotFoundError, ModuleNotFoundError):
            raise web.HTTPNotFound() from None
        response = web.Response(body=body, headers={"Content-Type": content_type})
        response.headers["Cache-Control"] = "no-cache"
        return response

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        self._validate_public_request(request, require_origin=True)
        device = self._authenticate(request)
        if device is None:
            raise web.HTTPUnauthorized(text="Pair this device first")
        requested_gateway = request.query.get("gateway_id")
        after_cursor_value = request.query.get("after_cursor")
        try:
            after_cursor = int(after_cursor_value) if after_cursor_value is not None else None
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid history cursor") from None
        if after_cursor is not None and after_cursor < 0:
            raise web.HTTPBadRequest(text="Invalid history cursor")
        if after_cursor is not None and requested_gateway is None:
            raise web.HTTPBadRequest(text="History cursor requires a Gateway identity")
        history_reset = requested_gateway not in {None, str(self.runtime.gateway_id)}
        reserved = False
        async with self._session_lock:
            current = self._authenticate(request)
            if current is None or current.device_id != device.device_id:
                raise web.HTTPUnauthorized(text="Device session expired")
            now = time.monotonic()
            connection_times = self._connection_times[device.device_id]
            while connection_times and connection_times[0] <= now - 60:
                connection_times.popleft()
            if len(connection_times) >= MAX_WEB_CONNECTIONS_PER_MINUTE:
                raise web.HTTPTooManyRequests(text="Device connection rate limit reached")
            connection_times.append(now)
            if sum(self._connection_counts.values()) >= MAX_WEB_CLIENTS:
                raise web.HTTPServiceUnavailable(text="Web client limit reached")
            if self._connection_counts[device.device_id] >= MAX_WEB_CLIENTS_PER_DEVICE:
                raise web.HTTPServiceUnavailable(text="Device connection limit reached")
            self._connection_counts[device.device_id] += 1
            reserved = True
        try:
            subscription = await self.runtime.history.subscribe(
                None if history_reset else after_cursor,
                worlds=self._authorized_worlds(device),
                maximum_events=self.snapshot_events,
            )
        except ValueError as exc:
            async with self._session_lock:
                self._release_connection(device.device_id)
            raise web.HTTPBadRequest(text="History cursor is invalid or unavailable") from exc
        except BaseException:
            async with self._session_lock:
                self._release_connection(device.device_id)
            raise

        socket = web.WebSocketResponse(
            heartbeat=20,
            receive_timeout=60,
            max_msg_size=MAX_WEB_MESSAGE_BYTES,
            compress=False,
        )
        try:
            await socket.prepare(request)
            session_token = request.cookies.get(SESSION_COOKIE)
            async with self._session_lock:
                current = self.devices.authenticate(session_token)
                if current is None or current.device_id != device.device_id:
                    revoked = True
                else:
                    self._active_sockets[device.device_id].add(socket)
                    revoked = False
            if revoked:
                await self._close_bounded(
                    socket, code=1008, message=b"Device session revoked"
                )
            if socket.closed:
                return socket
            await self._send_initial_state(
                socket,
                subscription,
                device,
                session_token,
                history_reset=history_reset,
            )
            sender = asyncio.create_task(
                self._send_events(socket, subscription, device, session_token),
                name=f"tfr-web-events-{device.device_id}",
            )
            receiver = asyncio.create_task(
                self._receive_requests(socket, device, session_token),
                name=f"tfr-web-requests-{device.device_id}",
            )
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except (ConnectionError, RuntimeError, ValueError):
            await self._close_bounded(socket, code=1008, message=b"Web gateway error")
        finally:
            await self.runtime.history.unsubscribe(subscription)
            async with self._session_lock:
                sockets = self._active_sockets.get(device.device_id)
                if sockets is not None:
                    sockets.discard(socket)
                    if not sockets:
                        self._active_sockets.pop(device.device_id, None)
                if reserved:
                    self._release_connection(device.device_id)
        return socket

    def _release_connection(self, device_id: UUID) -> None:
        remaining = self._connection_counts.get(device_id, 0) - 1
        if remaining > 0:
            self._connection_counts[device_id] = remaining
        else:
            self._connection_counts.pop(device_id, None)

    async def _send_if_active(
        self,
        socket: web.WebSocketResponse,
        device: DeviceRecord,
        session_token: str | None,
        message: Mapping[str, Any],
        *,
        required_world: str | None = None,
    ) -> bool:
        async with self._session_lock:
            current = self.devices.authenticate(session_token)
            active = current is not None and current.device_id == device.device_id
            authorized = required_world is None or required_world in self._authorized_worlds(device)
        if not active:
            await self._close_bounded(socket, code=1008, message=b"Device session revoked")
            return False
        if not authorized:
            await self._close_bounded(socket, code=1008, message=b"World authorization changed")
            return False
        return await self._send_bounded(socket, message)

    @staticmethod
    async def _close_bounded(
        socket: web.WebSocketResponse,
        *,
        code: int,
        message: bytes,
    ) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                socket.close(code=code, message=message),
                timeout=WEB_WRITE_TIMEOUT_SECONDS,
            )

    @staticmethod
    async def _send_bounded(
        socket: web.WebSocketResponse,
        message: Mapping[str, Any],
    ) -> bool:
        serialized = json.dumps(dict(message), ensure_ascii=True)
        if len(serialized.encode("utf-8")) > MAX_WEB_MESSAGE_BYTES:
            await WebGatewayServer._close_bounded(
                socket, code=1009, message=b"Web message exceeds size limit"
            )
            return False
        try:
            await asyncio.wait_for(
                socket.send_str(serialized),
                timeout=WEB_WRITE_TIMEOUT_SECONDS,
            )
        except (ConnectionError, RuntimeError, TimeoutError):
            await WebGatewayServer._close_bounded(
                socket, code=1008, message=b"Web client too slow"
            )
            return False
        return True

    async def _send_initial_state(
        self,
        socket: web.WebSocketResponse,
        subscription: HistorySubscription,
        device: DeviceRecord,
        session_token: str | None,
        *,
        history_reset: bool,
    ) -> None:
        visible: list[tuple[str, dict[str, Any]]] = []
        snapshot_bytes = 0
        snapshot_source_bytes = 0
        snapshot_truncated = subscription.snapshot.truncated
        for item in reversed(subscription.snapshot.events):
            if item.event.world not in self._authorized_worlds(device):
                continue
            if not _browser_event_allowed(item.event):
                continue
            source_bytes = _browser_source_bytes(item.event)
            if snapshot_source_bytes + source_bytes > MAX_WEB_SNAPSHOT_SOURCE_BYTES:
                snapshot_truncated = True
                break
            projected = browser_event(item.cursor, item.event)
            if projected is None:
                continue
            projected_bytes = len(
                json.dumps(projected, ensure_ascii=True).encode("utf-8")
            )
            if snapshot_bytes + projected_bytes > MAX_WEB_SNAPSHOT_BYTES:
                snapshot_truncated = True
                break
            visible.append((item.event.world, projected))
            snapshot_bytes += projected_bytes
            snapshot_source_bytes += source_bytes
        visible.reverse()
        if not await self._send_if_active(
            socket,
            device,
            session_token,
            {
                "type": "hello",
                "protocol": WEB_PROTOCOL_VERSION,
                "gateway_id": str(self.runtime.gateway_id),
                "cursor": str(subscription.snapshot.cursor),
                "history_truncated": snapshot_truncated,
                "history_reset": history_reset,
                "worlds": [
                    {
                        "world": descriptor["world"],
                        "state": descriptor["state"],
                        "aliases": descriptor["aliases"],
                    }
                    for descriptor in self.runtime.world_descriptors()
                    if descriptor["world"] in self._authorized_worlds(device)
                ],
                "snapshot_count": len(visible),
            },
        ):
            return
        for world, message in visible:
            if not await self._send_if_active(
                socket,
                device,
                session_token,
                message,
                required_world=world,
            ):
                return
        await self._send_if_active(
            socket,
            device,
            session_token,
            {
                "type": "ready",
                "protocol": WEB_PROTOCOL_VERSION,
                "cursor": str(subscription.snapshot.cursor),
            },
        )

    async def _send_events(
        self,
        socket: web.WebSocketResponse,
        subscription: HistorySubscription,
        device: DeviceRecord,
        session_token: str | None,
    ) -> None:
        while True:
            item: SequencedEvent | None = await subscription.queue.get()
            if item is None:
                await self._send_if_active(
                    socket,
                    device,
                    session_token,
                    {
                        "type": "error",
                        "protocol": WEB_PROTOCOL_VERSION,
                        "message": "Event stream overflowed; reconnecting is required",
                    },
                )
                return
            if item.event.world not in self._authorized_worlds(device):
                await self._close_bounded(
                    socket, code=1008, message=b"World authorization changed"
                )
                return
            message = browser_event(item.cursor, item.event)
            if message is not None:
                if not await self._send_if_active(
                    socket,
                    device,
                    session_token,
                    message,
                    required_world=item.event.world,
                ):
                    return
            else:
                if not await self._send_if_active(
                    socket,
                    device,
                    session_token,
                    {
                        "type": "cursor",
                        "protocol": WEB_PROTOCOL_VERSION,
                        "cursor": str(item.cursor),
                    },
                ):
                    return

    async def _receive_requests(
        self,
        socket: web.WebSocketResponse,
        device: DeviceRecord,
        session_token: str | None,
    ) -> None:
        async for frame in socket:
            if frame.type is WSMsgType.TEXT:
                now = time.monotonic()
                frame_times = self._frame_times[device.device_id]
                while frame_times and frame_times[0] <= now - 60:
                    frame_times.popleft()
                if len(frame_times) >= MAX_WEB_FRAMES_PER_MINUTE:
                    await self._close_bounded(
                        socket, code=1008, message=b"Web message rate limit exceeded"
                    )
                    return
                frame_times.append(now)
                try:
                    value = json.loads(frame.data)
                except json.JSONDecodeError:
                    await self._send_error(socket, None, "Invalid JSON message")
                    continue
                if not isinstance(value, dict):
                    await self._send_error(socket, None, "Message must be an object")
                    continue
                current = self.devices.authenticate(session_token)
                if current is None or current.device_id != device.device_id:
                    await self._close_bounded(
                        socket, code=1008, message=b"Device session revoked"
                    )
                    return
                await self._handle_request(socket, device, value)
            elif frame.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                return

    async def _handle_request(
        self,
        socket: web.WebSocketResponse,
        device: DeviceRecord,
        message: dict[str, Any],
    ) -> None:
        message_type = message.get("type")
        request_id_value = message.get("request_id")
        try:
            request_id = UUID(str(request_id_value))
        except (TypeError, ValueError, AttributeError):
            await self._send_error(socket, None, "request_id must be a UUID")
            return
        if message_type == "ping":
            if set(message) != {"type", "request_id"}:
                await self._send_error(socket, request_id, "Ping has invalid fields")
                return
            await self._send_ack(socket, request_id, ok=True)
            return
        if message_type != "command":
            await self._send_error(socket, request_id, "Unsupported request type")
            return
        if set(message) != {"type", "request_id", "world", "text"}:
            await self._send_error(socket, request_id, "Command has invalid fields")
            return
        world = message.get("world")
        text = message.get("text")
        if not isinstance(world, str) or not isinstance(text, str):
            await self._send_error(socket, request_id, "Command world and text must be strings")
            return
        if not text or any(character in text for character in "\r\n\0"):
            await self._send_error(socket, request_id, "Command must be a non-empty single line")
            return
        if world not in self._authorized_worlds(device):
            await self._send_error(socket, request_id, "World is not authorized for this device")
            return
        await self._submit_command(socket, device, request_id, world, text)

    async def _submit_command(
        self,
        socket: web.WebSocketResponse,
        device: DeviceRecord,
        request_id: UUID,
        world: str,
        text: str,
    ) -> None:
        fingerprint = hashlib.sha256(
            json.dumps([world, text], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        async with self._command_locks[device.device_id]:
            history = self._command_history[device.device_id]
            previous = history.get(request_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    response = self._ack(
                        request_id,
                        ok=False,
                        error="request_id was already used for another command",
                    )
                else:
                    response = previous[1]
            else:
                now = time.monotonic()
                command_times = self._command_times[device.device_id]
                while command_times and command_times[0] <= now - 60:
                    command_times.popleft()
                if len(command_times) >= MAX_COMMANDS_PER_MINUTE:
                    response = self._ack(request_id, ok=False, error="Command rate limit exceeded")
                else:
                    command_times.append(now)
                    async with self._session_lock:
                        active = not (
                            self.devices.authenticate_by_id(device.device_id) is None
                            or world not in self._authorized_worlds(device)
                        )
                        if active:
                            try:
                                self.runtime.submit_command_nowait(
                                    world=world,
                                    text=text,
                                    client_id=f"web:{device.device_id}",
                                    request_id=request_id,
                                )
                            except UnknownSessionError:
                                response = self._ack(
                                    request_id,
                                    ok=False,
                                    error="World session is not accepting commands",
                                )
                            except (RuntimeError, ValueError) as exc:
                                response = self._ack(request_id, ok=False, error=str(exc))
                            else:
                                response = self._ack(request_id, ok=True)
                        else:
                            response = self._ack(
                                request_id,
                                ok=False,
                                error=(
                                    "Device session was revoked or world is no longer authorized"
                                ),
                            )
                history[request_id] = (fingerprint, response)
                history.move_to_end(request_id)
                while len(history) > MAX_IDEMPOTENCY_RECORDS:
                    history.popitem(last=False)
        await self._send_bounded(socket, response)

    @staticmethod
    def _ack(request_id: UUID, *, ok: bool, error: str | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {
            "type": "ack",
            "protocol": WEB_PROTOCOL_VERSION,
            "request_id": str(request_id),
            "ok": ok,
        }
        if error is not None:
            value["error"] = error
        return value

    async def _send_ack(
        self,
        socket: web.WebSocketResponse,
        request_id: UUID,
        *,
        ok: bool,
        error: str | None = None,
    ) -> None:
        await self._send_bounded(socket, self._ack(request_id, ok=ok, error=error))

    async def _send_error(
        self,
        socket: web.WebSocketResponse,
        request_id: UUID | None,
        message: str,
    ) -> None:
        if request_id is not None:
            await self._send_ack(socket, request_id, ok=False, error=message)
            return
        await self._send_bounded(
            socket,
            {"type": "error", "protocol": WEB_PROTOCOL_VERSION, "message": message}
        )
