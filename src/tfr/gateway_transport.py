from __future__ import annotations

import ipaddress
import os
import ssl
import stat
from pathlib import Path

MINIMUM_TOKEN_BYTES = 32
MAXIMUM_TOKEN_FILE_BYTES = 4_096
DEFAULT_GATEWAY_PORT = 7347


def validate_tcp_endpoint(
    host: str,
    port: int,
    *,
    allow_zero: bool = False,
) -> tuple[str, int]:
    host = host.strip()
    if not host:
        raise ValueError("gateway TCP host cannot be empty")
    minimum_port = 0 if allow_zero else 1
    if not minimum_port <= port <= 65_535:
        raise ValueError("gateway TCP port must be between 1 and 65535")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if address.is_unspecified:
            raise ValueError("gateway TCP host must not be a wildcard address")
    return host, port


def _open_private_file(path: Path, label: str) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open {label}: {path}") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(f"{label} must be a regular file: {path}")
        if os.name == "posix":
            if details.st_uid != os.geteuid():
                raise ValueError(f"{label} must be owned by this user: {path}")
            if stat.S_IMODE(details.st_mode) & 0o077:
                raise ValueError(f"{label} must not grant group or other permissions: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def load_gateway_token(path: Path | str) -> str:
    token_path = Path(path).expanduser()
    descriptor = _open_private_file(token_path, "gateway token file")
    with os.fdopen(descriptor, "rb") as token_file:
        data = token_file.read(MAXIMUM_TOKEN_FILE_BYTES + 1)
    if len(data) > MAXIMUM_TOKEN_FILE_BYTES:
        raise ValueError("gateway token file is too large")
    try:
        token = data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("gateway token must be UTF-8 text") from exc
    return validate_gateway_token(token)


def validate_gateway_token(token: str) -> str:
    if not isinstance(token, str) or not token.isascii():
        raise ValueError("gateway token must contain only ASCII characters")
    if len(token.encode("ascii")) < MINIMUM_TOKEN_BYTES:
        raise ValueError(f"gateway token must contain at least {MINIMUM_TOKEN_BYTES} bytes")
    if any(character.isspace() for character in token):
        raise ValueError("gateway token cannot contain whitespace")
    return token


def create_gateway_server_tls_context(
    certificate: Path | str,
    private_key: Path | str,
) -> ssl.SSLContext:
    certificate_path = Path(certificate).expanduser()
    key_path = Path(private_key).expanduser()
    descriptor = _open_private_file(key_path, "gateway TLS private key")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        stable_key_path = Path(f"/dev/fd/{descriptor}")
        context.load_cert_chain(
            certificate_path,
            stable_key_path if stable_key_path.exists() else key_path,
        )
    except (OSError, ssl.SSLError) as exc:
        raise ValueError("cannot load gateway TLS certificate and private key") from exc
    finally:
        os.close(descriptor)
    return context


def create_gateway_client_tls_context(ca_file: Path | str | None = None) -> ssl.SSLContext:
    try:
        context = ssl.create_default_context(
            cafile=str(Path(ca_file).expanduser()) if ca_file is not None else None
        )
    except (OSError, ssl.SSLError) as exc:
        raise ValueError("cannot load gateway TLS CA file") from exc
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def validate_gateway_client_tls_context(context: ssl.SSLContext) -> None:
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise ValueError("gateway TLS context must verify certificates and hostnames")
    if context.minimum_version < ssl.TLSVersion.TLSv1_2:
        raise ValueError("gateway TLS context must require TLS 1.2 or newer")
