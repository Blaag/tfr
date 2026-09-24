from __future__ import annotations

import asyncio
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
from tfr.events import Actor, ActorType, Direction, Event, EventKind, Provenance
from tfr.gateway import EventHistory
from tfr.gateway_web import SESSION_COOKIE, WebGatewayServer, browser_event

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
        "pairing.mjs",
        "styles.css",
        "manifest.webmanifest",
        "sw.js",
        "icon.svg",
        "icon-512.png",
        "apple-touch-icon.png",
    } <= {asset.name for asset in asset_directory.iterdir()}


def make_event(
    *,
    kind: EventKind = EventKind.RAW_OUTPUT,
    text: str = "hello",
    world: str = "alpha",
    actor: Actor | None = None,
) -> Event:
    return Event(
        session_id=UUID("63f755aa-e407-4f78-ae05-f9d62c23f765"),
        world=world,
        connection_generation=1,
        sequence=1,
        direction=Direction.INBOUND,
        kind=kind,
        canonical_text=text,
        actor=actor,
    )


class FakeRuntime:
    def __init__(self, history: EventHistory) -> None:
        self.gateway_id = UUID("92716400-4bb9-43d2-845f-b8a0e51c9994")
        self.history = history
        self.commands: list[tuple[str, str, str, UUID]] = []
        self.agent_worlds = {"agent-world"}
        self.command_error: Exception | None = None

    def world_descriptors(self) -> list[dict[str, object]]:
        return [
            {
                "world": "alpha",
                "state": "connected",
                "aliases": ["a"],
            },
            {
                "world": "agent-world",
                "state": "connected",
                "aliases": [],
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


async def paired_client(
    tmp_path: Path,
    runtime: FakeRuntime,
) -> tuple[WebGatewayServer, TestClient, str]:
    gateway = WebGatewayServer(
        runtime,  # type: ignore[arg-type]
        origin=ORIGIN,
        host="127.0.0.1",
        port=7348,
        state_directory=tmp_path / "web",
        snapshot_events=20,
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
        assert first.headers["Set-Cookie"] == retry.headers["Set-Cookie"]
        assert other_browser.status == 200
        assert first.headers["Set-Cookie"] == other_browser.headers["Set-Cookie"]
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
        token = cookie[SESSION_COOKIE].value
        session = await client.get(
            "/api/session",
            headers={**PUBLIC_HEADERS, "Cookie": f"{SESSION_COOKIE}={token}"},
        )
        assert (await session.json())["paired"] is True
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
        assert hello["worlds"] == [
            {"world": "alpha", "state": "connected", "aliases": ["a"]}
        ]
        assert snapshot["event"]["text"] == "retained message"
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
