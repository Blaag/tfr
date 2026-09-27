from __future__ import annotations

import asyncio
import time
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from aiohttp import WSMsgType
from aiohttp.client_exceptions import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

import tfr.gateway_web as gateway_web
from tfr.core import EventBus, UnknownSessionError
from tfr.events import (
    Actor,
    ActorType,
    Confidence,
    Direction,
    Event,
    EventKind,
    Provenance,
    SpoofAssessment,
    SpoofReason,
    SpoofStatus,
)
from tfr.gateway import EventHistory
from tfr.gateway_web import SESSION_COOKIE, WebGatewayServer, browser_event
from tfr.updates import BuildIdentity

ORIGIN = "https://gateway.example.ts.net"
PUBLIC_HEADERS = {
    "Host": "gateway.example.ts.net",
    "Origin": ORIGIN,
    "Tailscale-User-Login": "black@example.com",
}
NAVIGATION_HEADERS = {
    "Host": "gateway.example.ts.net",
    "Tailscale-User-Login": "black@example.com",
}


def test_all_pwa_assets_are_in_the_python_package() -> None:
    asset_directory = files("tfr").joinpath("web")
    assert {
        "index.html",
        "app.mjs",
        "command.mjs",
        "event-details.mjs",
        "history-notice.mjs",
        "linkify.mjs",
        "pairing.mjs",
        "swipe.mjs",
        "styles.css",
        "manifest.webmanifest",
        "sw.js",
        "icon.svg",
        "icon-512.png",
        "apple-touch-icon.png",
    } <= {asset.name for asset in asset_directory.iterdir()}


def test_pwa_hidden_state_overrides_layout_display() -> None:
    styles = files("tfr").joinpath("web", "styles.css").read_text(encoding="utf-8")
    assert "[hidden]" in styles
    assert "display: none !important" in styles


def test_pwa_shell_uses_explicit_grid_rows() -> None:
    styles = files("tfr").joinpath("web", "styles.css").read_text(encoding="utf-8")
    for selector, row in (
        (".topbar", 1),
        (".history-notice", 2),
        (".transcript", 3),
        (".composer", 4),
    ):
        start = styles.index(f"{selector} {{")
        end = styles.index("}", start)
        assert f"grid-row: {row};" in styles[start:end]


def test_pwa_header_uses_one_shared_font_size() -> None:
    styles = files("tfr").joinpath("web", "styles.css").read_text(encoding="utf-8")

    topbar_start = styles.index(".topbar {")
    topbar_end = styles.index("}", topbar_start)
    assert "font-size: 0.82rem;" in styles[topbar_start:topbar_end]
    for selector in (".settings-button {", ".settings-glyph {", ".active-world strong {"):
        start = styles.index(selector)
        end = styles.index("}", start)
        assert "font-size: inherit;" in styles[start:end]
    connection_start = styles.index(".connection-state {")
    connection_end = styles.index("}", connection_start)
    assert "font-size: inherit;" in styles[connection_start:connection_end]


def test_transcript_rows_use_compact_terminal_spacing() -> None:
    styles = files("tfr").joinpath("web", "styles.css").read_text(encoding="utf-8")

    event_start = styles.index(".event {")
    event_end = styles.index("}", event_start)
    text_start = styles.index(".event-text {")
    text_end = styles.index("}", text_start)
    assert "padding: 0.06rem 0.9rem 0.08rem 0.7rem;" in styles[event_start:event_end]
    assert "line-height: 1.25;" in styles[text_start:text_end]


def test_pwa_line_wrap_setting_allows_horizontal_transcript_panning() -> None:
    asset_directory = files("tfr").joinpath("web")
    page = asset_directory.joinpath("index.html").read_text(encoding="utf-8")
    styles = asset_directory.joinpath("styles.css").read_text(encoding="utf-8")
    application = asset_directory.joinpath("app.mjs").read_text(encoding="utf-8")

    assert 'id="line-wrap" type="checkbox" role="switch" checked' in page
    assert ':root[data-line-wrap="off"] .event-text' in styles
    assert "overflow-wrap: normal;" in styles
    assert "white-space: pre;" in styles
    assert 'safeStore("tfr.lineWrap", "off")' in application
    assert "direction && lineWrapPreference()" in application


def test_pointer_opened_event_details_do_not_retain_transcript_focus() -> None:
    asset_directory = files("tfr").joinpath("web")
    application = asset_directory.joinpath("app.mjs").read_text(encoding="utf-8")
    styles = asset_directory.joinpath("styles.css").read_text(encoding="utf-8")

    assert "if (pointerActivated) item.blur();" in application
    assert 'setInputModality("pointer")' in application
    assert ':root[data-input-modality="keyboard"] .event:focus-visible' in styles
    assert "a:focus-visible,\n.event:focus-visible" not in styles


def test_history_notices_auto_dismiss() -> None:
    application = files("tfr").joinpath("web", "app.mjs").read_text(encoding="utf-8")

    assert (
        '"The Gateway restarted. Showing a fresh retained history.",\n        6000,'
        in application
    )
    assert "showHistoryNotice(decision.message, 10000)" in application
    assert "if (state.ready) recordLiveHistoryEvent(world, event);" in application
    assert "event.world === state.selectedWorld && !state.historyReset" in application
    assert "if (evicted) updateHistoryNotice();" in application
    assert 'available_count: "0"' in application


def test_unpair_cleanup_clears_transcript_and_reconnect_state() -> None:
    application = files("tfr").joinpath("web", "app.mjs").read_text(encoding="utf-8")
    start = application.index("async function clearLocalData() {")
    end = application.index("\nasync function unpairDevice", start)
    cleanup = application[start:end]

    assert "window.clearTimeout(state.reconnectTimer);" in cleanup
    assert "state.reconnectTimer = null;" in cleanup
    assert "resetGatewayState();" in cleanup
    assert "state.worlds = [];" in cleanup


def test_command_history_uses_a_touch_friendly_restore_sheet() -> None:
    asset_directory = files("tfr").joinpath("web")
    page = asset_directory.joinpath("index.html").read_text(encoding="utf-8")
    styles = asset_directory.joinpath("styles.css").read_text(encoding="utf-8")
    application = asset_directory.joinpath("app.mjs").read_text(encoding="utf-8")

    assert 'id="history-button" class="history-button"' in page
    assert 'id="history-dialog" class="history-dialog"' in page
    assert 'id="history-older"' not in page
    assert 'id="history-newer"' not in page
    assert "[...history].reverse().map" in application
    assert "elements.commandInput.value = text;" in application
    assert "elements.historyDialog.close();" in application
    history_button_start = styles.index(".history-button {")
    history_button_end = styles.index("}", history_button_start)
    assert "min-width: 4.6rem;" in styles[history_button_start:history_button_end]
    history_row_start = styles.index(".history-list button {")
    history_row_end = styles.index("}", history_row_start)
    assert "min-height: 3.25rem;" in styles[history_row_start:history_row_end]


def test_mobile_composer_keeps_a_visible_send_button() -> None:
    asset_directory = files("tfr").joinpath("web")
    page = asset_directory.joinpath("index.html").read_text(encoding="utf-8")
    styles = asset_directory.joinpath("styles.css").read_text(encoding="utf-8")

    assert 'id="send-button" class="send-button"' in page
    assert ".send-button {" in styles


def test_live_transcript_rendering_is_batched_and_bounded() -> None:
    application = files("tfr").joinpath("web", "app.mjs").read_text(encoding="utf-8")

    assert "function flushLiveEvents()" in application
    assert "document.createDocumentFragment()" in application
    assert "visible.slice(-MAX_RENDERED_EVENTS).map(eventNode)" in application
    assert "requestAnimationFrame(flushLiveEvents)" in application
    assert "window.cancelAnimationFrame(liveRenderFrame)" in application
    assert "if (reading.atLive) elements.transcript.scrollTop" in application


def test_command_submission_does_not_force_scrollback_to_live() -> None:
    application = files("tfr").joinpath("web", "app.mjs").read_text(encoding="utf-8")
    send_start = application.index("function sendCommand(event) {")
    send_end = application.index("\nfunction moveHistory", send_start)
    send_command = application[send_start:send_end]

    assert "reading.atLive = true" not in send_command
    assert "renderTranscript({ scrollToLive: true })" not in send_command


def make_event(
    *,
    kind: EventKind = EventKind.RAW_OUTPUT,
    text: str = "hello",
    world: str = "alpha",
    actor: Actor | None = None,
    generation: int = 1,
) -> Event:
    return Event(
        session_id=UUID("63f755aa-e407-4f78-ae05-f9d62c23f765"),
        world=world,
        connection_generation=generation,
        sequence=1,
        direction=Direction.INBOUND,
        kind=kind,
        canonical_text=text,
        actor=actor,
    )


class FakeRuntime:
    def __init__(self, history: EventHistory) -> None:
        self.gateway_id = UUID("92716400-4bb9-43d2-845f-b8a0e51c9994")
        self.build = BuildIdentity("1.2.3", "a" * 40)
        self.history = history
        self.commands: list[tuple[str, str, str, UUID]] = []
        self.agent_worlds = {"agent-world"}
        self.command_error: Exception | None = None
        self.connection_generation = 1

    def world_descriptors(self) -> list[dict[str, object]]:
        return [
            {
                "world": "alpha",
                "state": "connected",
                "aliases": ["a"],
                "connection_generation": self.connection_generation,
            },
            {
                "world": "agent-world",
                "state": "connected",
                "aliases": [],
                "connection_generation": 1,
            },
        ]

    def agent_descriptors(self) -> list[dict[str, object]]:
        return [
            {"name": f"bot-{world}", "world": world}
            for world in sorted(self.agent_worlds)
        ]

    async def submit_command(
        self,
        *,
        world: str,
        text: str,
        client_id: str,
        request_id: UUID,
    ) -> None:
        if world != "alpha":
            raise ValueError(f"unknown world: {world}")
        if not text or any(character in text for character in "\r\n\0"):
            raise ValueError("invalid command")
        self.commands.append((world, text, client_id, request_id))

    def submit_command_nowait(
        self,
        *,
        world: str,
        text: str,
        client_id: str,
        request_id: UUID,
    ) -> None:
        if self.command_error is not None:
            raise self.command_error
        if world != "alpha":
            raise ValueError(f"unknown world: {world}")
        if not text or any(character in text for character in "\r\n\0"):
            raise ValueError("invalid command")
        self.commands.append((world, text, client_id, request_id))


def test_browser_projection_is_plain_and_excludes_agent_events() -> None:
    projected = browser_event(4, make_event(text="\x1b[31m<script>x</script>\x1b[0m\x07"))

    assert projected is not None
    assert projected["cursor"] == "4"
    assert projected["event"]["text"] == "<script>x</script>"
    assert "metadata" not in projected["event"]
    assert browser_event(5, make_event(kind=EventKind.AGENT_REQUEST)) is None
    assert (
        browser_event(
            6,
            make_event(
                kind=EventKind.COMMAND,
                actor=Actor(ActorType.AGENT, "bot"),
            ),
        )
        is None
    )


def test_browser_projection_bounds_large_event_text() -> None:
    projected = browser_event(1, make_event(text="é" * 10_000))

    assert projected is not None
    assert projected["event"]["text_truncated"] is True
    assert len(projected["event"]["text"].encode("utf-8")) < 8_200


def test_browser_projection_uses_full_ascii_source_budget_before_sanitizing() -> None:
    projected = browser_event(1, make_event(text="\x1b[31m" * 3_000 + "hello"))

    assert projected is not None
    assert projected["event"]["text"] == "hello"
    assert projected["event"]["text_truncated"] is False


def test_browser_projection_bounds_and_sanitizes_provenance() -> None:
    event = make_event()
    event = Event(
        session_id=event.session_id,
        world=event.world,
        connection_generation=event.connection_generation,
        sequence=event.sequence,
        direction=event.direction,
        kind=event.kind,
        canonical_text=event.canonical_text,
        provenance=Provenance(sender_name="\x1b[31m" + "é" * 1_000),
    )

    projected = browser_event(1, event)

    assert projected is not None
    sender = projected["event"]["provenance"]["sender_name"]
    assert "\x1b" not in sender
    assert len(sender.encode("utf-8")) < 264


def test_browser_source_accounting_uses_the_projected_message_text() -> None:
    original = make_event(text="short")
    event = Event(
        session_id=original.session_id,
        world=original.world,
        connection_generation=original.connection_generation,
        sequence=original.sequence,
        direction=original.direction,
        kind=original.kind,
        canonical_text=original.canonical_text,
        provenance=Provenance(sender_name="sender"),
        spoof=SpoofAssessment(
            status=SpoofStatus.SPOOFED,
            speaker="Sender",
            speaker_span=(0, 6),
            suspected_sender="x" * 300,
            attribution_confidence=Confidence.HIGH,
        ),
        metadata={"message_text": "m" * 500},
    )

    projected = browser_event(1, event)

    assert projected is not None
    assert projected["event"]["text"] == "m" * 500
    assert gateway_web._browser_source_bytes(event) == 806


def test_browser_projection_reports_verified_nospoof_source() -> None:
    original = make_event(kind=EventKind.SAY, text='You say, "Hello"')
    event = Event(
        session_id=original.session_id,
        world=original.world,
        connection_generation=original.connection_generation,
        sequence=original.sequence,
        direction=original.direction,
        kind=original.kind,
        canonical_text=original.canonical_text,
        provenance=Provenance(
            sender_name="Black2",
            server_source="saypose",
            confidence=Confidence.HIGH,
        ),
        spoof=SpoofAssessment(
            status=SpoofStatus.NOT_SPOOFED,
            speaker="You",
            speaker_span=(0, 3),
        ),
    )

    projected = browser_event(1, event)

    assert projected is not None
    assert projected["event"]["spoof_status"] == "not_spoofed"
    assert projected["event"]["text_runs"] == [
        {"text": ""},
        {"text": "You", "role": "speaker"},
        {"text": ' say, "Hello"'},
    ]


def test_browser_projection_reports_mismatched_say_speaker_as_spoofed() -> None:
    original = make_event(kind=EventKind.SAY, text='Alice says, "Hello"')
    event = Event(
        session_id=original.session_id,
        world=original.world,
        connection_generation=original.connection_generation,
        sequence=original.sequence,
        direction=original.direction,
        kind=original.kind,
        canonical_text=original.canonical_text,
        provenance=Provenance(sender_name="Widget", confidence=Confidence.HIGH),
        spoof=SpoofAssessment(
            status=SpoofStatus.SPOOFED,
            speaker="Alice",
            speaker_span=(0, 5),
        ),
    )

    projected = browser_event(1, event)

    assert projected is not None
    assert projected["event"]["spoof_status"] == "spoofed"
    assert projected["event"]["text_runs"][1] == {"text": "Alice", "role": "speaker"}


def test_browser_projection_reports_inferred_multiline_spoof_sender() -> None:
    original = make_event(kind=EventKind.SAY, text='Bob says, "Hello"')
    event = Event(
        session_id=original.session_id,
        world=original.world,
        connection_generation=original.connection_generation,
        sequence=original.sequence,
        direction=original.direction,
        kind=original.kind,
        canonical_text=original.canonical_text,
        spoof=SpoofAssessment(
            status=SpoofStatus.SPOOFED,
            speaker="Bob",
            speaker_span=(0, 3),
            reason=SpoofReason.MISSING_NOSPOOF_PREFIX,
            suspected_sender="Black2",
            attribution_confidence=Confidence.INFERRED,
        ),
    )

    projected = browser_event(1, event)

    assert projected is not None
    assert projected["event"]["spoof_reason"] == "missing_nospoof_prefix"
    assert projected["event"]["spoof_sender"] == "Black2"
    assert projected["event"]["spoof_sender_confidence"] == "inferred"


def test_browser_projection_does_not_infer_spoof_status_without_source() -> None:
    original = make_event()
    event = Event(
        session_id=original.session_id,
        world=original.world,
        connection_generation=original.connection_generation,
        sequence=original.sequence,
        direction=original.direction,
        kind=original.kind,
        canonical_text=original.canonical_text,
        provenance=Provenance(sender_name="Widget", confidence=Confidence.HIGH),
    )

    projected = browser_event(1, event)

    assert projected is not None
    assert "spoof_status" not in projected["event"]


async def paired_client(
    tmp_path: Path,
    runtime: FakeRuntime,
    *,
    snapshot_events: int = 20,
) -> tuple[WebGatewayServer, TestClient, str]:
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=snapshot_events,
    )
    client = TestClient(TestServer(gateway.application()))
    await client.start_server()
    url = gateway.create_pairing_url("Test iPhone")
    code = parse_qs(urlsplit(url).fragment)["pair"][0]
    response = await client.post(
        "/api/pair",
        json={"code": code},
        headers=PUBLIC_HEADERS,
    )
    assert response.status == 200
    cookie = SimpleCookie()
    cookie.load(response.headers["Set-Cookie"])
    morsel = cookie[SESSION_COOKIE]
    assert morsel["secure"]
    assert morsel["httponly"]
    assert morsel["samesite"] == "Strict"
    assert 0 < int(morsel["max-age"]) <= 180 * 24 * 60 * 60
    assert parsedate_to_datetime(morsel["expires"]).timestamp() > time.time()
    return gateway, client, morsel.value


async def test_pairing_requires_exact_origin_and_host(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=5,
    )
    client = TestClient(TestServer(gateway.application()))
    await client.start_server()
    try:
        response = await client.post(
            "/api/pair",
            json={"code": "invalid"},
            headers={
                "Host": "gateway.example.ts.net",
                "Origin": "https://evil.test",
                "Tailscale-User-Login": "black@example.com",
            },
        )
        assert response.status == 403
        response = await client.post(
            "/pair",
            data={"code": "invalid"},
            headers={
                "Host": "gateway.example.ts.net",
                "Origin": "https://evil.test",
                "Tailscale-User-Login": "black@example.com",
            },
            allow_redirects=False,
        )
        assert response.status == 403
        response = await client.get(
            "/api/session",
            headers={
                "Host": "gateway.example.ts.net.evil.test",
                "Tailscale-User-Login": "black@example.com",
            },
        )
        assert response.status == 403
        response = await client.get(
            "/",
            headers={"Host": "gateway.example.ts.net"},
        )
        assert response.status == 403
    finally:
        await client.close()
        await bus.close()


async def test_pairing_retry_returns_the_same_device_cookie(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=5,
    )
    client = TestClient(TestServer(gateway.application()))
    await client.start_server()
    code = parse_qs(urlsplit(gateway.create_pairing_url("Test iPhone")).fragment)["pair"][0]
    try:
        payload = {"code": code, "retry_id": "browser-a"}
        first = await client.post("/api/pair", json=payload, headers=PUBLIC_HEADERS)
        retry = await client.post("/api/pair", json=payload, headers=PUBLIC_HEADERS)
        other_browser = await client.post(
            "/api/pair",
            json={"code": code, "retry_id": "browser-b"},
            headers=PUBLIC_HEADERS,
        )
        wrong_identity = await client.post(
            "/api/pair",
            json=payload,
            headers={**PUBLIC_HEADERS, "Tailscale-User-Login": "other@example.com"},
        )

        assert first.status == retry.status == 200
        assert other_browser.status == 200
        cookies = []
        for response in (first, retry, other_browser):
            cookie = SimpleCookie()
            cookie.load(response.headers["Set-Cookie"])
            cookies.append(cookie[SESSION_COOKIE])
        assert {morsel.value for morsel in cookies} == {cookies[0].value}
        assert {morsel["expires"] for morsel in cookies} == {cookies[0]["expires"]}
        assert all(morsel["secure"] and morsel["httponly"] for morsel in cookies)
        assert all(morsel["samesite"] == "Strict" for morsel in cookies)
        assert all(int(morsel["max-age"]) <= int(cookies[0]["max-age"]) for morsel in cookies)
        assert wrong_identity.status == 401
        assert len(gateway.device_descriptors()) == 1
    finally:
        await client.close()
        await bus.close()


async def test_navigation_pairing_sets_cookie_and_redirects(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=5,
    )
    client = TestClient(TestServer(gateway.application()))
    await client.start_server()
    code = parse_qs(urlsplit(gateway.create_pairing_url("Test iPhone")).fragment)["pair"][0]
    try:
        response = await client.post(
            "/pair",
            data={"code": code},
            headers=NAVIGATION_HEADERS,
            allow_redirects=False,
        )

        assert response.status == 303
        assert response.headers["Location"] == "/?pairing=complete"
        cookie = SimpleCookie()
        cookie.load(response.headers["Set-Cookie"])
        morsel = cookie[SESSION_COOKIE]
        token = morsel.value
        assert 0 < int(morsel["max-age"]) <= 180 * 24 * 60 * 60
        assert parsedate_to_datetime(morsel["expires"]).timestamp() > time.time()
        session = await client.get(
            "/api/session",
            headers={**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"},
        )
        assert (await session.json())["paired"] is True
    finally:
        await client.close()
        await bus.close()


async def test_get_pair_redirects_to_the_application_root(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=5,
    )
    client = TestClient(TestServer(gateway.application()))
    await client.start_server()
    try:
        response = await client.get(
            "/pair",
            headers=NAVIGATION_HEADERS,
            allow_redirects=False,
        )

        assert response.status == 303
        assert response.headers["Location"] == "/"
        assert response.headers["Cache-Control"] == "no-store"

        wrong_host = await client.get(
            "/pair",
            headers={**NAVIGATION_HEADERS, "Host": "gateway.example.ts.net.evil.test"},
            allow_redirects=False,
        )
        missing_identity = await client.get(
            "/pair",
            headers={"Host": "gateway.example.ts.net"},
            allow_redirects=False,
        )

        assert wrong_host.status == 403
        assert missing_identity.status == 403
    finally:
        await client.close()
        await bus.close()


async def test_navigation_pairing_accepts_opaque_ios_origin(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=5,
    )
    client = TestClient(TestServer(gateway.application()))
    await client.start_server()
    code = parse_qs(urlsplit(gateway.create_pairing_url("Test iPhone")).fragment)["pair"][0]
    try:
        response = await client.post(
            "/pair",
            data={"code": code},
            headers={**NAVIGATION_HEADERS, "Origin": "null"},
            allow_redirects=False,
        )

        assert response.status == 303
        assert response.headers["Location"] == "/?pairing=complete"
        assert "Set-Cookie" in response.headers
    finally:
        await client.close()
        await bus.close()


async def test_navigation_pairing_rejects_invalid_and_malformed_forms(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=5,
    )
    client = TestClient(TestServer(gateway.application()))
    await client.start_server()
    try:
        invalid = await client.post(
            "/pair",
            data={"code": "invalid"},
            headers=PUBLIC_HEADERS,
            allow_redirects=False,
        )
        malformed = await client.post(
            "/pair",
            data=b"code=invalid",
            headers={
                **PUBLIC_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded; charset=not-a-charset",
            },
            allow_redirects=False,
        )

        assert invalid.status == 303
        assert invalid.headers["Location"] == "/?pairing=invalid"
        assert "Set-Cookie" not in invalid.headers
        assert malformed.status == 400
    finally:
        await client.close()
        await bus.close()


async def test_pairing_rate_limit_is_isolated_by_tailscale_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_web, "MAX_PAIRING_ATTEMPTS_PER_MINUTE", 1)
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=5,
    )
    client = TestClient(TestServer(gateway.application()))
    await client.start_server()
    try:
        first = await client.post("/api/pair", json={"code": "invalid"}, headers=PUBLIC_HEADERS)
        limited = await client.post(
            "/api/pair", json={"code": "invalid"}, headers=PUBLIC_HEADERS
        )
        other_identity = await client.post(
            "/api/pair",
            json={"code": "invalid"},
            headers={**PUBLIC_HEADERS, "Tailscale-User-Login": "other@example.com"},
        )
        monkeypatch.setattr(gateway_web, "MAX_GLOBAL_PAIRING_ATTEMPTS_PER_MINUTE", 2)
        globally_limited = await client.post(
            "/api/pair",
            json={"code": "invalid"},
            headers={**PUBLIC_HEADERS, "Tailscale-User-Login": "third@example.com"},
        )
        form_limited = await client.post(
            "/pair",
            data={"code": "invalid"},
            headers={**PUBLIC_HEADERS, "Tailscale-User-Login": "fourth@example.com"},
            allow_redirects=False,
        )

        assert first.status == 401
        assert limited.status == 429
        assert other_identity.status == 401
        assert globally_limited.status == 429
        assert form_limited.status == 303
        assert form_limited.headers["Location"] == "/?pairing=retry"
        assert "third@example.com" not in gateway._pairing_attempts
        assert "fourth@example.com" not in gateway._pairing_attempts
    finally:
        await client.close()
        await bus.close()


async def test_websocket_snapshot_commands_and_idempotency(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    await bus.publish(make_event(text="retained \x1b[32mmessage\x1b[0m"))
    await bus.publish(make_event(text="private agent output", world="agent-world"))
    await history.flush()
    runtime = FakeRuntime(history)
    gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {
        **PUBLIC_HEADERS,
        "Cookie": f"{SESSION_COOKIE}={token}",
    }
    try:
        wrong_identity = await client.get(
            "/api/session",
            headers={
                **headers,
                "Tailscale-User-Login": "someone-else@example.com",
            },
        )
        assert (await wrong_identity.json())["paired"] is False
        socket = await client.ws_connect("/ws", headers=headers)
        hello = await socket.receive_json()
        snapshot = await socket.receive_json()
        ready = await socket.receive_json()
        assert hello["type"] == "hello"
        assert hello["build"] == {
            "version": "1.2.3",
            "commit": "a" * 40,
            "protocol": 2,
        }
        assert hello["worlds"] == [
            {
                "world": "alpha",
                "state": "connected",
                "aliases": ["a"],
                "connection_generation": "1",
                "history": {
                    "connection_generation": "1",
                    "available_count": "1",
                    "snapshot_count": 1,
                    "snapshot_gap": False,
                },
            }
        ]
        assert snapshot["event"]["text"] == "retained message"
        assert snapshot["event"]["connection_generation"] == "1"
        assert ready["type"] == "ready"

        request_id = str(uuid4())
        command = {
            "type": "command",
            "request_id": request_id,
            "world": "alpha",
            "text": "look",
        }
        await socket.send_json(command)
        assert (await socket.receive_json())["ok"] is True
        await socket.send_json(command)
        assert (await socket.receive_json())["ok"] is True
        assert len(runtime.commands) == 1

        await socket.send_json({**command, "text": "say changed"})
        changed = await socket.receive_json()
        assert changed["ok"] is False
        assert "already used" in changed["error"]

        await socket.send_json({**command, "request_id": str(uuid4()), "text": ""})
        empty = await socket.receive_json()
        assert empty["ok"] is False
        assert "non-empty single line" in empty["error"]

        await socket.send_json({**command, "request_id": str(uuid4()), "sensitive": True})
        forbidden = await socket.receive_json()
        assert forbidden["ok"] is False
        assert "invalid fields" in forbidden["error"]

        leaked_session_id = uuid4()
        runtime.command_error = UnknownSessionError(
            f"session {leaked_session_id} is not registered"
        )
        await socket.send_json({**command, "request_id": str(uuid4())})
        stopped = await socket.receive_json()
        assert stopped["ok"] is False
        assert stopped["error"] == "World session is not accepting commands"
        assert str(leaked_session_id) not in stopped["error"]
        runtime.command_error = None

        await socket.send_json(
            {
                **command,
                "request_id": str(uuid4()),
                "world": "agent-world",
            }
        )
        unauthorized = await socket.receive_json()
        assert unauthorized["ok"] is False
        assert "not authorized" in unauthorized["error"]
        await socket.close()
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_websocket_delivers_rapid_event_burst_in_order(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5_000})
    history.start()
    runtime = FakeRuntime(history)
    _gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        assert (await socket.receive_json())["type"] == "hello"
        assert (await socket.receive_json())["type"] == "ready"

        for sequence in range(5_000):
            await bus.publish(make_event(text=f"stress {sequence:03d}"))
        await history.flush()
        received = [
            await asyncio.wait_for(socket.receive_json(), timeout=5)
            for _ in range(5_000)
        ]

        assert [message["event"]["text"] for message in received] == [
            f"stress {sequence:03d}" for sequence in range(5_000)
        ]
        assert [int(message["cursor"]) for message in received] == list(range(1, 5_001))
        await socket.close()
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_websocket_large_history_returns_newest_ordered_snapshot(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 2_000})
    history.start()
    for sequence in range(2_000):
        await bus.publish(make_event(text=f"history {sequence:04d}"))
    await history.flush()
    runtime = FakeRuntime(history)
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=500,
    )
    client = TestClient(TestServer(gateway.application()))
    await client.start_server()
    url = gateway.create_pairing_url("History iPhone")
    code = parse_qs(urlsplit(url).fragment)["pair"][0]
    response = await client.post("/api/pair", json={"code": code}, headers=PUBLIC_HEADERS)
    cookie = SimpleCookie()
    cookie.load(response.headers["Set-Cookie"])
    token = cookie[SESSION_COOKIE].value
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        hello = await socket.receive_json()
        assert hello["history_truncated"] is True
        assert hello["snapshot_count"] == 500
        assert hello["worlds"][0]["history"] == {
            "connection_generation": "1",
            "available_count": "2000",
            "snapshot_count": 500,
            "snapshot_gap": False,
        }
        received = [await socket.receive_json() for _ in range(500)]
        assert [message["event"]["text"] for message in received] == [
            f"history {sequence:04d}" for sequence in range(1_500, 2_000)
        ]
        assert (await socket.receive_json())["type"] == "ready"
        await socket.close()
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_websocket_snapshot_filters_before_limit_and_scopes_counts_to_generation(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 20})
    history.start()
    await bus.publish(make_event(text="old generation", generation=0))
    await bus.publish(make_event(kind=EventKind.AGENT_REQUEST, text="hidden one"))
    await bus.publish(make_event(kind=EventKind.AGENT_RESPONSE, text="hidden two"))
    for sequence in range(3):
        await bus.publish(make_event(text=f"visible {sequence}"))
    await history.flush()
    runtime = FakeRuntime(history)
    _gateway, client, token = await paired_client(
        tmp_path,
        runtime,
        snapshot_events=2,
    )
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        hello = await socket.receive_json()
        events = [await socket.receive_json(), await socket.receive_json()]

        assert [message["event"]["text"] for message in events] == ["visible 1", "visible 2"]
        assert hello["worlds"][0]["history"] == {
            "connection_generation": "1",
            "available_count": "3",
            "snapshot_count": 2,
            "snapshot_gap": False,
        }
        assert (await socket.receive_json())["type"] == "ready"
        await socket.close()
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_websocket_reports_omitted_current_generation_retention(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 2})
    history.start()
    for sequence in range(5):
        await bus.publish(make_event(text=f"retained {sequence}"))
    await history.flush()
    runtime = FakeRuntime(history)
    _gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        hello = await socket.receive_json()

        assert hello["worlds"][0]["history"] == {
            "connection_generation": "1",
            "available_count": "5",
            "snapshot_count": 2,
            "snapshot_gap": False,
        }
        assert [
            (await socket.receive_json())["event"]["text"] for _ in range(2)
        ] == ["retained 3", "retained 4"]
        assert (await socket.receive_json())["type"] == "ready"
        await socket.close()
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_websocket_resume_reports_total_generation_and_backfill_counts(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    for sequence in range(5):
        await bus.publish(make_event(text=f"message {sequence}"))
    await history.flush()
    runtime = FakeRuntime(history)
    _gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect(
            f"/ws?gateway_id={runtime.gateway_id}&after_cursor=3",
            headers=headers,
        )
        hello = await socket.receive_json()

        assert hello["worlds"][0]["history"] == {
            "connection_generation": "1",
            "available_count": "5",
            "snapshot_count": 2,
            "snapshot_gap": False,
        }
        assert [
            (await socket.receive_json())["event"]["text"] for _ in range(2)
        ] == ["message 3", "message 4"]
        assert (await socket.receive_json())["type"] == "ready"
        await socket.close()
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_websocket_resume_marks_a_retention_gap(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 2})
    history.start()
    for sequence in range(5):
        await bus.publish(make_event(text=f"message {sequence}"))
    await history.flush()
    runtime = FakeRuntime(history)
    _gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect(
            f"/ws?gateway_id={runtime.gateway_id}&after_cursor=1",
            headers=headers,
        )
        hello = await socket.receive_json()

        assert hello["worlds"][0]["history"]["snapshot_gap"] is True
        await socket.close()
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_existing_device_cannot_access_world_after_it_becomes_agent_only(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    gateway, client, token = await paired_client(tmp_path, runtime)
    runtime.agent_worlds.add("alpha")
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        hello = await socket.receive_json()
        assert hello["type"] == "hello"
        assert hello["worlds"] == []
        assert (await socket.receive_json())["type"] == "ready"

        await socket.send_json(
            {
                "type": "command",
                "request_id": str(uuid4()),
                "world": "alpha",
                "text": "look",
            }
        )
        denied = await socket.receive_json()
        assert denied["ok"] is False
        assert "not authorized" in denied["error"]
        assert runtime.commands == []
        await socket.close()

        assert gateway.device_descriptors()[0]["allowed_worlds"] == ["alpha"]
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_connected_device_is_disconnected_if_world_becomes_agent_only(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    runtime = FakeRuntime(history)
    _gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        assert (await socket.receive_json())["type"] == "hello"
        assert (await socket.receive_json())["type"] == "ready"

        runtime.agent_worlds.add("alpha")
        await bus.publish(make_event(text="agent-only output"))
        await history.flush()
        assert (await socket.receive()).type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_websocket_bounds_aggregate_snapshot_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_web, "MAX_WEB_SNAPSHOT_BYTES", 1_500)
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    for sequence in range(4):
        await bus.publish(make_event(text=f"{sequence}:" + "x" * 500))
    await history.flush()
    runtime = FakeRuntime(history)
    _gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        hello = await socket.receive_json()
        assert hello["history_truncated"] is True
        assert 0 < hello["snapshot_count"] < 4
        assert hello["worlds"][0]["history"] == {
            "connection_generation": "1",
            "available_count": "4",
            "snapshot_count": hello["snapshot_count"],
            "snapshot_gap": False,
        }
        for _ in range(hello["snapshot_count"]):
            assert (await socket.receive_json())["type"] == "event"
        assert (await socket.receive_json())["type"] == "ready"
        await socket.close()
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_websocket_bounds_snapshot_source_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_web, "MAX_WEB_SNAPSHOT_SOURCE_BYTES", 600)
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 10})
    history.start()
    await bus.publish(make_event(text="a" * 500))
    await bus.publish(make_event(text="b" * 500))
    await history.flush()
    runtime = FakeRuntime(history)
    _gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        hello = await socket.receive_json()
        assert hello["history_truncated"] is True
        assert hello["snapshot_count"] == 1
        assert hello["worlds"][0]["history"] == {
            "connection_generation": "1",
            "available_count": "2",
            "snapshot_count": 1,
            "snapshot_gap": False,
        }
        assert (await socket.receive_json())["type"] == "event"
        assert (await socket.receive_json())["type"] == "ready"
        await socket.close()
    finally:
        await client.close()
        await history.stop()
        await bus.close()


async def test_websocket_rate_limits_frames_and_connection_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_web, "MAX_WEB_FRAMES_PER_MINUTE", 1)
    monkeypatch.setattr(gateway_web, "MAX_WEB_CONNECTIONS_PER_MINUTE", 2)
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    _gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        assert (await socket.receive_json())["type"] == "hello"
        assert (await socket.receive_json())["type"] == "ready"
        await socket.send_json({"type": "ping", "request_id": str(uuid4())})
        assert (await socket.receive_json())["ok"] is True
        await socket.send_json({"type": "ping", "request_id": str(uuid4())})
        assert (await socket.receive()).type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

        second = await client.ws_connect("/ws", headers=headers)
        assert (await second.receive_json())["type"] == "hello"
        assert (await second.receive_json())["type"] == "ready"
        await second.close()
        with pytest.raises(WSServerHandshakeError, match="429"):
            await client.ws_connect("/ws", headers=headers)
    finally:
        await client.close()
        await bus.close()


async def test_command_backpressure_is_rejected_without_blocking_revocation(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    runtime.command_error = RuntimeError("command queue is full")
    gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        assert (await socket.receive_json())["type"] == "hello"
        assert (await socket.receive_json())["type"] == "ready"
        await socket.send_json(
            {
                "type": "command",
                "request_id": str(uuid4()),
                "world": "alpha",
                "text": "look",
            }
        )
        rejected = await socket.receive_json()
        assert rejected["ok"] is False
        assert "queue is full" in rejected["error"]
        device_id = UUID(gateway.device_descriptors()[0]["device_id"])

        assert await asyncio.wait_for(gateway.revoke_device(device_id), timeout=1) is True
        assert runtime.commands == []
    finally:
        await client.close()
        await bus.close()


async def test_websocket_rejects_unpaired_client(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=5,
    )
    client = TestClient(TestServer(gateway.application()))
    await client.start_server()
    try:
        response = await client.get("/ws", headers=PUBLIC_HEADERS)
        assert response.status == 401
    finally:
        await client.close()
        await bus.close()


async def test_websocket_does_not_expose_history_cursor_exceptions(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        response = await client.get(
            f"/ws?gateway_id={runtime.gateway_id}&after_cursor=999",
            headers=headers,
        )

        assert response.status == 400
        assert await response.text() == "History cursor is invalid or unavailable"
        assert gateway._connection_counts == {}
    finally:
        await client.close()
        await bus.close()


async def test_revocation_closes_active_socket_and_invalidates_cookie(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    try:
        socket = await client.ws_connect("/ws", headers=headers)
        assert (await socket.receive_json())["type"] == "hello"
        assert (await socket.receive_json())["type"] == "ready"
        device_id = UUID(gateway.device_descriptors()[0]["device_id"])

        assert await gateway.revoke_device(device_id) is True

        frame = await socket.receive()
        assert frame.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
        response = await client.get("/api/session", headers=headers)
        assert (await response.json())["paired"] is False
    finally:
        await client.close()
        await bus.close()


async def test_websocket_enforces_per_device_connection_limit(tmp_path: Path) -> None:
    bus = EventBus()
    history = EventHistory(bus, {"alpha": 5})
    runtime = FakeRuntime(history)
    _gateway, client, token = await paired_client(tmp_path, runtime)
    headers = {**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"}
    sockets = []
    try:
        for _ in range(2):
            socket = await client.ws_connect("/ws", headers=headers)
            assert (await socket.receive_json())["type"] == "hello"
            assert (await socket.receive_json())["type"] == "ready"
            sockets.append(socket)

        with pytest.raises(WSServerHandshakeError, match="503"):
            await client.ws_connect("/ws", headers=headers)
    finally:
        for socket in sockets:
            await socket.close()
        await client.close()
        await bus.close()
