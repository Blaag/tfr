from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Literal, TypedDict, cast

CURRENT_DEPLOYMENT_SCHEMA = 2
MAX_DEPLOYMENT_BYTES = 16_384
DeploymentProfile = Literal["all-in-one", "local-split", "gateway", "remote-ui"]


class DeploymentDocument(TypedDict, total=False):
    schema_version: int
    profile: DeploymentProfile
    gateway_socket: str


def deployment_path() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local/state"
    return base / "tfr/deployment.json"


def _read(path: Path) -> DeploymentDocument | None:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None
        if metadata.st_size > MAX_DEPLOYMENT_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    schema = value.get("schema_version")
    profile = value.get("profile")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema not in {1, 2}:
        return None
    if profile not in {"all-in-one", "local-split", "gateway", "remote-ui"}:
        return None
    allowed = {"schema_version", "profile", "gateway_socket"} if profile == "remote-ui" else {
        "schema_version",
        "profile",
        "gateway_socket",
    }
    required = {"schema_version", "profile"} if profile == "remote-ui" else allowed
    if not required <= set(value) <= allowed:
        return None
    if profile == "remote-ui" and schema == 1 and set(value) != required:
        return None
    if profile != "remote-ui" and not isinstance(value.get("gateway_socket"), str):
        return None
    return cast(DeploymentDocument, value)


def load_deployment(*, path: Path | None = None) -> DeploymentDocument | None:
    return _read(path or deployment_path())


def write_deployment(
    profile: DeploymentProfile,
    *,
    gateway_socket: Path | str | None = None,
    path: Path | None = None,
) -> None:
    destination = path or deployment_path()
    value: DeploymentDocument = {
        "schema_version": CURRENT_DEPLOYMENT_SCHEMA,
        "profile": profile,
    }
    if profile != "remote-ui":
        if gateway_socket is None:
            raise ValueError("gateway_socket is required for a local deployment")
        value["gateway_socket"] = str(gateway_socket)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(value, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def reconcile_remote_ui_deployment(*, path: Path | None = None) -> DeploymentDocument | None:
    destination = path or deployment_path()
    value = _read(destination)
    if value is None or value["profile"] != "remote-ui":
        return value
    if value["schema_version"] == 1:
        write_deployment("remote-ui", path=destination)
        return _read(destination)
    return value


def ensure_verified_deployment(
    profile: DeploymentProfile,
    *,
    gateway_socket: Path | str | None = None,
    path: Path | None = None,
) -> bool:
    destination = path or deployment_path()
    value = _read(destination)
    if value is None:
        if os.path.lexists(destination):
            return False
        write_deployment(profile, gateway_socket=gateway_socket, path=destination)
        return True
    if value["profile"] != profile:
        return False
    if value["schema_version"] == CURRENT_DEPLOYMENT_SCHEMA:
        if profile != "remote-ui" and value.get("gateway_socket") != str(gateway_socket):
            write_deployment(profile, gateway_socket=gateway_socket, path=destination)
        return True
    write_deployment(profile, gateway_socket=gateway_socket, path=destination)
    return True


def deployment_schema(*, reconcile_remote_ui: bool = False) -> int | None:
    value = (
        reconcile_remote_ui_deployment()
        if reconcile_remote_ui
        else load_deployment()
    )
    return value["schema_version"] if value is not None else None
