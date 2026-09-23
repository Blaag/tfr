from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

PAIRING_TTL_SECONDS = 600
MAX_DEVICE_LABEL_CHARACTERS = 80
DEVICE_TOKEN_BYTES = 32
PAIRING_CODE_BYTES = 32
DEVICE_LIFETIME_SECONDS = 180 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    device_id: UUID
    label: str
    token_digest: str
    created_at: datetime
    expires_at: datetime
    allowed_worlds: tuple[str, ...]
    tailscale_login: str
    scope: str = "chat"

    def as_dict(self) -> dict[str, object]:
        return {
            "device_id": str(self.device_id),
            "label": self.label,
            "token_digest": self.token_digest,
            "created_at": self.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "expires_at": self.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "allowed_worlds": list(self.allowed_worlds),
            "tailscale_login": self.tailscale_login,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, value: object) -> DeviceRecord:
        if not isinstance(value, dict) or set(value) != {
            "device_id",
            "label",
            "token_digest",
            "created_at",
            "expires_at",
            "allowed_worlds",
            "tailscale_login",
            "scope",
        }:
            raise ValueError("web device record has invalid fields")
        label = value["label"]
        digest = value["token_digest"]
        scope = value["scope"]
        if (
            not isinstance(label, str)
            or not 1 <= len(label) <= MAX_DEVICE_LABEL_CHARACTERS
            or not label.isprintable()
        ):
            raise ValueError("web device label is invalid")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("web device token digest is invalid")
        try:
            bytes.fromhex(digest)
        except ValueError as exc:
            raise ValueError("web device token digest is invalid") from exc
        if scope != "chat":
            raise ValueError("web device scope is invalid")
        created_at = cls._timestamp(value["created_at"], "creation")
        expires_at = cls._timestamp(value["expires_at"], "expiration")
        allowed_worlds = value["allowed_worlds"]
        tailscale_login = value["tailscale_login"]
        if (
            not isinstance(allowed_worlds, list)
            or not allowed_worlds
            or not all(isinstance(world, str) and world for world in allowed_worlds)
            or len(set(allowed_worlds)) != len(allowed_worlds)
        ):
            raise ValueError("web device allowed worlds are invalid")
        if (
            not isinstance(tailscale_login, str)
            or not 1 <= len(tailscale_login) <= 320
            or not tailscale_login.isprintable()
        ):
            raise ValueError("web device Tailscale login is invalid")
        try:
            device_id = UUID(str(value["device_id"]))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("web device ID is invalid") from exc
        return cls(
            device_id=device_id,
            label=label,
            token_digest=digest,
            created_at=created_at,
            expires_at=expires_at,
            allowed_worlds=tuple(allowed_worlds),
            tailscale_login=tailscale_login,
            scope=scope,
        )

    @staticmethod
    def _timestamp(value: object, label: str) -> datetime:
        if not isinstance(value, str):
            raise ValueError(f"web device {label} time is invalid")
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"web device {label} time is invalid") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError(f"web device {label} time must include a timezone")
        return timestamp


@dataclass(frozen=True, slots=True)
class PairingChallenge:
    label: str
    expires_at: float
    allowed_worlds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RedeemedPairing:
    device: DeviceRecord
    token: str
    tailscale_login: str
    retry_id: str | None
    expires_at: float


class DeviceStore:
    def __init__(self, state_directory: Path | str, origin: str) -> None:
        self.state_directory = Path(state_directory).expanduser()
        self.path = self.state_directory / "devices.json"
        self.origin = origin.rstrip("/")
        self._devices: dict[str, DeviceRecord] = {}
        self._pairings: dict[str, PairingChallenge] = {}
        self._redeemed_pairings: dict[str, RedeemedPairing] = {}
        self._redemption_expirations: dict[str, asyncio.TimerHandle] = {}
        self._lock = asyncio.Lock()
        self._load()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def create_pairing_url(self, label: str, allowed_worlds: tuple[str, ...]) -> str:
        label = label.strip()
        if (
            not 1 <= len(label) <= MAX_DEVICE_LABEL_CHARACTERS
            or not label.isprintable()
        ):
            raise ValueError(
                f"device label must contain 1-{MAX_DEVICE_LABEL_CHARACTERS} characters"
            )
        if not allowed_worlds or any(not world for world in allowed_worlds):
            raise ValueError("at least one non-agent world is required for web pairing")
        now = time.time()
        self._pairings = {
            digest: challenge
            for digest, challenge in self._pairings.items()
            if challenge.expires_at > now
        }
        self._redeemed_pairings = {
            digest: redemption
            for digest, redemption in self._redeemed_pairings.items()
            if redemption.expires_at > now
        }
        code = secrets.token_urlsafe(PAIRING_CODE_BYTES)
        self._pairings[self._digest(code)] = PairingChallenge(
            label=label,
            expires_at=now + PAIRING_TTL_SECONDS,
            allowed_worlds=tuple(dict.fromkeys(allowed_worlds)),
        )
        return f"{self.origin}/#pair={quote(code, safe='')}"

    async def redeem(
        self,
        code: str,
        tailscale_login: str,
        retry_id: str | None = None,
    ) -> tuple[DeviceRecord, str]:
        if not isinstance(code, str) or len(code) > 128 or not code.isascii():
            raise ValueError("pairing code is invalid")
        if (
            not isinstance(tailscale_login, str)
            or not 1 <= len(tailscale_login) <= 320
            or not tailscale_login.isprintable()
        ):
            raise ValueError("Tailscale identity is invalid")
        if retry_id is not None and (
            not isinstance(retry_id, str)
            or not 1 <= len(retry_id) <= 128
            or not retry_id.isascii()
        ):
            raise ValueError("pairing retry ID is invalid")
        async with self._lock:
            now = time.time()
            code_digest = self._digest(code)
            redemption = self._redeemed_pairings.get(code_digest)
            if redemption is not None:
                if redemption.expires_at <= now:
                    self._clear_redemption(code_digest)
                else:
                    if redemption.tailscale_login != tailscale_login or not secrets.compare_digest(
                        redemption.retry_id or "", retry_id or ""
                    ):
                        raise ValueError("pairing code is invalid or expired")
                    return redemption.device, redemption.token
            challenge = self._pairings.pop(code_digest, None)
            if challenge is None or challenge.expires_at <= now:
                raise ValueError("pairing code is invalid or expired")
            token = secrets.token_urlsafe(DEVICE_TOKEN_BYTES)
            created_at = datetime.now(UTC)
            record = DeviceRecord(
                device_id=uuid4(),
                label=challenge.label,
                token_digest=self._digest(token),
                created_at=created_at,
                expires_at=datetime.fromtimestamp(
                    created_at.timestamp() + DEVICE_LIFETIME_SECONDS,
                    UTC,
                ),
                allowed_worlds=challenge.allowed_worlds,
                tailscale_login=tailscale_login,
            )
            self._devices[record.token_digest] = record
            try:
                self._save()
            except BaseException:
                self._devices.pop(record.token_digest, None)
                self._pairings[code_digest] = challenge
                raise
            self._redeemed_pairings[code_digest] = RedeemedPairing(
                device=record,
                token=token,
                tailscale_login=tailscale_login,
                retry_id=retry_id,
                expires_at=challenge.expires_at,
            )
            self._redemption_expirations[code_digest] = asyncio.get_running_loop().call_later(
                max(0.0, challenge.expires_at - time.time()),
                self._expire_redemption,
                code_digest,
            )
            return record, token

    def _expire_redemption(self, code_digest: str) -> None:
        self._redeemed_pairings.pop(code_digest, None)
        self._redemption_expirations.pop(code_digest, None)

    def _clear_redemption(self, code_digest: str) -> None:
        self._redeemed_pairings.pop(code_digest, None)
        expiration = self._redemption_expirations.pop(code_digest, None)
        if expiration is not None:
            expiration.cancel()

    def authenticate(self, token: str | None) -> DeviceRecord | None:
        if token is None or not token.isascii() or len(token) > 128:
            return None
        device = self._devices.get(self._digest(token))
        if device is None or device.expires_at <= datetime.now(UTC):
            return None
        return device

    def authenticate_by_id(self, device_id: UUID) -> DeviceRecord | None:
        device = next(
            (item for item in self._devices.values() if item.device_id == device_id),
            None,
        )
        if device is None or device.expires_at <= datetime.now(UTC):
            return None
        return device

    def device_descriptors(self) -> list[dict[str, object]]:
        return [
            {
                "device_id": str(device.device_id),
                "label": device.label,
                "created_at": device.created_at.astimezone(UTC).isoformat().replace(
                    "+00:00", "Z"
                ),
                "expires_at": device.expires_at.astimezone(UTC).isoformat().replace(
                    "+00:00", "Z"
                ),
                "allowed_worlds": list(device.allowed_worlds),
                "tailscale_login": device.tailscale_login,
                "scope": device.scope,
            }
            for device in sorted(self._devices.values(), key=lambda item: item.created_at)
        ]

    async def revoke(self, device_id: UUID) -> bool:
        async with self._lock:
            digest = next(
                (
                    token_digest
                    for token_digest, device in self._devices.items()
                    if device.device_id == device_id
                ),
                None,
            )
            if digest is None:
                return False
            record = self._devices.pop(digest)
            try:
                self._save()
            except BaseException:
                self._devices[digest] = record
                raise
            for code_digest, redemption in tuple(self._redeemed_pairings.items()):
                if redemption.device.device_id == device_id:
                    self._clear_redemption(code_digest)
            return True

    def _load(self) -> None:
        self._ensure_private_directory()
        try:
            descriptor = self._open_private_file()
        except FileNotFoundError:
            return
        with os.fdopen(descriptor, encoding="utf-8") as state_file:
            try:
                value = json.load(state_file)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError(f"cannot read web device state: {self.path}") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "devices"}:
            raise ValueError("web device state has invalid fields")
        if value["schema_version"] != 1 or not isinstance(value["devices"], list):
            raise ValueError("web device state has an unsupported schema")
        devices = [DeviceRecord.from_dict(item) for item in value["devices"]]
        if len({device.device_id for device in devices}) != len(devices) or len(
            {device.token_digest for device in devices}
        ) != len(devices):
            raise ValueError("web device state contains duplicate records")
        self._devices = {device.token_digest: device for device in devices}

    def _ensure_private_directory(self) -> None:
        try:
            self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise ValueError(
                f"cannot create web device state directory: {self.state_directory}"
            ) from exc
        details = self.state_directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode):
            raise ValueError(f"web device state parent is not a directory: {self.state_directory}")
        if os.name == "posix":
            if details.st_uid != os.geteuid():
                raise ValueError(
                    f"web device state directory must be owned by this user: {self.state_directory}"
                )
            if stat.S_IMODE(details.st_mode) & 0o077:
                raise ValueError(
                    f"web device state directory must not grant group or other permissions: "
                    f"{self.state_directory}"
                )

    def _open_private_file(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError(f"cannot open web device state: {self.path}") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"web device state must be a regular file: {self.path}")
            if os.name == "posix":
                if details.st_uid != os.geteuid():
                    raise ValueError(f"web device state must be owned by this user: {self.path}")
                if stat.S_IMODE(details.st_mode) & 0o077:
                    raise ValueError(
                        f"web device state must not grant group or other permissions: {self.path}"
                    )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _save(self) -> None:
        self._ensure_private_directory()
        if self.path.exists() or self.path.is_symlink():
            descriptor = self._open_private_file()
            os.close(descriptor)
        value = {
            "schema_version": 1,
            "devices": [
                device.as_dict()
                for device in sorted(self._devices.values(), key=lambda item: str(item.device_id))
            ],
        }
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary, flags, 0o600)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                descriptor = -1
                json.dump(value, state_file, ensure_ascii=True, separators=(",", ":"))
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, self.path)
            if os.name == "posix":
                directory_descriptor = os.open(
                    self.state_directory,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
