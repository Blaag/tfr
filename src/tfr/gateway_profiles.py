from __future__ import annotations

import getpass
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

from tfr.gateway_transport import (
    DEFAULT_GATEWAY_PORT,
    validate_gateway_token,
    validate_tcp_endpoint,
)

MAX_PROFILE_NAME_CHARACTERS = 80
MAX_PROFILES = 32


def default_gateway_profiles_directory() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return base / "tfr" / "connections"


@dataclass(frozen=True, slots=True)
class GatewayConnectionProfile:
    name: str
    host: str
    port: int
    tls_server_name: str
    tls_ca: str | None
    credential_reference: str
    ui_id: UUID
    ui_name: str

    def as_dict(self, *, export: bool = False) -> dict[str, object]:
        value: dict[str, object] = {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "tls_server_name": self.tls_server_name,
            "tls_ca": self.tls_ca,
            "ui_id": str(self.ui_id),
            "ui_name": self.ui_name,
        }
        if not export:
            value["credential_reference"] = self.credential_reference
        return value

    @classmethod
    def from_dict(cls, value: object) -> GatewayConnectionProfile:
        fields = {
            "name",
            "host",
            "port",
            "tls_server_name",
            "tls_ca",
            "credential_reference",
            "ui_id",
            "ui_name",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("Gateway connection profile has invalid fields")
        name = _profile_name(value["name"])
        host, port = validate_tcp_endpoint(value["host"], value["port"])
        server_name = value["tls_server_name"]
        reference = value["credential_reference"]
        tls_ca = value["tls_ca"]
        if (
            not isinstance(server_name, str)
            or not server_name
            or len(server_name) > 253
            or not server_name.isprintable()
        ):
            raise ValueError("Gateway TLS server name is invalid")
        if tls_ca is not None and (
            not isinstance(tls_ca, str) or not tls_ca or len(tls_ca) > 1_024
        ):
            raise ValueError("Gateway TLS CA path is invalid")
        if (
            not isinstance(reference, str)
            or not reference.startswith(("credential:", "keychain:"))
            or len(reference) > 100
        ):
            raise ValueError("Gateway credential reference is invalid")
        try:
            ui_id = UUID(str(value["ui_id"]))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Gateway profile UI ID is invalid") from exc
        ui_name = _profile_name(value["ui_name"])
        return cls(name, host, port, server_name, tls_ca, reference, ui_id, ui_name)


class GatewayCredentialStore:
    """Private credential backend; profiles retain references, never token plaintext."""

    def __init__(self, directory: Path | str, *, use_keychain: bool = False) -> None:
        self.directory = Path(directory).expanduser()
        self.use_keychain = (
            use_keychain and sys.platform == "darwin" and shutil.which("security") is not None
        )

    def store(self, token: str, *, reference: str | None = None) -> str:
        validate_gateway_token(token)
        if self.use_keychain:
            identifier = reference.split(":", 1)[1] if reference else secrets.token_hex(16)
            subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-U",
                    "-a",
                    identifier,
                    "-s",
                    "tfr-gateway",
                    "-w",
                    token,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return f"keychain:{identifier}"
        self._ensure_directory()
        identifier = reference.removeprefix("credential:") if reference else secrets.token_hex(16)
        if not identifier or any(character not in "0123456789abcdef" for character in identifier):
            raise ValueError("Gateway credential reference is invalid")
        path = self.directory / identifier
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
        os.chmod(path, 0o600)
        return f"credential:{identifier}"

    def load(self, reference: str) -> str:
        if reference.startswith("keychain:"):
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-a",
                    reference.split(":", 1)[1],
                    "-s",
                    "tfr-gateway",
                    "-w",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            token = result.stdout.strip()
            validate_gateway_token(token)
            return token
        identifier = reference.removeprefix("credential:")
        if not identifier or any(character not in "0123456789abcdef" for character in identifier):
            raise ValueError("Gateway credential reference is invalid")
        path = self.directory / identifier
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="ascii") as stream:
            token = stream.read(4097).strip()
        validate_gateway_token(token)
        return token

    def delete(self, reference: str) -> None:
        if reference.startswith("keychain:"):
            subprocess.run(
                [
                    "security",
                    "delete-generic-password",
                    "-a",
                    reference.split(":", 1)[1],
                    "-s",
                    "tfr-gateway",
                ],
                check=False,
                capture_output=True,
            )
            return
        identifier = reference.removeprefix("credential:")
        if identifier and all(character in "0123456789abcdef" for character in identifier):
            (self.directory / identifier).unlink(missing_ok=True)

    def _ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)


class GatewayProfileStore:
    def __init__(self, directory: Path | str | None = None) -> None:
        using_default = directory is None
        self.directory = Path(directory or default_gateway_profiles_directory()).expanduser()
        self.path = self.directory / "profiles.json"
        self.credentials = GatewayCredentialStore(
            self.directory / "credentials", use_keychain=using_default
        )
        self._profiles: dict[str, GatewayConnectionProfile] = {}
        self._active: str | None = None
        self._load()

    @property
    def active(self) -> GatewayConnectionProfile | None:
        return self._profiles.get(self._active.casefold()) if self._active else None

    def profiles(self) -> tuple[GatewayConnectionProfile, ...]:
        return tuple(sorted(self._profiles.values(), key=lambda item: item.name.casefold()))

    def get(self, name: str) -> GatewayConnectionProfile:
        try:
            return self._profiles[_profile_name(name).casefold()]
        except KeyError as exc:
            raise ValueError(f"unknown Gateway profile: {name}") from exc

    def add(self, profile: GatewayConnectionProfile, token: str, *, activate: bool = True) -> None:
        key = profile.name.casefold()
        if key in self._profiles:
            raise ValueError(f"Gateway profile already exists: {profile.name}")
        if len(self._profiles) >= MAX_PROFILES:
            raise ValueError("Gateway profile limit reached")
        reference = self.credentials.store(token)
        stored = replace(profile, credential_reference=reference)
        self._profiles[key] = stored
        if activate or self._active is None:
            self._active = stored.name
        try:
            self._save()
        except BaseException:
            self._profiles.pop(key, None)
            self.credentials.delete(reference)
            raise

    def switch(self, name: str) -> GatewayConnectionProfile:
        profile = self.get(name)
        self._active = profile.name
        self._save()
        return profile

    def rename(self, name: str, replacement: str) -> GatewayConnectionProfile:
        profile = self.get(name)
        replacement = _profile_name(replacement)
        new_key = replacement.casefold()
        if new_key != profile.name.casefold() and new_key in self._profiles:
            raise ValueError(f"Gateway profile already exists: {replacement}")
        del self._profiles[profile.name.casefold()]
        renamed = replace(profile, name=replacement)
        self._profiles[new_key] = renamed
        if self._active and self._active.casefold() == profile.name.casefold():
            self._active = replacement
        self._save()
        return renamed

    def repair(
        self,
        name: str,
        *,
        host: str,
        port: int,
        tls_server_name: str,
        tls_ca: str | None,
        token: str | None,
    ) -> GatewayConnectionProfile:
        profile = self.get(name)
        host, port = validate_tcp_endpoint(host, port)
        repaired = replace(
            profile,
            host=host,
            port=port,
            tls_server_name=tls_server_name,
            tls_ca=tls_ca,
        )
        if token is not None:
            self.credentials.store(token, reference=profile.credential_reference)
        GatewayConnectionProfile.from_dict(repaired.as_dict())
        self._profiles[profile.name.casefold()] = repaired
        self._save()
        return repaired

    def forget(self, name: str) -> GatewayConnectionProfile:
        profile = self.get(name)
        del self._profiles[profile.name.casefold()]
        if self._active and self._active.casefold() == profile.name.casefold():
            self._active = next(iter(self._profiles.values())).name if self._profiles else None
        self._save()
        self.credentials.delete(profile.credential_reference)
        return profile

    def export(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "active": self._active,
            "profiles": [profile.as_dict(export=True) for profile in self.profiles()],
        }

    def _load(self) -> None:
        if not self.path.exists():
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"schema_version", "active", "profiles"}:
            raise ValueError("Gateway profiles document is invalid")
        if value["schema_version"] != 1 or not isinstance(value["profiles"], list):
            raise ValueError("Gateway profiles document is invalid")
        profiles = [GatewayConnectionProfile.from_dict(item) for item in value["profiles"]]
        if len(profiles) > MAX_PROFILES or len({item.name.casefold() for item in profiles}) != len(
            profiles
        ):
            raise ValueError("Gateway profiles document is invalid")
        self._profiles = {item.name.casefold(): item for item in profiles}
        active = value["active"]
        if active is not None and (
            not isinstance(active, str) or active.casefold() not in self._profiles
        ):
            raise ValueError("Gateway active profile is invalid")
        self._active = active

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        value = {
            "schema_version": 1,
            "active": self._active,
            "profiles": [profile.as_dict() for profile in self.profiles()],
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".profiles.", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            Path(temporary).unlink(missing_ok=True)


def interactive_gateway_setup(
    store: GatewayProfileStore,
    *,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
    profile_name: str | None = None,
) -> GatewayConnectionProfile:
    name = profile_name or input_fn("Profile name [default]: ").strip() or "default"
    host = input_fn("Gateway host: ").strip()
    port_text = input_fn(f"Gateway port [{DEFAULT_GATEWAY_PORT}]: ").strip()
    try:
        port = int(port_text) if port_text else DEFAULT_GATEWAY_PORT
    except ValueError as exc:
        raise ValueError("Gateway port must be an integer") from exc
    host, port = validate_tcp_endpoint(host, port)
    server_name = input_fn(f"TLS server name [{host}]: ").strip() or host
    tls_ca = input_fn("Custom CA certificate path [system trust]: ").strip() or None
    token = secret_fn("Gateway authentication token: ").strip()
    platform = socket.gethostname().strip() or "Unknown device"
    proposed_ui_name = f"{platform} · Terminal"[:MAX_PROFILE_NAME_CHARACTERS]
    ui_name = input_fn(f"UI display name [{proposed_ui_name}]: ").strip() or proposed_ui_name
    profile = GatewayConnectionProfile(
        name=_profile_name(name),
        host=host,
        port=port,
        tls_server_name=server_name,
        tls_ca=tls_ca,
        credential_reference="credential:pending",
        ui_id=uuid4(),
        ui_name=_profile_name(ui_name),
    )
    store.add(profile, token)
    return store.get(profile.name)


def interactive_gateway_repair(
    store: GatewayProfileStore,
    name: str,
    *,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
) -> GatewayConnectionProfile:
    profile = store.get(name)
    host = input_fn(f"Gateway host [{profile.host}]: ").strip() or profile.host
    port_text = input_fn(f"Gateway port [{profile.port}]: ").strip()
    try:
        port = int(port_text) if port_text else profile.port
    except ValueError as exc:
        raise ValueError("Gateway port must be an integer") from exc
    server_name = (
        input_fn(f"TLS server name [{profile.tls_server_name}]: ").strip()
        or profile.tls_server_name
    )
    current_ca = profile.tls_ca or "system trust"
    ca_text = input_fn(f"Custom CA certificate path [{current_ca}]: ").strip()
    tls_ca = profile.tls_ca if not ca_text else (None if ca_text == "-" else ca_text)
    token = secret_fn("Replacement token [leave blank to keep current]: ").strip() or None
    return store.repair(
        name,
        host=host,
        port=port,
        tls_server_name=server_name,
        tls_ca=tls_ca,
        token=token,
    )


def _profile_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_PROFILE_NAME_CHARACTERS
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValueError("Gateway profile name is invalid")
    return value
