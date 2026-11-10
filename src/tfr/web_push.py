from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

MAX_PUSH_ENDPOINT_CHARACTERS = 4096
MAX_PUSH_KEY_CHARACTERS = 256
PUSH_TTL_SECONDS = 15 * 60
PUSH_TIMEOUT_SECONDS = 10
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
        self._private_pem = self._load_or_create()
        private_key = serialization.load_pem_private_key(self._private_pem, password=None)
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1
        ):
            raise ValueError("web push VAPID key must be a P-256 private key")
        public = private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        self.public_key = base64.urlsafe_b64encode(public).rstrip(b"=").decode("ascii")
        self.vapid = Vapid.from_pem(self._private_pem)

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

    async def send(self, endpoint: str, p256dh: str, auth: str) -> PushResult:
        async with self._semaphore:
            return await asyncio.to_thread(self._send, endpoint, p256dh, auth)

    def _send(self, endpoint: str, p256dh: str, auth: str) -> PushResult:
        subscription = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}
        payload = json.dumps(
            {"version": 1, "type": "activity"}, separators=(",", ":"), ensure_ascii=True
        )
        try:
            response = webpush(
                subscription,
                data=payload,
                vapid_private_key=self.keys.vapid,
                vapid_claims={"sub": self.keys.subject},
                ttl=PUSH_TTL_SECONDS,
                timeout=PUSH_TIMEOUT_SECONDS,
            )
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            return PushResult(
                delivered=False,
                permanent=status in {404, 410},
                status=status,
                error="push service rejected the request" if status else "push service unavailable",
            )
        except (OSError, ValueError):
            return PushResult(delivered=False, error="push request could not be created")
        status = getattr(response, "status_code", 201)
        return PushResult(
            delivered=200 <= status < 300,
            permanent=status in {404, 410},
            status=status,
            error=None if 200 <= status < 300 else "push service rejected the request",
        )
