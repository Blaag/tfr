from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from pywebpush import WebPushException

import tfr.web_push as web_push
from tfr.web_push import VapidKeyStore, WebPushSender, validate_push_endpoint


def test_vapid_key_is_stable_private_and_public_key_is_urlsafe(tmp_path: Path) -> None:
    state = tmp_path / "web"
    first = VapidKeyStore(state, "https://gateway.example.test")
    second = VapidKeyStore(state, "https://gateway.example.test")

    assert first.public_key == second.public_key
    assert len(first.public_key) == 87
    assert "+" not in first.public_key and "/" not in first.public_key
    if os.name == "posix":
        assert stat.S_IMODE(first.path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://web.push.apple.com/token",
        "https://127.0.0.1/push",
        "https://localhost/push",
        "https://example.com/push",
        "https://user:password@web.push.apple.com/token",
    ],
)
def test_push_endpoint_rejects_non_service_and_private_targets(endpoint: str) -> None:
    with pytest.raises(ValueError):
        validate_push_endpoint(endpoint)


def test_push_endpoint_accepts_ios_and_android_services() -> None:
    assert validate_push_endpoint("https://web.push.apple.com/token").endswith("/token")
    assert validate_push_endpoint("https://fcm.googleapis.com/fcm/send/token").endswith("token")


def test_sender_uses_only_private_generic_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def send(subscription: object, **options: object) -> object:
        captured.update({"subscription": subscription, **options})
        return SimpleNamespace(status_code=201)

    monkeypatch.setattr(web_push, "webpush", send)
    sender = WebPushSender(VapidKeyStore(tmp_path / "web", "https://gateway.example.test"))

    result = sender._send(
        "https://web.push.apple.com/token",
        "BA" + "A" * 85,
        "Y" * 22,
    )

    assert result.delivered is True
    assert captured["data"] == '{"version":1,"type":"activity"}'
    assert "message" not in str(captured)


def test_sender_marks_expired_subscription_permanent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(*_args: object, **_kwargs: object) -> object:
        raise WebPushException("gone", response=SimpleNamespace(status_code=410))

    monkeypatch.setattr(web_push, "webpush", reject)
    sender = WebPushSender(VapidKeyStore(tmp_path / "web", "https://gateway.example.test"))

    result = sender._send("https://web.push.apple.com/token", "key", "auth")

    assert result.delivered is False
    assert result.permanent is True
    assert result.status == 410
