from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import secrets
import ssl
import stat
import time
from collections import Counter, OrderedDict, defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import format_datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from aiohttp import WSMsgType, web

from tfr.ansi import BrowserTextSpan, ansi_visible_text, browser_text_spans, terminal_plain_text
from tfr.combo import Combo, ComboTracker
from tfr.core import UnknownSessionError
from tfr.diagnostics import DiagnosticBuffer
from tfr.events import Actor, ActorType, Confidence, Direction, Event, EventKind, Provenance
from tfr.gateway import GatewayRuntime, HistorySubscription, SequencedEvent
from tfr.gateway_devices import DeviceRecord, DeviceStore
from tfr.gateway_uis import UiRecord, UiStore
from tfr.multiline import PreparedMultiline, prepare_multiline_text
from tfr.plugins import PluginManager
from tfr.presentation import effect_programs_as_dict

WEB_PROTOCOL_VERSION = 1
SESSION_COOKIE = "__Host-tfr_session"
TAILSCALE_LOGIN_HEADER = "Tailscale-User-Login"
# Sentinel "identity" recorded for devices paired in direct transport, where
# there is no Tailscale Serve identity header. Device credentials are instead
# bound to possession of the per-device bearer token over authenticated TLS.
# Using a fixed value keeps DeviceStore.redeem/authenticate binding logic
# unchanged across both transports.
DIRECT_IDENTITY = "direct"
MAX_WEB_MESSAGE_BYTES = 131_072
MAX_WEB_EFFECT_DEMOS_BYTES = 131_072
MAX_COMMANDS_PER_MINUTE = 30
MAX_PAIRING_ATTEMPTS_PER_MINUTE = 20
MAX_GLOBAL_PAIRING_ATTEMPTS_PER_MINUTE = 100
MAX_IDEMPOTENCY_RECORDS = 256
MAX_WEB_CLIENTS = 8
MAX_WEB_CLIENTS_PER_DEVICE = 2
MAX_WEB_EVENT_TEXT_BYTES = 8_192
MAX_WEB_EVENT_SOURCE_BYTES = 32_768
MAX_WEB_TEXT_RUNS = 64
MAX_WEB_PROVENANCE_BYTES = 256
MAX_WEB_PROVENANCE_SOURCE_BYTES = 4_096
MAX_WEB_SNAPSHOT_BYTES = 2_097_152
MAX_WEB_SNAPSHOT_SOURCE_BYTES = 8_388_608
MAX_WEB_FRAMES_PER_MINUTE = 120
MAX_WEB_CONNECTIONS_PER_MINUTE = 12
MAX_WEB_DIAGNOSTICS_PER_MINUTE = 20
MAX_WEB_PASTE_SOURCE_BYTES = 65_536
MAX_WEB_PASTE_LINES = 100
MAX_WEB_PASTE_COMMAND_BYTES = 7_000
MAX_WEB_PASTE_STARTS_PER_MINUTE = 4
WEB_PASTE_DELAY_SECONDS = 0.5
WEB_WRITE_TIMEOUT_SECONDS = 5
TAILSCALE_COMMAND_TIMEOUT_SECONDS = 10
MAX_TAILSCALE_OUTPUT_BYTES = 1_048_576

_PORTABLE_SCREEN_CLEAR_EFFECTS = {
    "water_ripple": (2.6, 24.0),
    "doom_fire": (3.2, 24.0),
    "acid_rain": (3.0, 24.0),
    "film_burn": (4.2, 24.0),
    "flame": (2.1, 24.0),
    "sandstorm": (2.8, 24.0),
    "spotlight": (3.6, 24.0),
    "vortex": (2.8, 24.0),
    "water": (5.0, 30.0),
}

_PORTABLE_SCREEN_CLEAR_OPTIONS = {
    "acid_rain": ("density", "streak_length"),
    "film_burn": ("minimum_holes", "maximum_holes"),
    "sandstorm": ("direction", "gust_strength"),
    "spotlight": ("spotlight_count",),
    "vortex": ("direction", "turns"),
    "water": ("color_mode", "slosh_strength"),
}

_PWA_DIAGNOSTIC_CODES = {
    ("startup", "application_import_failed"): "The PWA application module failed to initialize",
    ("connection", "ready_timeout"): "The PWA Gateway connection did not become ready",
    ("connection", "websocket_closed"): "The PWA WebSocket closed unexpectedly",
    ("presentation", "gateway_omitted"): "The Gateway omitted a configured presentation",
    ("presentation", "palette_front_rejected"): "The PWA rejected a palette-front presentation",
    ("presentation", "hue_wave_rejected"): "The PWA rejected a hue-wave presentation",
    ("presentation", "presentation_schema_rejected"): "The PWA rejected a presentation schema",
}

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
    "/app.mjs": ("app.mjs", "text/javascript; charset=utf-8"),
    "/bootstrap.mjs": ("bootstrap.mjs", "text/javascript; charset=utf-8"),
    "/command.mjs": ("command.mjs", "text/javascript; charset=utf-8"),
    "/combo.mjs": ("combo.mjs", "text/javascript; charset=utf-8"),
    "/connection-lifecycle.mjs": (
        "connection-lifecycle.mjs",
        "text/javascript; charset=utf-8",
    ),
    "/diagnostics.mjs": ("diagnostics.mjs", "text/javascript; charset=utf-8"),
    "/event-details.mjs": ("event-details.mjs", "text/javascript; charset=utf-8"),
    "/effects-lab.mjs": ("effects-lab.mjs", "text/javascript; charset=utf-8"),
    "/history-notice.mjs": ("history-notice.mjs", "text/javascript; charset=utf-8"),
    "/linkify.mjs": ("linkify.mjs", "text/javascript; charset=utf-8"),
    "/motion.mjs": ("motion.mjs", "text/javascript; charset=utf-8"),
    "/pairing.mjs": ("pairing.mjs", "text/javascript; charset=utf-8"),
    "/paste.mjs": ("paste.mjs", "text/javascript; charset=utf-8"),
    "/presentation.mjs": ("presentation.mjs", "text/javascript; charset=utf-8"),
    "/scroll-restoration.mjs": ("scroll-restoration.mjs", "text/javascript; charset=utf-8"),
    "/screen-clear.mjs": ("screen-clear.mjs", "text/javascript; charset=utf-8"),
    "/swipe.mjs": ("swipe.mjs", "text/javascript; charset=utf-8"),
    "/text-runs.mjs": ("text-runs.mjs", "text/javascript; charset=utf-8"),
    "/transcript-events.mjs": ("transcript-events.mjs", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/repair.html": ("repair.html", "text/html; charset=utf-8"),
    "/repair.css": ("repair.css", "text/css; charset=utf-8"),
    "/repair.mjs": ("repair.mjs", "text/javascript; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


@dataclass(frozen=True, slots=True)
class WebProvisioningState:
    origin: str


@dataclass(slots=True)
class WebPasteTransfer:
    device_id: UUID
    request_id: UUID
    socket: web.WebSocketResponse
    world: str
    generation: int
    prepared: PreparedMultiline
    sent_lines: int = 0
    cancel_reason: str = "disconnected"
    task: asyncio.Task[None] | None = None


def _tailscale_origin(status: object) -> str:
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        raise ValueError("Tailscale is not running or logged in")
    self_status = status.get("Self")
    dns_name = self_status.get("DNSName") if isinstance(self_status, dict) else None
    if not isinstance(dns_name, str) or not dns_name.strip("."):
        raise ValueError("Tailscale did not report an HTTPS MagicDNS name")
    hostname = dns_name.rstrip(".")
    if any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
        for character in hostname
    ):
        raise ValueError("Tailscale reported an invalid HTTPS MagicDNS name")
    return f"https://{hostname}"


def _serve_root_proxy(status: object, origin: str) -> str | None:
    if not isinstance(status, dict):
        raise ValueError("Tailscale Serve returned invalid status")
    web_status = status.get("Web", {})
    if not isinstance(web_status, dict):
        raise ValueError("Tailscale Serve returned invalid web status")
    expected_host = urlsplit(origin).hostname
    roots: list[str] = []
    for address, service in web_status.items():
        if not isinstance(address, str) or not isinstance(service, dict):
            continue
        address_host = address.rsplit(":", 1)[0].strip("[]").rstrip(".")
        if address_host != expected_host:
            continue
        handlers = service.get("Handlers", {})
        if not isinstance(handlers, dict) or "/" not in handlers:
            continue
        handler = handlers["/"]
        proxy = handler.get("Proxy") if isinstance(handler, dict) else None
        roots.append(proxy if isinstance(proxy, str) else "<non-proxy handler>")
    if len(roots) > 1:
        raise ValueError("Tailscale Serve has multiple HTTPS root handlers for this device")
    return roots[0] if roots else None


class WebGatewayManager:
    """Lazily provisions and owns the private PWA listener."""

    def __init__(
        self,
        runtime: GatewayRuntime,
        *,
        configured_origin: str | None,
        host: str,
        port: int,
        state_directory: Path | str,
        snapshot_events: int,
        speaking_streaks_enabled: bool,
        ui_store: UiStore | None = None,
        transport: str = "tailscale",
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if transport not in {"tailscale", "direct"}:
            raise ValueError("web gateway transport must be 'tailscale' or 'direct'")
        if transport == "direct":
            if configured_origin is None:
                raise ValueError("direct transport requires a configured origin")
            if ssl_context is None:
                raise ValueError("direct transport requires a TLS context")
        self.runtime = runtime
        self.transport = transport
        self._ssl_context = ssl_context
        self.configured_origin = configured_origin
        self.host = host
        self.port = port
        self.state_directory = Path(state_directory).expanduser()
        self.snapshot_events = snapshot_events
        self.speaking_streaks_enabled = speaking_streaks_enabled
        self.ui_store = ui_store
        self._server: WebGatewayServer | None = None
        self._lock = asyncio.Lock()
        self._state_path = self.state_directory / "provisioning.json"
        self._stored_state = self._load_state()

    @property
    def origin(self) -> str | None:
        if self._server is not None:
            return self._server.origin
        if self.configured_origin is not None:
            return self.configured_origin
        return self._stored_state.origin if self._stored_state is not None else None

    @property
    def active(self) -> bool:
        return self._server is not None

    async def start_existing(self, *, eager: bool) -> None:
        origin = self.configured_origin if eager else self.origin
        if origin is not None:
            await self._start_server(origin)

    async def pair_device(self, label: str) -> dict[str, Any]:
        async with self._lock:
            if self.transport == "direct":
                return await self._pair_device_direct(label)
            origin = self.configured_origin or await self._discover_origin()
            started = self._server is None
            previous_state = self._stored_state
            await self._start_server(origin)
            try:
                assert self._server is not None
                pairing_url = self._server.create_pairing_url(label)
                target_host = f"[{self.host}]" if ":" in self.host else self.host
                target = f"http://{target_host}:{self.port}"
                serve_status = await self._tailscale_json("serve", "status", "--json")
                existing = _serve_root_proxy(serve_status, origin)
                configured = False
                if existing is None:
                    await self._tailscale("serve", "--bg", "--yes", target)
                    verified = _serve_root_proxy(
                        await self._tailscale_json("serve", "status", "--json"), origin
                    )
                    if verified != target:
                        raise ValueError(
                            "Tailscale Serve did not install the expected private route"
                        )
                    configured = True
                elif existing.rstrip("/") != target:
                    raise ValueError(
                        f"Tailscale Serve root is already used by {existing!r}; "
                        "TFR did not replace it"
                    )
                self._save_state(WebProvisioningState(origin=origin))
                return {
                    "pairing_url": pairing_url,
                    "origin": origin,
                    "setup": {
                        "backend_started": started,
                        "serve_configured": configured,
                    },
                }
            except BaseException:
                if started and previous_state is None:
                    await self.stop()
                raise

    async def _pair_device_direct(self, label: str) -> dict[str, Any]:
        """Pair a device when the Gateway terminates TLS itself.

        No Tailscale discovery or Serve configuration occurs: the origin is the
        configured ``direct_host``/``direct_port`` and the listener binds that
        address directly with the Gateway's TLS certificate.
        """
        assert self.configured_origin is not None
        origin = self.configured_origin
        started = self._server is None
        previous_state = self._stored_state
        await self._start_server(origin)
        try:
            assert self._server is not None
            pairing_url = self._server.create_pairing_url(label)
            self._save_state(WebProvisioningState(origin=origin))
            return {
                "pairing_url": pairing_url,
                "origin": origin,
                "setup": {
                    "backend_started": started,
                    "serve_configured": False,
                },
            }
        except BaseException:
            if started and previous_state is None:
                await self.stop()
            raise

    def status(self) -> dict[str, Any]:
        devices = self.device_descriptors()
        return {
            "enabled": True,
            "active": self.active,
            "origin": self.origin,
            "device_count": len(devices),
            "transport": self.transport,
        }

    def device_descriptors(self) -> list[dict[str, object]]:
        return self._server.device_descriptors() if self._server is not None else []

    async def revoke_device(self, device_id: UUID) -> bool:
        return await self._server.revoke_device(device_id) if self._server is not None else False

    def diagnostic_entries(self, category: str | None = None) -> list[dict[str, Any]]:
        return self._server.diagnostic_entries(category) if self._server is not None else []

    def clear_diagnostics(self) -> None:
        if self._server is not None:
            self._server.clear_diagnostics()

    async def notify_update(self, message: str) -> None:
        if self._server is not None:
            await self._server.notify_update(message)

    async def notify_ui_settings(self, settings: Mapping[str, bool]) -> None:
        if self._server is not None:
            await self._server.notify_ui_settings(settings)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await server.stop()

    async def _start_server(self, origin: str) -> None:
        if self._server is not None:
            if self._server.origin != origin.rstrip("/"):
                raise ValueError("the running PWA origin differs from the requested origin")
            return
        server = WebGatewayServer(
            self.runtime,
            origin=origin,
            host=self.host,
            port=self.port,
            state_directory=self.state_directory,
            snapshot_events=self.snapshot_events,
            speaking_streaks_enabled=self.speaking_streaks_enabled,
            ui_store=self.ui_store,
            transport=self.transport,
            ssl_context=self._ssl_context,
        )
        await server.start()
        self._server = server

    async def _discover_origin(self) -> str:
        return _tailscale_origin(await self._tailscale_json("status", "--json"))

    async def _tailscale_json(self, *arguments: str) -> object:
        output = await self._tailscale(*arguments)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise ValueError("Tailscale returned invalid JSON") from exc

    async def _tailscale(self, *arguments: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "tailscale",
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ValueError(
                "Tailscale is not installed; install and sign in before pairing"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=TAILSCALE_COMMAND_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ValueError("Tailscale did not respond in time") from exc
        if len(stdout) > MAX_TAILSCALE_OUTPUT_BYTES or len(stderr) > MAX_TAILSCALE_OUTPUT_BYTES:
            raise ValueError("Tailscale returned too much output")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()
            raise ValueError(
                f"Tailscale command failed: {detail[:500]}"
                if detail
                else "Tailscale command failed"
            )
        return stdout.decode("utf-8", "strict")

    def _load_state(self) -> WebProvisioningState | None:
        if not self.state_directory.exists() and not self.state_directory.is_symlink():
            return None
        try:
            descriptor = self._open_state(os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, encoding="utf-8") as state_file:
            try:
                value = json.load(state_file)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError(f"cannot read web provisioning state: {self._state_path}") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "origin"}:
            raise ValueError("web provisioning state has invalid fields")
        origin = value.get("origin")
        parsed = urlsplit(origin) if isinstance(origin, str) else None
        if (
            value.get("schema_version") != 1
            or parsed is None
            or parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("web provisioning state has an unsupported schema")
        return WebProvisioningState(origin=origin.rstrip("/"))

    def _save_state(self, state: WebProvisioningState) -> None:
        self._ensure_state_directory()
        if self._state_path.exists() or self._state_path.is_symlink():
            descriptor = self._open_state(os.O_RDONLY)
            os.close(descriptor)
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{secrets.token_hex(8)}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                descriptor = -1
                json.dump(
                    {"schema_version": 1, "origin": state.origin}, state_file, separators=(",", ":")
                )
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, self._state_path)
            if os.name == "posix":
                directory_descriptor = os.open(
                    self.state_directory,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            self._stored_state = state
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def _ensure_state_directory(self) -> None:
        self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        details = self.state_directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode):
            raise ValueError("web provisioning state parent is not a directory")
        if os.name == "posix" and (
            details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise ValueError(
                "web provisioning state directory must be private and owned by this user"
            )

    def _open_state(self, flags: int) -> int:
        self._ensure_state_directory()
        descriptor = os.open(
            self._state_path,
            flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or (
            os.name == "posix"
            and (details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077)
        ):
            os.close(descriptor)
            raise ValueError("web provisioning state must be a private regular file")
        return descriptor


def browser_event(
    cursor: int,
    event: Event,
    plugins: PluginManager | None = None,
    combo: Combo | None = None,
) -> dict[str, Any] | None:
    if not _browser_event_allowed(event):
        return None
    raw_text = _browser_event_source_text(event)
    source_text, source_truncated = _bounded_utf8(raw_text, MAX_WEB_EVENT_SOURCE_BYTES)
    spans = browser_text_spans(source_text)
    text = "".join(span.text for span in spans)
    encoded_text = text.encode("utf-8")
    text_truncated = source_truncated or len(encoded_text) > MAX_WEB_EVENT_TEXT_BYTES
    if text_truncated:
        text = _truncate_with_ellipsis(encoded_text, MAX_WEB_EVENT_TEXT_BYTES)
        spans = _truncate_browser_spans(spans, text)
    value: dict[str, Any] = {
        "type": "event",
        "protocol": WEB_PROTOCOL_VERSION,
        "cursor": str(cursor),
        "event": {
            "id": str(event.event_id),
            "timestamp": event.timestamp.isoformat().replace("+00:00", "Z"),
            "world": event.world,
            "connection_generation": str(event.connection_generation),
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
    if event.spoof is not None:
        value["event"]["spoof_status"] = event.spoof.status.value
        if event.spoof.reason is not None:
            value["event"]["spoof_reason"] = event.spoof.reason.value
        if event.spoof.suspected_sender is not None:
            value["event"]["spoof_sender"] = _bounded_visible_text(
                event.spoof.suspected_sender, MAX_WEB_PROVENANCE_BYTES
            )
            assert event.spoof.attribution_confidence is not None
            value["event"]["spoof_sender_confidence"] = event.spoof.attribution_confidence.value
    if combo is not None and 0 <= combo.body_start < combo.body_end <= len(text):
        value["event"]["combo"] = {
            "count": combo.count,
            "speaker": combo.speaker,
            "body_start": combo.body_start,
            "body_end": combo.body_end,
            "notice": combo.notice,
            "color": combo.color,
        }
    speaker_span = event.spoof.speaker_span if event.spoof is not None else None
    if speaker_span is not None and not (0 <= speaker_span[0] < speaker_span[1] <= len(text)):
        speaker_span = None
    text_runs = _browser_text_runs(spans, speaker_span)
    if text_runs is not None:
        value["event"]["text_runs"] = text_runs
    if plugins is not None:
        programs = plugins.presentation_programs(event, text)
        if programs:
            value["event"]["presentation"] = effect_programs_as_dict(programs)
    if event.kind is EventKind.CONNECTION:
        state = event.metadata.get("state")
        if isinstance(state, str):
            value["event"]["connection_state"] = state
        error = event.metadata.get("error")
        if isinstance(error, str):
            value["event"]["connection_error"] = _bounded_visible_text(
                error, MAX_WEB_PROVENANCE_BYTES
            )
        delay = event.metadata.get("delay_seconds")
        if isinstance(delay, (int, float)) and not isinstance(delay, bool):
            value["event"]["reconnect_delay_seconds"] = delay
    for optional_field in ("presentation", "combo", "text_runs", "provenance"):
        if len(json.dumps(value, ensure_ascii=True).encode("utf-8")) <= MAX_WEB_MESSAGE_BYTES:
            break
        value["event"].pop(optional_field, None)
    if len(json.dumps(value, ensure_ascii=True).encode("utf-8")) > MAX_WEB_MESSAGE_BYTES:
        for optional_field in (
            "spoof_sender",
            "spoof_sender_confidence",
            "spoof_reason",
            "spoof_status",
        ):
            value["event"].pop(optional_field, None)
    return value


def portable_screen_clear_config(runtime: GatewayRuntime) -> dict[str, Any]:
    effects: list[dict[str, Any]] = []
    for name in runtime.plugins.loaded_plugins:
        defaults = _PORTABLE_SCREEN_CLEAR_EFFECTS.get(name)
        if defaults is None:
            continue
        default_duration, default_fps = defaults
        configured = runtime.plugins.config.get(name, {})
        if not isinstance(configured, Mapping):
            configured = {}
        duration = configured.get("duration_seconds", default_duration)
        if name == "film_burn":
            duration = float(configured.get("minimum_growth_duration_seconds", 3.5)) + float(
                configured.get("burn_duration_seconds", 0.7)
            )
        frames_per_second = configured.get("frames_per_second", default_fps)
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or not 0 < duration <= 60
        ):
            duration = default_duration
        if (
            isinstance(frames_per_second, bool)
            or not isinstance(frames_per_second, (int, float))
            or not math.isfinite(frames_per_second)
            or not 1 <= frames_per_second <= 30
        ):
            frames_per_second = default_fps
        effect: dict[str, Any] = {
            "name": name,
            "duration_ms": round(float(duration) * 1000),
            "frames_per_second": float(frames_per_second),
        }
        option_names = _PORTABLE_SCREEN_CLEAR_OPTIONS.get(name, ())
        options = {option: configured[option] for option in option_names if option in configured}
        if options:
            effect["options"] = options
        effects.append(effect)
    mode = getattr(runtime, "screen_clear_mode", "cycle")
    locked = getattr(runtime, "screen_clear_effect", None)
    if mode not in {"cycle", "random", "locked"}:
        mode = "cycle"
    if mode == "locked" and not any(effect["name"] == locked for effect in effects):
        mode = "cycle"
        locked = None
    return {"mode": mode, "effect": locked, "effects": effects}


def _truncate_browser_spans(
    spans: tuple[BrowserTextSpan, ...],
    truncated_text: str,
) -> tuple[BrowserTextSpan, ...]:
    remaining = len(truncated_text) - 1
    output: list[BrowserTextSpan] = []
    for span in spans:
        if remaining <= 0:
            break
        fragment = span.text[:remaining]
        if fragment:
            output.append(replace(span, text=fragment))
            remaining -= len(fragment)
    output.append(BrowserTextSpan("…"))
    return tuple(output)


def _browser_text_runs(
    spans: tuple[BrowserTextSpan, ...],
    speaker_span: tuple[int, int] | None,
) -> list[dict[str, Any]] | None:
    if len(spans) > MAX_WEB_TEXT_RUNS:
        spans = (BrowserTextSpan("".join(span.text for span in spans)),)
    has_style = any(span.as_dict()["style"] for span in spans)
    if not has_style and speaker_span is None:
        return None
    speaker_start, speaker_end = speaker_span or (-1, -1)
    visible_offset = 0
    runs: list[dict[str, Any]] = []
    for span in spans:
        span_start = visible_offset
        span_end = span_start + len(span.text)
        boundaries = [span_start]
        boundaries.extend(
            boundary
            for boundary in (speaker_start, speaker_end)
            if span_start < boundary < span_end
        )
        boundaries.append(span_end)
        for start, end in zip(boundaries, boundaries[1:], strict=False):
            if start == end:
                continue
            run = span.as_dict()
            run["text"] = span.text[start - span_start : end - span_start]
            if speaker_start <= start and end <= speaker_end:
                run["role"] = "speaker"
            runs.append(run)
        visible_offset = span_end
    return runs


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
    raw_text = _browser_event_source_text(event)
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
    if event.spoof is not None and event.spoof.suspected_sender is not None:
        total += len(
            _bounded_utf8(event.spoof.suspected_sender, MAX_WEB_PROVENANCE_SOURCE_BYTES)[0].encode(
                "utf-8"
            )
        )
    return total


def _browser_event_source_text(event: Event) -> str:
    message_text = event.metadata.get("message_text")
    if event.provenance is not None and isinstance(message_text, str):
        for source in (event.display_text, event.canonical_text):
            if source is None:
                continue
            projected = ansi_visible_text(source, message_text)
            if projected is not None:
                return projected
        return message_text
    return event.display_text or event.plain_text or event.canonical_text or ""


def _browser_event_allowed(event: Event) -> bool:
    return _browser_event_class_allowed(
        event.kind,
        event.actor.type if event.actor is not None else None,
    )


def _browser_event_class_allowed(kind: EventKind, actor_type: ActorType | None) -> bool:
    return kind in _VISIBLE_EVENT_KINDS and not (
        kind is EventKind.COMMAND and actor_type is not ActorType.HUMAN
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
        speaking_streaks_enabled: bool = True,
        ui_store: UiStore | None = None,
        transport: str = "tailscale",
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if transport not in {"tailscale", "direct"}:
            raise ValueError("web gateway transport must be 'tailscale' or 'direct'")
        if transport == "tailscale":
            if host not in {"127.0.0.1", "::1"}:
                raise ValueError("web gateway must listen only on loopback")
            if ssl_context is not None:
                raise ValueError("tailscale transport terminates TLS at Tailscale Serve")
            if not 1 <= port <= 65_535:
                raise ValueError("web gateway port must be between 1 and 65535")
        else:
            # Direct transport: the Gateway terminates TLS itself and binds a
            # real local interface (for example a private VPC IP) or a wildcard.
            # Wildcard is permitted here because, unlike the native TFR-protocol
            # listener, the direct PWA relies on the host/cloud firewall as its
            # outer boundary. The public/NAT address a browser reaches is
            # direct_host and is never bound here.
            if not host.strip():
                raise ValueError("direct transport bind host cannot be empty")
            if not 1 <= port <= 65_535:
                raise ValueError("web gateway port must be between 1 and 65535")
            if ssl_context is None:
                raise ValueError("direct transport requires a TLS context")
        parsed_origin = urlsplit(origin)
        if parsed_origin.scheme != "https" or not parsed_origin.netloc:
            raise ValueError("web gateway origin must be an HTTPS origin")
        self.runtime = runtime
        self.transport = transport
        self._ssl_context = ssl_context
        self.origin = origin.rstrip("/")
        self.expected_host = parsed_origin.netloc
        self.websocket_origin = f"wss://{parsed_origin.netloc}"
        self.host = host
        self.port = port
        self.snapshot_events = snapshot_events
        self.speaking_streaks_enabled = speaking_streaks_enabled
        self.ui_store = ui_store
        self.devices = DeviceStore(state_directory, self.origin)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._active_sockets: dict[UUID, set[web.WebSocketResponse]] = defaultdict(set)
        self._connection_counts: dict[UUID, int] = defaultdict(int)
        self._command_history: dict[UUID, OrderedDict[UUID, tuple[str, dict[str, Any]]]] = (
            defaultdict(OrderedDict)
        )
        self._command_times: dict[UUID, deque[float]] = defaultdict(deque)
        self._command_locks: dict[UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._frame_times: dict[UUID, deque[float]] = defaultdict(deque)
        self._connection_times: dict[UUID, deque[float]] = defaultdict(deque)
        self._pairing_attempts: dict[str, deque[float]] = defaultdict(deque)
        self._global_pairing_attempts: deque[float] = deque()
        self._session_lock = asyncio.Lock()
        self.diagnostics = DiagnosticBuffer()
        self._diagnostic_times: dict[UUID, deque[float]] = defaultdict(deque)
        self._paste_transfers: dict[UUID, WebPasteTransfer] = {}
        self._paste_times: dict[UUID, deque[float]] = defaultdict(deque)
        self._paste_request_ids: dict[UUID, OrderedDict[UUID, None]] = defaultdict(OrderedDict)

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
        descriptors = self.devices.device_descriptors()
        for descriptor in descriptors:
            try:
                device_id = UUID(str(descriptor["device_id"]))
            except (KeyError, TypeError, ValueError, AttributeError):
                descriptor["active"] = False
            else:
                descriptor["active"] = bool(self._active_sockets.get(device_id))
            try:
                ui_id = UUID(str(descriptor["ui_id"]))
            except (KeyError, TypeError, ValueError, AttributeError):
                pass
            else:
                ui_record = self.ui_store.get(ui_id) if self.ui_store is not None else None
                if ui_record is not None:
                    descriptor["ui_name"] = ui_record.display_name
        return sorted(
            descriptors,
            key=lambda item: (bool(item["active"]), str(item.get("last_used_at", ""))),
            reverse=True,
        )

    def diagnostic_entries(self, category: str | None = None) -> list[dict[str, Any]]:
        entries = [*self.runtime.plugins.diagnostics.entries(), *self.diagnostics.entries()]
        if category == "web":
            entries = [
                entry for entry in entries if entry.component in {"web", "startup", "connection"}
            ]
        elif category == "presentations":
            entries = [entry for entry in entries if entry.component == "presentation"]
        elif category == "plugins":
            entries = [entry for entry in entries if entry.component == "plugin"]
        ordered = sorted(entries, key=lambda item: item.timestamp)[-100:]
        return [entry.as_dict() for entry in ordered]

    def clear_diagnostics(self) -> None:
        self.runtime.plugins.diagnostics.clear()
        self.diagnostics.clear()

    async def revoke_device(self, device_id: UUID) -> bool:
        async with self._session_lock:
            revoked = await self.devices.revoke(device_id)
            sockets = tuple(self._active_sockets.get(device_id, ()))
        if revoked:
            await self._cancel_paste(device_id, reason="revoked")
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
            site = web.TCPSite(runner, self.host, self.port, ssl_context=self._ssl_context)
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
                web.get("/pair", self._pair_redirect),
                web.post("/pair", self._pair_page),
                web.post("/api/logout", self._logout),
                web.post("/api/diagnostics", self._report_diagnostic),
                web.get("/ws", self._websocket),
                web.get(
                    "/{asset:index.html|app.mjs|bootstrap.mjs|command.mjs|connection-lifecycle.mjs|"
                    "diagnostics.mjs|event-details.mjs|effects-lab.mjs|history-notice.mjs|linkify.mjs|motion.mjs|pairing.mjs|paste.mjs|"
                    "presentation.mjs|scroll-restoration.mjs|screen-clear.mjs|combo.mjs|swipe.mjs|text-runs.mjs|"
                    "transcript-events.mjs|styles.css|manifest.webmanifest|"
                    "repair.html|repair.css|repair.mjs|sw.js|icon.svg|icon-512.png|"
                    "apple-touch-icon.png}",
                    self._asset,
                ),
                web.get("/", self._asset),
            ]
        )
        return application

    async def stop(self) -> None:
        await asyncio.gather(
            *(
                self._cancel_paste(device_id, reason="gateway_stopping")
                for device_id in tuple(self._paste_transfers)
            ),
            return_exceptions=True,
        )
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

    async def notify_update(self, message: str) -> None:
        await asyncio.gather(
            *(
                self._send_bounded(
                    socket,
                    {
                        "type": "maintenance",
                        "protocol": WEB_PROTOCOL_VERSION,
                        "message": message,
                    },
                )
                for sockets in tuple(self._active_sockets.values())
                for socket in tuple(sockets)
            ),
            return_exceptions=True,
        )

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

    def _validate_public_request(
        self,
        request: web.Request,
        *,
        require_origin: bool,
        allow_missing_origin: bool = False,
    ) -> None:
        hosts = request.headers.getall("Host", [])
        if hosts != [self.expected_host]:
            raise web.HTTPForbidden(text="Unexpected host")
        origins = request.headers.getall("Origin", [])
        if require_origin and origins != [self.origin]:
            raise web.HTTPForbidden(text="Unexpected origin")
        if allow_missing_origin and origins not in ([], [self.origin], ["null"]):
            raise web.HTTPForbidden(text="Unexpected origin")
        self._request_identity(request)

    def _request_identity(self, request: web.Request) -> str:
        """The identity a device credential is bound to for this request.

        In tailscale transport this is the Tailscale Serve identity header,
        which is required. In direct transport there is no such header, so a
        fixed sentinel is used and the credential is protected by the bearer
        token over authenticated TLS instead.
        """
        if self.transport == "direct":
            return DIRECT_IDENTITY
        return self._tailscale_login(request)

    @staticmethod
    def _tailscale_login(request: web.Request) -> str:
        values = request.headers.getall(TAILSCALE_LOGIN_HEADER, [])
        if len(values) != 1 or not 1 <= len(values[0]) <= 320 or not values[0].isprintable():
            raise web.HTTPForbidden(text="Tailscale Serve identity is required")
        return values[0]

    def _authenticate(self, request: web.Request) -> DeviceRecord | None:
        device = self.devices.authenticate(request.cookies.get(SESSION_COOKIE))
        if device is None or device.tailscale_login != self._request_identity(request):
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
        token = request.cookies.get(SESSION_COOKIE)
        refreshed = False
        if device is not None:
            device, refreshed = await self.devices.mark_used(device.device_id)
        ui_record = await self._register_pwa_ui(device) if device is not None else None
        session_value: dict[str, Any] = {
            "paired": device is not None,
            "device": (
                {"id": str(device.device_id), "label": device.label, "scope": device.scope}
                if device is not None
                else None
            ),
        }
        if ui_record is not None:
            session_value["ui"] = self._ui_descriptor(ui_record)
        response = self._json_response(session_value)
        if device is not None and refreshed and token is not None:
            self._set_session_cookie(response, token, device)
        return response

    async def _report_diagnostic(self, request: web.Request) -> web.Response:
        self._validate_public_request(request, require_origin=True)
        device = self._authenticate(request)
        if device is None:
            raise web.HTTPUnauthorized(text="Pair this device first")
        now = time.monotonic()
        attempts = self._diagnostic_times[device.device_id]
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= MAX_WEB_DIAGNOSTICS_PER_MINUTE:
            raise web.HTTPTooManyRequests(text="Diagnostic report rate limit reached")
        attempts.append(now)
        try:
            value = await request.json(loads=json.loads)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid diagnostic report") from None
        if not isinstance(value, dict) or set(value) - {
            "severity",
            "component",
            "code",
            "operation",
            "event_id",
            "shell_version",
        }:
            raise web.HTTPBadRequest(text="Invalid diagnostic report")
        component = value.get("component")
        code = value.get("code")
        safe_message = _PWA_DIAGNOSTIC_CODES.get((component, code))
        if safe_message is None:
            raise web.HTTPBadRequest(text="Unsupported diagnostic code")
        try:
            ui_record = self.ui_store.get(device.ui_id) if self.ui_store is not None else None
            self.diagnostics.record(
                severity=value.get("severity", "warning"),
                component=component,
                code=code,
                message=safe_message,
                operation=value.get("operation"),
                client="pwa",
                event_id=value.get("event_id"),
                device_id_suffix=str(device.device_id)[-8:],
                shell_version=value.get("shell_version"),
                ui_name=ui_record.display_name if ui_record is not None else None,
            )
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid diagnostic report") from None
        return self._json_response({"accepted": True})

    def _record_pairing_attempt(self, rate_limit_key: str) -> bool:
        now = time.monotonic()
        for identity, identity_attempts in tuple(self._pairing_attempts.items()):
            while identity_attempts and identity_attempts[0] <= now - 60:
                identity_attempts.popleft()
            if not identity_attempts:
                self._pairing_attempts.pop(identity, None)
        while self._global_pairing_attempts and self._global_pairing_attempts[0] <= now - 60:
            self._global_pairing_attempts.popleft()
        attempts = self._pairing_attempts.get(rate_limit_key)
        if (attempts is not None and len(attempts) >= MAX_PAIRING_ATTEMPTS_PER_MINUTE) or len(
            self._global_pairing_attempts
        ) >= MAX_GLOBAL_PAIRING_ATTEMPTS_PER_MINUTE:
            return False
        if attempts is None:
            attempts = self._pairing_attempts[rate_limit_key]
        attempts.append(now)
        self._global_pairing_attempts.append(now)
        return True

    def _pairing_identity(self, request: web.Request) -> tuple[str, str]:
        """Return (bound_identity, rate_limit_key) for a pairing request.

        ``bound_identity`` is persisted on the device credential. In direct
        transport it is a fixed sentinel, so per-identity pairing rate limiting
        would collapse into one shared bucket; the client IP is used as the
        rate-limit key instead. In tailscale transport both are the Serve
        identity header.
        """
        if self.transport == "direct":
            return DIRECT_IDENTITY, (request.remote or "unknown")
        identity = self._tailscale_login(request)
        return identity, identity

    @staticmethod
    def _set_session_cookie(
        response: web.StreamResponse,
        token: str,
        device: DeviceRecord,
    ) -> None:
        remaining_seconds = max(0, int((device.expires_at - datetime.now(UTC)).total_seconds()))
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=remaining_seconds,
            expires=format_datetime(device.expires_at.astimezone(UTC), usegmt=True),
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/",
        )

    async def _pair(self, request: web.Request) -> web.Response:
        self._validate_public_request(request, require_origin=True)
        bound_identity, rate_limit_key = self._pairing_identity(request)
        if not self._record_pairing_attempt(rate_limit_key):
            return self._json_response({"error": "Pairing rate limit exceeded"}, status=429)
        try:
            value = await request.json(loads=json.loads)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json_response({"error": "Invalid JSON"}, status=400)
        if (
            not isinstance(value, dict)
            or not {"code"} <= set(value) <= {"code", "retry_id"}
            or not isinstance(value.get("code"), str)
            or ("retry_id" in value and not isinstance(value["retry_id"], str))
        ):
            return self._json_response({"error": "Invalid pairing request"}, status=400)
        try:
            device, token = await self.devices.redeem(
                value["code"],
                bound_identity,
            )
        except ValueError as exc:
            return self._json_response({"error": str(exc)}, status=401)
        ui_record = await self._register_pwa_ui(device)
        paired_value: dict[str, Any] = {
            "paired": True,
            "device": {"id": str(device.device_id), "label": device.label},
        }
        if ui_record is not None:
            paired_value["ui"] = self._ui_descriptor(ui_record)
        response = self._json_response(paired_value)
        self._set_session_cookie(response, token, device)
        return response

    async def _pair_redirect(self, request: web.Request) -> web.Response:
        self._validate_public_request(request, require_origin=False)
        return web.Response(
            status=303,
            headers={"Location": "/", "Cache-Control": "no-store"},
        )

    async def _pair_page(self, request: web.Request) -> web.Response:
        self._validate_public_request(
            request,
            require_origin=False,
            allow_missing_origin=True,
        )
        bound_identity, rate_limit_key = self._pairing_identity(request)
        if not self._record_pairing_attempt(rate_limit_key):
            return web.Response(
                status=303,
                headers={
                    "Location": "/?pairing=retry",
                    "Cache-Control": "no-store",
                    "Retry-After": "5",
                },
            )
        if request.content_type != "application/x-www-form-urlencoded":
            return web.Response(text="Invalid pairing request", status=400)
        try:
            value = await request.post()
        except (LookupError, ValueError):
            return web.Response(text="Invalid pairing request", status=400)
        codes = value.getall("code", [])
        if set(value) != {"code"} or len(codes) != 1 or not isinstance(codes[0], str):
            return web.Response(text="Invalid pairing request", status=400)
        try:
            device, token = await self.devices.redeem(codes[0], bound_identity)
        except ValueError:
            return web.Response(
                status=303,
                headers={"Location": "/?pairing=invalid", "Cache-Control": "no-store"},
            )
        await self._register_pwa_ui(device)
        response = web.Response(
            status=303,
            headers={"Location": "/?pairing=complete", "Cache-Control": "no-store"},
        )
        self._set_session_cookie(response, token, device)
        return response

    async def _register_pwa_ui(self, device: DeviceRecord) -> UiRecord | None:
        if self.ui_store is None:
            return None
        return await self.ui_store.register(
            ui_id=device.ui_id,
            client_type="pwa",
            platform=device.label,
            versions={"web_protocol": str(WEB_PROTOCOL_VERSION)},
            capabilities=(),
        )

    @staticmethod
    def _ui_descriptor(record: UiRecord | None) -> dict[str, str] | None:
        if record is None:
            return None
        return {"id": str(record.ui_id), "name": record.display_name}

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
                maximum_events_per_world=self.snapshot_events,
                event_filter=_browser_event_allowed,
                event_class_filter=_browser_event_class_allowed,
                maximum_source_bytes=MAX_WEB_SNAPSHOT_SOURCE_BYTES,
                event_source_bytes=_browser_source_bytes,
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
        session_token = request.cookies.get(SESSION_COOKIE)
        refreshed, cookie_changed = await self.devices.mark_used(device.device_id)
        if refreshed is not None:
            device = refreshed
        if cookie_changed and session_token is not None:
            self._set_session_cookie(socket, session_token, device)
        try:
            await socket.prepare(request)
            async with self._session_lock:
                current = self.devices.authenticate(session_token)
                if current is None or current.device_id != device.device_id:
                    revoked = True
                else:
                    self._active_sockets[device.device_id].add(socket)
                    revoked = False
            if revoked:
                await self._close_bounded(socket, code=1008, message=b"Device session revoked")
            if socket.closed:
                return socket
            await self._send_initial_state(
                socket,
                subscription,
                device,
                session_token,
                history_reset=history_reset,
                combo_tracker=(combo_tracker := ComboTracker()),
            )
            sender = asyncio.create_task(
                self._send_events(socket, subscription, device, session_token, combo_tracker),
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
            await self._cancel_paste(
                device.device_id,
                reason="disconnected",
                socket=socket,
            )
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

    async def notify_ui_settings(self, settings: Mapping[str, bool]) -> None:
        message = {
            "type": "ui_settings",
            "protocol": WEB_PROTOCOL_VERSION,
            "speaking_streaks_enabled": settings["speaking_streaks_enabled"],
        }
        sockets = tuple(
            socket for device_sockets in self._active_sockets.values() for socket in device_sockets
        )
        await asyncio.gather(
            *(self._send_bounded(socket, message) for socket in sockets),
            return_exceptions=True,
        )

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
            await WebGatewayServer._close_bounded(socket, code=1008, message=b"Web client too slow")
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
        combo_tracker: ComboTracker,
    ) -> None:
        descriptors = {
            str(descriptor["world"]): descriptor for descriptor in self.runtime.world_descriptors()
        }
        authorized = self._authorized_worlds(device)
        for item in subscription.snapshot.events:
            event = item.event
            if event.world not in authorized or not _browser_event_allowed(event):
                continue
            descriptor = descriptors.get(event.world, {})
            combo_tracker.observe(
                event,
                terminal_plain_text(_browser_event_source_text(event)),
                event.timestamp.timestamp(),
                character_name=descriptor.get("character"),
            )
        visible: list[tuple[str, dict[str, Any]]] = []
        visible_counts: dict[tuple[str, int], int] = defaultdict(int)
        selected_counts: Counter[tuple[str, int]] = Counter(
            (item.event.world, item.event.connection_generation)
            for item in subscription.snapshot.events
        )
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
            projected = browser_event(item.cursor, item.event, self.runtime.plugins)
            if projected is None:
                continue
            projected_bytes = len(json.dumps(projected, ensure_ascii=True).encode("utf-8"))
            if snapshot_bytes + projected_bytes > MAX_WEB_SNAPSHOT_BYTES:
                snapshot_truncated = True
                break
            visible.append((item.event.world, projected))
            visible_counts[(item.event.world, item.event.connection_generation)] += 1
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
                "build": self.runtime.build.as_dict(),
                "cursor": str(subscription.snapshot.cursor),
                "history_truncated": snapshot_truncated,
                "history_reset": history_reset,
                "worlds": [
                    self._browser_world_descriptor(
                        descriptor,
                        subscription,
                        visible_counts,
                        selected_counts,
                    )
                    for descriptor in self.runtime.world_descriptors()
                    if descriptor["world"] in self._authorized_worlds(device)
                ],
                "effect_demos": self._effect_demos(device),
                "screen_clear": portable_screen_clear_config(self.runtime),
                "speaking_streaks_enabled": (
                    self.runtime.shared_ui_settings()["speaking_streaks_enabled"]
                    if hasattr(self.runtime, "shared_ui_settings")
                    else self.speaking_streaks_enabled
                ),
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

    def _effect_demos(self, device: DeviceRecord) -> list[dict[str, Any]]:
        worlds = sorted(self._authorized_worlds(device))
        if not worlds:
            return []
        demos = self.runtime.plugins.effect_demos()[:100]
        if not demos:
            return []
        output = []
        encoded_bytes = 2
        for demo in demos:
            world = next(
                (candidate for candidate in demo.worlds if candidate in worlds),
                worlds[0] if not demo.worlds else None,
            )
            if world is None:
                continue
            session = self.runtime.session_for(world)
            samples = []
            for sample in demo.samples:
                kind, text = sample.kind, sample.text
                event = Event(
                    session_id=session.session_id,
                    world=world,
                    connection_generation=session.connection_generation,
                    sequence=0,
                    direction=Direction.INBOUND,
                    kind=kind,
                    event_id=uuid4(),
                    timestamp=datetime.now(UTC),
                    canonical_text=text,
                    plain_text=text,
                    display_text=text,
                    parser_name="effects-lab",
                    confidence=Confidence.INFERRED,
                    provenance=Provenance(
                        sender_name=demo.label if demo.category == "speaker" else None,
                        confidence=Confidence.INFERRED,
                    ),
                )
                projected = browser_event(0, event, self.runtime.plugins)
                if projected is not None:
                    samples.append(projected["event"])
            descriptor = {
                "id": demo.id,
                "label": demo.label,
                "category": demo.category,
                "samples": samples,
                "worlds": list(demo.worlds),
            }
            descriptor_bytes = len(json.dumps(descriptor, ensure_ascii=True).encode("utf-8"))
            if encoded_bytes + descriptor_bytes > MAX_WEB_EFFECT_DEMOS_BYTES:
                break
            output.append(descriptor)
            encoded_bytes += descriptor_bytes
        return output

    @staticmethod
    def _browser_world_descriptor(
        descriptor: Mapping[str, Any],
        subscription: HistorySubscription,
        visible_counts: Mapping[tuple[str, int], int],
        selected_counts: Mapping[tuple[str, int], int],
    ) -> dict[str, Any]:
        world = str(descriptor["world"])
        generation = int(descriptor.get("connection_generation", 0))
        key = (world, generation)
        return {
            "world": world,
            "state": descriptor["state"],
            "aliases": descriptor["aliases"],
            "server": descriptor["server"],
            "character": descriptor.get("character"),
            "connection_generation": str(generation),
            "history": {
                "connection_generation": str(generation),
                "available_count": str(subscription.snapshot.available_counts.get(key, 0)),
                "snapshot_count": visible_counts.get(key, 0),
                "snapshot_gap": key in subscription.snapshot.after_cursor_gaps
                or (
                    subscription.snapshot.resumed
                    and selected_counts.get(key, 0) > visible_counts.get(key, 0)
                ),
            },
        }

    async def _send_events(
        self,
        socket: web.WebSocketResponse,
        subscription: HistorySubscription,
        device: DeviceRecord,
        session_token: str | None,
        combo_tracker: ComboTracker,
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
                await self._close_bounded(socket, code=1008, message=b"World authorization changed")
                return
            descriptor = next(
                (
                    value
                    for value in self.runtime.world_descriptors()
                    if value["world"] == item.event.world
                ),
                {},
            )
            combo = combo_tracker.observe(
                item.event,
                terminal_plain_text(_browser_event_source_text(item.event)),
                item.event.timestamp.timestamp(),
                character_name=descriptor.get("character"),
            )
            message = browser_event(item.cursor, item.event, self.runtime.plugins, combo=combo)
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
                    await self._close_bounded(socket, code=1008, message=b"Device session revoked")
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
        if message_type == "paste":
            if set(message) != {"type", "request_id", "world", "text"}:
                await self._send_error(socket, request_id, "Paste has invalid fields")
                return
            world = message.get("world")
            text = message.get("text")
            if not isinstance(world, str) or not isinstance(text, str):
                await self._send_error(socket, request_id, "Paste world and text must be strings")
                return
            if world not in self._authorized_worlds(device):
                await self._send_error(
                    socket, request_id, "World is not authorized for this device"
                )
                return
            await self._start_paste(socket, device, request_id, world, text)
            return
        if message_type == "cancel_paste":
            if set(message) != {"type", "request_id"}:
                await self._send_error(socket, request_id, "Paste cancellation has invalid fields")
                return
            transfer = self._paste_transfers.get(device.device_id)
            if (
                transfer is None
                or transfer.request_id != request_id
                or transfer.socket is not socket
            ):
                await self._send_error(socket, request_id, "Paste transfer is not active")
                return
            await self._cancel_paste(device.device_id, reason="user", socket=socket)
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

    def _world_descriptor(self, world: str) -> Mapping[str, Any] | None:
        return next(
            (
                descriptor
                for descriptor in self.runtime.world_descriptors()
                if descriptor["world"] == world
            ),
            None,
        )

    async def _start_paste(
        self,
        socket: web.WebSocketResponse,
        device: DeviceRecord,
        request_id: UUID,
        world: str,
        text: str,
    ) -> None:
        descriptor = self._world_descriptor(world)
        if descriptor is None or descriptor.get("state") != "connected":
            await self._send_error(
                socket, request_id, "World session is not accepting pasted lines"
            )
            return
        try:
            prepared = prepare_multiline_text(
                text,
                server=str(descriptor.get("server", "")),
                encoding=str(descriptor.get("encoding", "utf-8")),
                source="PWA paste",
                maximum_source_bytes=MAX_WEB_PASTE_SOURCE_BYTES,
                maximum_lines=MAX_WEB_PASTE_LINES,
                maximum_command_bytes=MAX_WEB_PASTE_COMMAND_BYTES,
            )
        except ValueError as exc:
            await self._send_error(socket, request_id, str(exc))
            return
        if not prepared.commands:
            await self._send_error(socket, request_id, "Paste must contain at least one line")
            return
        async with self._session_lock:
            current = self.devices.authenticate_by_id(device.device_id)
            if current is None or world not in self._authorized_worlds(current):
                error = "Device session was revoked or world is no longer authorized"
            elif (
                request_id in self._command_history[device.device_id]
                or request_id in self._paste_request_ids[device.device_id]
            ):
                error = "request_id was already used"
            elif device.device_id in self._paste_transfers:
                error = "A paste transfer is already active for this device"
            else:
                now = time.monotonic()
                starts = self._paste_times[device.device_id]
                while starts and starts[0] <= now - 60:
                    starts.popleft()
                if len(starts) >= MAX_WEB_PASTE_STARTS_PER_MINUTE:
                    error = "Paste transfer rate limit exceeded"
                else:
                    starts.append(now)
                    transfer = WebPasteTransfer(
                        device_id=device.device_id,
                        request_id=request_id,
                        socket=socket,
                        world=world,
                        generation=int(descriptor.get("connection_generation", 0)),
                        prepared=prepared,
                    )
                    self._paste_transfers[device.device_id] = transfer
                    paste_ids = self._paste_request_ids[device.device_id]
                    paste_ids[request_id] = None
                    while len(paste_ids) > MAX_IDEMPOTENCY_RECORDS:
                        paste_ids.popitem(last=False)
                    error = None
        if error is not None:
            await self._send_error(socket, request_id, error)
            return
        if not await self._send_bounded(
            socket,
            {
                "type": "paste_started",
                "protocol": WEB_PROTOCOL_VERSION,
                "request_id": str(request_id),
                "total_lines": prepared.line_count,
            },
        ):
            await self._cancel_paste(device.device_id, reason="disconnected", socket=socket)
            return
        transfer.task = asyncio.create_task(
            self._run_paste(transfer),
            name=f"tfr-web-paste-{device.device_id}",
        )

    def _paste_status(self, transfer: WebPasteTransfer) -> str | None:
        current = self.devices.authenticate_by_id(transfer.device_id)
        if current is None:
            return "revoked"
        active_sockets = self._active_sockets.get(transfer.device_id, ())
        if transfer.socket.closed or transfer.socket not in active_sockets:
            return "disconnected"
        if transfer.world not in self._authorized_worlds(current):
            return "revoked"
        descriptor = self._world_descriptor(transfer.world)
        if descriptor is None or descriptor.get("state") != "connected":
            return "world_disconnected"
        if int(descriptor.get("connection_generation", 0)) != transfer.generation:
            return "world_changed"
        return None

    async def _run_paste(self, transfer: WebPasteTransfer) -> None:
        completed = False
        reason = "submission_failed"
        try:
            await asyncio.sleep(0)
            for line_number, command in enumerate(transfer.prepared.commands, start=1):
                if line_number > 1:
                    await asyncio.sleep(WEB_PASTE_DELAY_SECONDS)
                if status := self._paste_status(transfer):
                    reason = status
                    break
                try:
                    device = self.devices.authenticate_by_id(transfer.device_id)
                    ui_record = (
                        self.ui_store.get(device.ui_id)
                        if self.ui_store is not None and device is not None
                        else None
                    )
                    self.runtime.submit_command_nowait(
                        world=transfer.world,
                        text=command,
                        client_id=f"web:{transfer.device_id}",
                        request_id=uuid4(),
                        actor=Actor(ActorType.HUMAN, f"web:{transfer.device_id}"),
                        correlation_id=transfer.request_id,
                        expected_connection_generation=transfer.generation,
                        metadata={
                            "paste_request_id": str(transfer.request_id),
                            "paste_line": line_number,
                            "paste_total_lines": transfer.prepared.line_count,
                        },
                        ui_id=device.ui_id if device is not None else None,
                        ui_name=ui_record.display_name if ui_record is not None else None,
                    )
                except (UnknownSessionError, RuntimeError, ValueError):
                    reason = "submission_failed"
                    break
                transfer.sent_lines = line_number
                if not await self._send_bounded(
                    transfer.socket,
                    {
                        "type": "paste_progress",
                        "protocol": WEB_PROTOCOL_VERSION,
                        "request_id": str(transfer.request_id),
                        "sent_lines": transfer.sent_lines,
                        "total_lines": transfer.prepared.line_count,
                    },
                ):
                    reason = "disconnected"
                    break
            else:
                completed = True
        except asyncio.CancelledError:
            reason = transfer.cancel_reason
        finally:
            current = self._paste_transfers.get(transfer.device_id)
            if current is transfer:
                self._paste_transfers.pop(transfer.device_id, None)
            message = {
                "type": "paste_complete" if completed else "paste_cancelled",
                "protocol": WEB_PROTOCOL_VERSION,
                "request_id": str(transfer.request_id),
                "sent_lines": transfer.sent_lines,
                "total_lines": transfer.prepared.line_count,
            }
            if not completed:
                message["reason"] = reason
            await self._send_bounded(transfer.socket, message)

    async def _cancel_paste(
        self,
        device_id: UUID,
        *,
        reason: str,
        socket: web.WebSocketResponse | None = None,
    ) -> bool:
        transfer = self._paste_transfers.get(device_id)
        if transfer is None or (socket is not None and transfer.socket is not socket):
            return False
        transfer.cancel_reason = reason
        task = transfer.task
        if task is None:
            self._paste_transfers.pop(device_id, None)
            return True
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return True

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
            if request_id in self._paste_request_ids[device.device_id]:
                response = self._ack(
                    request_id,
                    ok=False,
                    error="request_id was already used for a paste transfer",
                )
            elif previous is not None:
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
                                ui_record = (
                                    self.ui_store.get(device.ui_id)
                                    if self.ui_store is not None
                                    else None
                                )
                                self.runtime.submit_command_nowait(
                                    world=world,
                                    text=text,
                                    client_id=f"web:{device.device_id}",
                                    request_id=request_id,
                                    ui_id=device.ui_id,
                                    ui_name=(
                                        ui_record.display_name if ui_record is not None else None
                                    ),
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
            socket, {"type": "error", "protocol": WEB_PROTOCOL_VERSION, "message": message}
        )
