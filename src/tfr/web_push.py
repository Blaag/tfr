from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_PUSH_ENDPOINT_CHARACTERS = 4096
MAX_PUSH_KEY_CHARACTERS = 256
PUSH_TTL_SECONDS = 15 * 60
PUSH_TIMEOUT_SECONDS = 10
PUSH_RECORD_SIZE = 4096
_ALLOWED_ENDPOINT_HOSTS = frozenset(
    {
        "fcm.googleapis.com",
        "android.googleapis.com",
        "updates.push.services.mozilla.com",
    }
)


def validate_push_endpoint(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_PUSH_ENDPOINT_CHARACTERS:
        raise ValueError("push endpoint is invalid")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("push endpoint is invalid")
    host = host.rstrip(".").casefold()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("push endpoint must use a public push service")
    if host not in _ALLOWED_ENDPOINT_HOSTS and not host.endswith(".push.apple.com"):
        raise ValueError("push endpoint provider is unsupported")
    return value


def validate_push_key(value: object, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_PUSH_KEY_CHARACTERS:
        raise ValueError(f"push {label} key is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"push {label} key is invalid") from exc
    expected_bytes = 65 if label == "p256dh" else 16
    if len(decoded) != expected_bytes or (label == "p256dh" and decoded[0] != 4):
        raise ValueError(f"push {label} key is invalid")
    return value


class VapidKeyStore:
    def __init__(self, state_directory: Path, subject: str) -> None:
        self.state_directory = state_directory
        self.path = state_directory / "web-push-vapid.pem"
        self.subject = subject
        private_pem = self._load_or_create()
        private_key = serialization.load_pem_private_key(private_pem, password=None)
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1
        ):
            raise ValueError("web push VAPID key must be a P-256 private key")
        self.private_key = private_key
        public = private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        self.public_key = base64.urlsafe_b64encode(public).rstrip(b"=").decode("ascii")

    def _load_or_create(self) -> bytes:
        self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        details = self.state_directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode) or (
            os.name == "posix"
            and (details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077)
        ):
            raise ValueError("web push state directory must be private and owned by this user")
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return self._create()
        try:
            file_details = os.fstat(descriptor)
            if not stat.S_ISREG(file_details.st_mode) or (
                os.name == "posix"
                and (
                    file_details.st_uid != os.geteuid()
                    or stat.S_IMODE(file_details.st_mode) & 0o077
                )
            ):
                raise ValueError("web push VAPID key must be a private regular file")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                return source.read(16_384)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _create(self) -> bytes:
        private_key = ec.generate_private_key(ec.SECP256R1())
        value = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
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
            with os.fdopen(descriptor, "wb") as target:
                descriptor = -1
                target.write(value)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        return value


@dataclass(frozen=True, slots=True)
class PushResult:
    delivered: bool
    permanent: bool = False
    status: int | None = None
    error: str | None = None


class WebPushSender:
    def __init__(self, keys: VapidKeyStore, *, concurrency: int = 4) -> None:
        self.keys = keys
        self._semaphore = asyncio.Semaphore(concurrency)

    async def send(
        self,
        endpoint: str,
        p256dh: str,
        auth: str,
        *,
        world: str | None = None,
        reason: str | None = None,
    ) -> PushResult:
        async with self._semaphore:
            return await self._send(endpoint, p256dh, auth, world=world, reason=reason)

    async def _send(
        self,
        endpoint: str,
        p256dh: str,
        auth: str,
        *,
        world: str | None = None,
        reason: str | None = None,
    ) -> PushResult:
        try:
            value: dict[str, object] = {"version": 1, "type": "activity"}
            if world is not None and reason is not None:
                value.update({"world": world[:100], "reason": reason})
            payload = json.dumps(
                value,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            encrypted = _encrypt_web_push(payload, p256dh, auth)
            token = _vapid_token(
                self.keys.private_key,
                endpoint,
                self.keys.subject,
                now=int(time.time()),
            )
            headers = {
                "Authorization": f"vapid t={token}, k={self.keys.public_key}",
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(PUSH_TTL_SECONDS),
            }
            timeout = aiohttp.ClientTimeout(total=PUSH_TIMEOUT_SECONDS)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(
                    endpoint,
                    data=encrypted,
                    headers=headers,
                    allow_redirects=False,
                ) as response,
            ):
                status = response.status
                await response.read()
        except (aiohttp.ClientError, TimeoutError):
            return PushResult(
                delivered=False,
                error="push service unavailable",
            )
        except (OSError, ValueError):
            return PushResult(delivered=False, error="push request could not be created")
        return PushResult(
            delivered=200 <= status < 300,
            permanent=status in {404, 410},
            status=status,
            error=None if 200 <= status < 300 else "push service rejected the request",
        )


def _decode_urlsafe(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode_urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def _encrypt_web_push(
    payload: bytes,
    receiver_public_key: str,
    auth_secret: str,
    *,
    sender_private_key: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    receiver_bytes = _decode_urlsafe(receiver_public_key)
    auth_bytes = _decode_urlsafe(auth_secret)
    if len(receiver_bytes) != 65 or receiver_bytes[0] != 4 or len(auth_bytes) != 16:
        raise ValueError("push subscription key is invalid")
    if len(payload) > PUSH_RECORD_SIZE - 17:
        raise ValueError("push payload is too large")
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver_bytes)
    sender = sender_private_key or ec.generate_private_key(ec.SECP256R1())
    if not isinstance(sender.curve, ec.SECP256R1):
        raise ValueError("push sender key must use P-256")
    sender_bytes = _public_bytes(sender.public_key())
    shared_secret = sender.exchange(ec.ECDH(), receiver)
    key_info = b"WebPush: info\x00" + receiver_bytes + sender_bytes
    input_key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth_bytes,
        info=key_info,
    ).derive(shared_secret)
    salt = salt or os.urandom(16)
    if len(salt) != 16:
        raise ValueError("push salt must contain 16 bytes")
    content_key = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=salt,
        info=b"Content-Encoding: aes128gcm\x00",
    ).derive(input_key_material)
    nonce = HKDF(
        algorithm=hashes.SHA256(),
        length=12,
        salt=salt,
        info=b"Content-Encoding: nonce\x00",
    ).derive(input_key_material)
    ciphertext = AESGCM(content_key).encrypt(nonce, payload + b"\x02", None)
    return (
        salt
        + PUSH_RECORD_SIZE.to_bytes(4, "big")
        + bytes([len(sender_bytes)])
        + sender_bytes
        + ciphertext
    )


def _vapid_token(
    private_key: ec.EllipticCurvePrivateKey,
    endpoint: str,
    subject: str,
    *,
    now: int,
) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("push endpoint is invalid")
    header = _encode_urlsafe(b'{"alg":"ES256","typ":"JWT"}')
    claims = _encode_urlsafe(
        json.dumps(
            {
                "aud": f"{parsed.scheme}://{parsed.netloc}",
                "exp": now + 12 * 60 * 60,
                "sub": subject,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{claims}.{_encode_urlsafe(signature)}"
