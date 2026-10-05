from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Literal

from tfr.config import ConfigurationError, load_configuration, load_ui_configuration
from tfr.executables import find_uv as _find_uv
from tfr.gateway_client import GatewayClient
from tfr.gateway_transport import (
    create_gateway_client_tls_context,
    create_gateway_server_tls_context,
    load_gateway_token,
    validate_tcp_endpoint,
)
from tfr.updates import current_build

DoctorProfile = Literal["all-in-one", "local-split", "gateway", "remote-ui"]


class Doctor:
    def __init__(self) -> None:
        self.failures = 0

    def ok(self, message: str) -> None:
        print(f"[ok] {message}")

    def fail(self, message: str) -> None:
        self.failures += 1
        print(f"[fail] {message}")

    def check_private_file(self, path: Path, label: str) -> None:
        try:
            details = path.stat()
        except OSError as exc:
            self.fail(f"{label} is unavailable at {path}: {exc}")
            return
        if not stat.S_ISREG(details.st_mode):
            self.fail(f"{label} is not a regular file: {path}")
        elif os.name == "posix" and stat.S_IMODE(details.st_mode) & 0o077:
            self.fail(f"{label} permissions are {stat.S_IMODE(details.st_mode):04o}; use 0600")
        else:
            self.ok(f"{label} exists with private permissions: {path}")


async def run_doctor(
    *,
    profile: DoctorProfile,
    config: Path,
    socket_path: Path | None,
    listen_host: str | None,
    listen_port: int,
    gateway_host: str | None,
    gateway_port: int,
    token_file: Path | None,
    tls_certificate: Path | None,
    tls_private_key: Path | None,
    tls_ca: Path | None,
    tls_server_name: str | None,
) -> int:
    doctor = Doctor()
    build = current_build()
    doctor.ok(
        f"TFR {build.version}" + (f" ({build.commit[:12]})" if build.commit else "")
    )
    if shutil.which("git") is None:
        doctor.fail("git is unavailable; stable installs and updates require it")
    else:
        doctor.ok("git is available")
    uv = _find_uv()
    if uv is None:
        doctor.fail("uv is unavailable; install it from https://docs.astral.sh/uv/")
    else:
        doctor.ok(f"uv is available: {uv}")

    try:
        if profile == "remote-ui":
            configuration = load_ui_configuration(config)
            doctor.ok(f"UI configuration is valid: {configuration.main_path}")
        else:
            bundle = load_configuration(config)
            doctor.ok(
                f"configuration is valid: {len(bundle.worlds.worlds)} world(s), "
                f"{len(bundle.agents.agents)} agent(s)"
            )
            for warning in bundle.warnings:
                doctor.fail(warning)
    except ConfigurationError as exc:
        doctor.fail(str(exc))
        return 2

    if profile == "all-in-one":
        doctor.ok("all-in-one mode does not require a Gateway")
    elif profile == "local-split":
        from tfr.gateway import default_gateway_socket

        target = (socket_path or default_gateway_socket()).expanduser()
        try:
            client = await GatewayClient.connect(target)
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            doctor.fail(f"local Gateway is not reachable at {target}: {exc}")
            print("Start it with the generated tfr-gateway launcher, then rerun this check.")
        else:
            await client.stop()
            await client.event_bus.close()
            doctor.ok(f"local Gateway handshake succeeded at {target}")
    elif profile == "gateway":
        if listen_host is None:
            doctor.fail("Gateway profile requires --listen-host")
        elif token_file is None or tls_certificate is None or tls_private_key is None:
            doctor.fail("Gateway profile requires token, TLS certificate, and TLS private key")
        else:
            try:
                validate_tcp_endpoint(listen_host, listen_port)
                load_gateway_token(token_file)
                create_gateway_server_tls_context(tls_certificate, tls_private_key)
            except (OSError, ValueError) as exc:
                doctor.fail(f"Gateway network configuration is invalid: {exc}")
            else:
                doctor.ok(
                    "Gateway TLS listener configuration is valid for "
                    f"{listen_host}:{listen_port}"
                )
            doctor.check_private_file(token_file.expanduser(), "gateway token")
            doctor.check_private_file(tls_private_key.expanduser(), "Gateway TLS private key")
    else:
        if gateway_host is None or token_file is None:
            doctor.fail("remote UI profile requires --gateway-host and --token-file")
        else:
            doctor.check_private_file(token_file.expanduser(), "gateway token")
            try:
                client = await GatewayClient.connect_tcp(
                    gateway_host,
                    gateway_port,
                    auth_token=load_gateway_token(token_file),
                    tls_context=create_gateway_client_tls_context(tls_ca),
                    server_hostname=tls_server_name,
                )
            except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                doctor.fail(
                    f"remote Gateway handshake failed for {gateway_host}:{gateway_port}: {exc}"
                )
                print("Check DNS/private routing, firewall or Tailscale ACL, TLS hostname/CA, ")
                print("token contents, and that the Gateway and UI run compatible TFR versions.")
            else:
                await client.stop()
                await client.event_bus.close()
                doctor.ok(f"remote Gateway handshake succeeded at {gateway_host}:{gateway_port}")

    if doctor.failures:
        print(f"Doctor found {doctor.failures} problem(s).")
        return 2
    print("Doctor found no problems.")
    return 0
