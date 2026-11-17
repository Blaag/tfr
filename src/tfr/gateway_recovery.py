from __future__ import annotations

import copy
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tfr.gateway_devices import DeviceRecord
from tfr.gateway_transport import validate_gateway_token
from tfr.gateway_uis import UiRecord

GATEWAY_RECOVERY_VERSION = 1
MAX_RECOVERY_FILE_BYTES = 1024 * 1024
_WEB_FILES = frozenset({"devices.json", "provisioning.json", "web-push-vapid.pem"})


def build_gateway_recovery(
    *,
    listen_host: str,
    listen_port: int,
    token_file: Path,
    tls_certificate: Path,
    tls_private_key: Path,
    ui_state: dict[str, object],
    web_state_directory: Path,
    web_origin: str | None,
    web_transport: str,
    web_target: str | None,
    web_enabled: bool = False,
    web_mode: str = "on-demand",
    web_listen_host: str = "127.0.0.1",
    web_listen_port: int = 7348,
    web_direct_host: str | None = None,
    web_direct_port: int | None = None,
    web_tls_certificate: Path | None = None,
    web_tls_private_key: Path | None = None,
) -> dict[str, object]:
    token = _read_text(token_file, private=True).strip()
    certificate = _read_text(tls_certificate, private=False)
    private_key = _read_text(tls_private_key, private=True)
    web_files = {
        name: _read_text(web_state_directory / name, private=True)
        for name in sorted(_WEB_FILES)
        if (web_state_directory / name).is_file()
    }
    value: dict[str, object] = {
        "schema_version": GATEWAY_RECOVERY_VERSION,
        "gateway": {
            "listen_host": listen_host,
            "listen_port": listen_port,
            "token": token,
            "tls_certificate": certificate,
            "tls_private_key": private_key,
            "tls_server_name": _certificate_server_name(certificate),
        },
        "ui_state": copy.deepcopy(ui_state),
        "web": {
            "enabled": web_enabled,
            "mode": web_mode,
            "origin": web_origin,
            "transport": web_transport,
            "target": web_target,
            "listen_host": web_listen_host,
            "listen_port": web_listen_port,
            "direct_host": web_direct_host,
            "direct_port": web_direct_port,
            "tls_certificate": (
                _read_text(web_tls_certificate, private=False)
                if web_tls_certificate is not None
                else None
            ),
            "tls_private_key": (
                _read_text(web_tls_private_key, private=True)
                if web_tls_private_key is not None
                else None
            ),
            "files": web_files,
        },
    }
    validate_gateway_recovery(value)
    return value


def validate_gateway_recovery(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "gateway",
        "ui_state",
        "web",
    }:
        raise ValueError("Gateway recovery section has invalid fields")
    if value["schema_version"] != GATEWAY_RECOVERY_VERSION:
        raise ValueError("Gateway recovery version is unsupported")
    gateway = value["gateway"]
    if not isinstance(gateway, dict) or set(gateway) != {
        "listen_host",
        "listen_port",
        "token",
        "tls_certificate",
        "tls_private_key",
        "tls_server_name",
    }:
        raise ValueError("Gateway recovery bootstrap is invalid")
    host = gateway["listen_host"]
    port = gateway["listen_port"]
    token = gateway["token"]
    certificate_text = gateway["tls_certificate"]
    private_key_text = gateway["tls_private_key"]
    server_name = gateway["tls_server_name"]
    if (
        not isinstance(host, str)
        or not host.strip()
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not isinstance(token, str)
        or not isinstance(certificate_text, str)
        or not isinstance(private_key_text, str)
        or not isinstance(server_name, str)
        or not server_name
    ):
        raise ValueError("Gateway recovery bootstrap is invalid")
    validate_gateway_token(token)
    certificate = _certificate(certificate_text)
    private_key = _private_key(private_key_text)
    if certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ) != private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ):
        raise ValueError("Gateway recovery TLS certificate and key do not match")

    _validate_ui_state(value["ui_state"])
    web = value["web"]
    if not isinstance(web, dict) or set(web) != {
        "enabled",
        "mode",
        "origin",
        "transport",
        "target",
        "listen_host",
        "listen_port",
        "direct_host",
        "direct_port",
        "tls_certificate",
        "tls_private_key",
        "files",
    }:
        raise ValueError("Gateway recovery web state is invalid")
    enabled = web["enabled"]
    mode = web["mode"]
    origin = web["origin"]
    transport = web["transport"]
    target = web["target"]
    listen_host = web["listen_host"]
    listen_port = web["listen_port"]
    direct_host = web["direct_host"]
    direct_port = web["direct_port"]
    web_certificate_text = web["tls_certificate"]
    web_private_key_text = web["tls_private_key"]
    files = web["files"]
    if origin is not None:
        _https_origin(origin)
    if transport not in {"tailscale", "direct"}:
        raise ValueError("Gateway recovery web transport is invalid")
    if (
        not isinstance(enabled, bool)
        or mode not in {"on-demand", "disabled"}
        or not isinstance(listen_host, str)
        or not listen_host
        or not isinstance(listen_port, int)
        or isinstance(listen_port, bool)
        or not 1 <= listen_port <= 65535
        or (direct_host is not None and not isinstance(direct_host, str))
        or (
            direct_port is not None
            and (
                not isinstance(direct_port, int)
                or isinstance(direct_port, bool)
                or not 1 <= direct_port <= 65535
            )
        )
    ):
        raise ValueError("Gateway recovery web listener is invalid")
    if (web_certificate_text is None) != (web_private_key_text is None):
        raise ValueError("Gateway recovery web TLS keypair is incomplete")
    if web_certificate_text is not None and web_private_key_text is not None:
        if not isinstance(web_certificate_text, str) or not isinstance(web_private_key_text, str):
            raise ValueError("Gateway recovery web TLS keypair is invalid")
        web_certificate = _certificate(web_certificate_text)
        web_private_key = _private_key(web_private_key_text)
        if web_certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) != web_private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ):
            raise ValueError("Gateway recovery web TLS certificate and key do not match")
    if transport == "direct" and not direct_host:
        raise ValueError("Gateway recovery direct web host is missing")
    if target is not None:
        parsed_target = urlsplit(target) if isinstance(target, str) else None
        if (
            parsed_target is None
            or parsed_target.scheme != "http"
            or parsed_target.hostname not in {"127.0.0.1", "::1"}
            or parsed_target.port is None
            or parsed_target.path not in {"", "/"}
            or parsed_target.username is not None
            or parsed_target.password is not None
            or parsed_target.query
            or parsed_target.fragment
        ):
            raise ValueError("Gateway recovery web target is invalid")
    if (
        not isinstance(files, dict)
        or not set(files) <= _WEB_FILES
        or not all(
            isinstance(name, str)
            and isinstance(content, str)
            and len(content.encode("utf-8")) <= MAX_RECOVERY_FILE_BYTES
            for name, content in files.items()
        )
    ):
        raise ValueError("Gateway recovery web files are invalid")
    if "devices.json" in files:
        _validate_device_state(json.loads(files["devices.json"]))
    if "provisioning.json" in files:
        provisioning = json.loads(files["provisioning.json"])
        if (
            not isinstance(provisioning, dict)
            or set(provisioning) != {"schema_version", "origin"}
            or provisioning["schema_version"] != 1
        ):
            raise ValueError("Gateway recovery provisioning state is invalid")
        _https_origin(provisioning["origin"])
        if origin is not None and str(provisioning["origin"]).rstrip("/") != origin.rstrip("/"):
            raise ValueError("Gateway recovery web origins do not match")
    if "web-push-vapid.pem" in files:
        key = _private_key(files["web-push-vapid.pem"])
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise ValueError("Gateway recovery VAPID key is invalid")
    return copy.deepcopy(value)


def restore_gateway_recovery(
    value: object,
    *,
    config_directory: Path,
    managed_state_directory: Path,
    web_state_directory: Path,
) -> dict[str, object]:
    recovery = validate_gateway_recovery(value)
    details = gateway_recovery_details(
        recovery,
        config_directory=config_directory,
    )
    gateway = recovery["gateway"]
    web = recovery["web"]
    if not isinstance(gateway, dict) or not isinstance(web, dict):  # pragma: no cover
        raise ValueError("validated Gateway recovery has invalid sections")
    files = web["files"]
    if not isinstance(files, dict):  # pragma: no cover
        raise ValueError("validated Gateway recovery has invalid web files")
    writes = [
        (config_directory / "gateway.token", f"{gateway['token']}\n".encode(), 0o600),
        (
            config_directory / "gateway.crt",
            str(gateway["tls_certificate"]).encode(),
            0o644,
        ),
        (
            config_directory / "gateway.key",
            str(gateway["tls_private_key"]).encode(),
            0o600,
        ),
        (
            managed_state_directory / "uis.json",
            (
                json.dumps(recovery["ui_state"], ensure_ascii=True, separators=(",", ":")) + "\n"
            ).encode(),
            0o600,
        ),
        *(
            (web_state_directory / name, str(content).encode(), 0o600)
            for name, content in files.items()
        ),
    ]
    if web["tls_certificate"] is not None and web["tls_private_key"] is not None:
        writes.extend(
            (
                (
                    config_directory / "web-gateway.crt",
                    str(web["tls_certificate"]).encode(),
                    0o644,
                ),
                (
                    config_directory / "web-gateway.key",
                    str(web["tls_private_key"]).encode(),
                    0o600,
                ),
            )
        )
    pending_writes = []
    for target, data, mode in writes:
        if target.exists() or target.is_symlink():
            if _existing_target_matches(target, data, mode=mode):
                continue
            raise ValueError(f"refusing to replace existing recovery target: {target}")
        pending_writes.append((target, data, mode))
    created: list[Path] = []
    try:
        for target, data, mode in pending_writes:
            _write_private(target, data, mode=mode)
            created.append(target)
    except BaseException:
        for target in reversed(created):
            target.unlink(missing_ok=True)
        raise
    return details


def gateway_recovery_details(
    value: object,
    *,
    config_directory: Path,
) -> dict[str, object]:
    recovery = validate_gateway_recovery(value)
    gateway = recovery["gateway"]
    web = recovery["web"]
    if not isinstance(gateway, dict) or not isinstance(web, dict):  # pragma: no cover
        raise ValueError("validated Gateway recovery has invalid sections")
    return {
        "listen_host": gateway["listen_host"],
        "port": gateway["listen_port"],
        "certificate": config_directory / "gateway.crt",
        "private_key": config_directory / "gateway.key",
        "token": config_directory / "gateway.token",
        "certificate_host": gateway["tls_server_name"],
        "ca_file": None,
        "web_origin": web["origin"],
        "web_transport": web["transport"],
        "web_target": web["target"],
        "web": copy.deepcopy(web),
    }


def _validate_ui_state(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "uis",
        "configuration_profiles",
        "credentials",
    }:
        raise ValueError("Gateway recovery UI state is invalid")
    if value["schema_version"] != 3 or not isinstance(value["uis"], list):
        raise ValueError("Gateway recovery UI state is invalid")
    records = [UiRecord.from_dict(item) for item in value["uis"]]
    ids = {str(record.ui_id) for record in records}
    if len(ids) != len(records) or len(
        {record.display_name.casefold() for record in records}
    ) != len(records):
        raise ValueError("Gateway recovery UI state contains duplicate records")
    profiles = value["configuration_profiles"]
    credentials = value["credentials"]
    if not isinstance(profiles, dict) or not isinstance(credentials, dict):
        raise ValueError("Gateway recovery UI authority is invalid")
    if not set(profiles) <= ids:
        raise ValueError("Gateway recovery UI authority refers to an unknown UI")
    try:
        for profile_id in profiles.values():
            UUID(str(profile_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Gateway recovery UI profile is invalid") from exc
    if len(set(str(item) for item in profiles.values())) != len(profiles):
        raise ValueError("Gateway recovery UI profile is claimed more than once")
    for ui_id, digest in credentials.items():
        try:
            UUID(ui_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Gateway recovery UI credentials are invalid") from exc
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("Gateway recovery UI credentials are invalid")


def _validate_device_state(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "devices"}
        or value["schema_version"] not in {1, 2}
        or not isinstance(value["devices"], list)
    ):
        raise ValueError("Gateway recovery device state is invalid")
    devices = [DeviceRecord.from_dict(item) for item in value["devices"]]
    if len({item.device_id for item in devices}) != len(devices) or len(
        {item.token_digest for item in devices}
    ) != len(devices):
        raise ValueError("Gateway recovery device state contains duplicate records")
    endpoints = [item.push.endpoint for item in devices if item.push is not None]
    if len(endpoints) != len(set(endpoints)):
        raise ValueError("Gateway recovery device state contains duplicate push subscriptions")


def _https_origin(value: object) -> str:
    parsed = urlsplit(value) if isinstance(value, str) else None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Gateway recovery web origin is invalid")
    return value.rstrip("/")


def _certificate(value: str) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(value.encode())
    except ValueError as exc:
        raise ValueError("Gateway recovery TLS certificate is invalid") from exc


def _private_key(value: str) -> Any:
    try:
        return serialization.load_pem_private_key(value.encode(), password=None)
    except (TypeError, ValueError) as exc:
        raise ValueError("Gateway recovery private key is invalid") from exc


def _certificate_server_name(value: str) -> str:
    certificate = _certificate(value)
    try:
        alternatives = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        names = alternatives.get_values_for_type(x509.DNSName)
        addresses = alternatives.get_values_for_type(x509.IPAddress)
    except x509.ExtensionNotFound:
        names = []
        addresses = []
    if names:
        return names[0]
    if addresses:
        return str(addresses[0])
    attributes = certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    if attributes and attributes[0].value:
        return attributes[0].value
    raise ValueError("Gateway TLS certificate has no DNS server name")


def _read_text(path: Path, *, private: bool) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(f"Gateway recovery source is not a regular file: {path}")
        if (
            private
            and os.name == "posix"
            and (details.st_uid != os.geteuid() or details.st_mode & 0o077)
        ):
            raise ValueError(f"Gateway recovery source must be owned and private: {path}")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            value = stream.read(MAX_RECOVERY_FILE_BYTES + 1)
        if len(value.encode("utf-8")) > MAX_RECOVERY_FILE_BYTES:
            raise ValueError(f"Gateway recovery source is too large: {path}")
        return value
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_private(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to replace existing recovery target: {path}")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _existing_target_matches(path: Path, data: bytes, *, mode: int) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            return False
        if os.name == "posix" and (
            details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & ~mode
        ):
            return False
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read(len(data) + 1) == data
    finally:
        if descriptor >= 0:
            os.close(descriptor)
