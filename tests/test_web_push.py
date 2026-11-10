from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

import tfr.web_push as web_push
from tfr.web_push import (
    VapidKeyStore,
    WebPushSender,
    _encrypt_web_push,
    _vapid_token,
    validate_push_endpoint,
)

RECEIVER_PUBLIC = (
    "BDmIMiq59Sx_EdXRqpKirAsAJ1vK2Ok0aCJXMj_aZySChVtzifEWwZwAFDEcPVfcAgAeOg7IvZDHl3MgNKrNmRg"
)
AUTH_SECRET = "AAECAwQFBgcICQoLDA0ODw"
REFERENCE_CIPHERTEXT = (
    "101112131415161718191a1b1c1d1e1f00001000410453c5a47b0f3273d7333320d3dc363b241a999287"
    "81b9d8827d9660a01dcdf9e98024c87efd5c1388f0a6c3d274467f4824d89218ce12550c298f6a9b393e"
    "fe208f0e49c69b82b69fe47f3afc194aa2872dd1449dedd1e6972cff872b32a1a747d8f3e9be659a6c952"
    "628d217d8a0449d"
)


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


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


def test_aes128gcm_encoding_matches_established_http_ece_vector() -> None:
    sender = ec.derive_private_key(0xFEDCBA987654321, ec.SECP256R1())

    encoded = _encrypt_web_push(
        b'{"version":1,"type":"activity"}',
        RECEIVER_PUBLIC,
        AUTH_SECRET,
        sender_private_key=sender,
        salt=bytes(range(16, 32)),
    )

    assert encoded.hex() == REFERENCE_CIPHERTEXT


def test_vapid_token_has_bounded_claims_and_valid_es256_signature(tmp_path: Path) -> None:
    keys = VapidKeyStore(tmp_path / "web", "https://gateway.example.test")
    token = _vapid_token(
        keys.private_key,
        "https://web.push.apple.com/device",
        keys.subject,
        now=1_700_000_000,
    )
    encoded_header, encoded_claims, encoded_signature = token.split(".")
    header = json.loads(decode(encoded_header))
    claims = json.loads(decode(encoded_claims))
    signature = decode(encoded_signature)
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")

    assert header == {"alg": "ES256", "typ": "JWT"}
    assert claims == {
        "aud": "https://web.push.apple.com",
        "exp": 1_700_043_200,
        "sub": "https://gateway.example.test",
    }
    keys.private_key.public_key().verify(
        encode_dss_signature(r, s),
        f"{encoded_header}.{encoded_claims}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def read(self) -> bytes:
        return b""


class FakeSession:
    status = 201
    request: dict[str, object] = {}

    def __init__(self, **_options: object) -> None:
        pass

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def post(self, endpoint: str, **options: object) -> FakeResponse:
        type(self).request = {"endpoint": endpoint, **options}
        return FakeResponse(type(self).status)


async def test_sender_uses_only_private_generic_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, bytes] = {}

    def encrypt(payload: bytes, _p256dh: str, _auth: str) -> bytes:
        captured["payload"] = payload
        return b"encrypted"

    monkeypatch.setattr(web_push, "_encrypt_web_push", encrypt)
    monkeypatch.setattr(web_push, "_vapid_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(web_push.aiohttp, "ClientSession", FakeSession)
    sender = WebPushSender(VapidKeyStore(tmp_path / "web", "https://gateway.example.test"))

    result = await sender._send("https://web.push.apple.com/token", "key", "auth")

    assert result.delivered is True
    assert captured["payload"] == b'{"version":1,"type":"activity"}'
    assert FakeSession.request["data"] == b"encrypted"
    assert "message" not in str(FakeSession.request)


async def test_sender_marks_expired_subscription_permanent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_push, "_encrypt_web_push", lambda *_args: b"encrypted")
    monkeypatch.setattr(web_push, "_vapid_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(web_push.aiohttp, "ClientSession", FakeSession)
    FakeSession.status = 410
    sender = WebPushSender(VapidKeyStore(tmp_path / "web", "https://gateway.example.test"))

    result = await sender._send("https://web.push.apple.com/token", "key", "auth")

    assert result.delivered is False
    assert result.permanent is True
    assert result.status == 410
    FakeSession.status = 201
