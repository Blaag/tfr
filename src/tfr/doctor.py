from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Literal

from tfr.config import (
    ConfigurationError,
    MainConfig,
    UiConfiguration,
    load_configuration,
    load_gateway_bootstrap,
    load_ui_configuration,
)
from tfr.executables import find_uv as _find_uv
from tfr.gateway_client import GatewayClient
from tfr.gateway_transport import (
    create_gateway_client_tls_context,
    create_gateway_server_tls_context,
    load_gateway_token,
    validate_tcp_endpoint,
)
from tfr.service_management import service_definition, service_status
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

    def warning(self, message: str) -> None:
        print(f"[warning] {message}")

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
    doctor.ok(f"TFR {build.version}" + (f" ({build.commit[:12]})" if build.commit else ""))
    if shutil.which("git") is None:
        doctor.fail("git is unavailable; stable installs and updates require it")
    else:
        doctor.ok("git is available")
    uv = _find_uv()
    if uv is None:
        doctor.fail("uv is unavailable; install it from https://docs.astral.sh/uv/")
    else:
        doctor.ok(f"uv is available: {uv}")

    configured_state = os.environ.get("XDG_STATE_HOME")
    state_directory = (
        Path(configured_state).expanduser() / "tfr"
        if configured_state
        else Path.home() / ".local" / "state" / "tfr"
    )
    if socket_path is None:
        from tfr.gateway import default_gateway_socket

        socket_path = default_gateway_socket()
    target_socket = socket_path.expanduser()
    if profile in {"gateway", "local-split"}:
        definition = service_definition(
            Path.home() / ".local" / "bin" / "tfr-gateway", state_directory
        )
        status = service_status(definition)
        if definition is None:
            doctor.ok("Gateway user-service management is unsupported; launcher mode is available")
        elif not status.installed:
            doctor.ok("Gateway user service is not installed; launcher-only mode is available")
        elif not status.managed:
            doctor.fail(f"Gateway service definition is not TFR-managed: {definition.path}")
        elif not status.available:
            doctor.fail(f"Gateway service manager is unavailable for {definition.path}")
        else:
            if status.enabled:
                doctor.ok(f"Gateway user service is enabled: {definition.path}")
            else:
                doctor.warning(
                    "Gateway user service definition is installed but not enabled; "
                    "launcher-only operation remains available"
                )
            if status.active:
                doctor.ok("Gateway user service is active")
            elif status.enabled:
                doctor.fail(f"Gateway user service is not active: {status.detail}")
            if status.linger is False:
                doctor.warning(
                    "systemd user lingering is disabled; the Gateway may stop after logout. "
                    f"Run: loginctl enable-linger {os.environ.get('USER', os.getuid())}"
                )
            if status.log_hint:
                print(f"[info] Gateway service logs: {status.log_hint}")

    try:
        if profile == "remote-ui":
            configuration = (
                load_ui_configuration(config)
                if config.exists()
                else UiConfiguration(main_path=config.resolve(), main=MainConfig())
            )
            doctor.ok(
                "UI connection bootstrap is valid"
                + (f": {configuration.main_path}" if config.exists() else " (profile-managed)")
            )
        elif profile in {"gateway", "local-split"}:
            bootstrap = load_gateway_bootstrap(config)
            from tfr.core_settings import core_configuration_registry
            from tfr.gateway_configuration import GatewayConfigurationStore

            store = GatewayConfigurationStore(
                bootstrap.state_directory,
                core_configuration_registry(),
                defer_plugin_validation=True,
            )
            doctor.ok(f"Gateway-managed configuration is valid at revision {store.revision}")
            for warning in bootstrap.warnings:
                print(f"[warning] {warning}")
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
        try:
            client = await GatewayClient.connect(target_socket)
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            doctor.fail(f"local Gateway is not reachable at {target_socket}: {exc}")
            print("Start it with the generated tfr-gateway launcher, then rerun this check.")
        else:
            await _check_web_status(doctor, client)
            await client.stop()
            await client.event_bus.close()
            doctor.ok(f"local Gateway handshake succeeded at {target_socket}")
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
                    f"Gateway TLS listener configuration is valid for {listen_host}:{listen_port}"
                )
            doctor.check_private_file(token_file.expanduser(), "gateway token")
            doctor.check_private_file(tls_private_key.expanduser(), "Gateway TLS private key")
        try:
            client = await GatewayClient.connect(target_socket)
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            doctor.fail(f"Gateway application is not reachable at {target_socket}: {exc}")
        else:
            await _check_web_status(doctor, client)
            await client.stop()
            await client.event_bus.close()
            doctor.ok(f"Gateway application handshake succeeded at {target_socket}")
    else:
        saved_profile = None
        profile_store = None
        if gateway_host is None and token_file is None:
            from tfr.gateway_profiles import GatewayProfileStore

            profile_store = GatewayProfileStore()
            saved_profile = profile_store.active
            if saved_profile is not None:
                gateway_host = saved_profile.host
                gateway_port = saved_profile.port
                tls_ca = Path(saved_profile.tls_ca) if saved_profile.tls_ca else None
                tls_server_name = saved_profile.tls_server_name
        if gateway_host is None or (token_file is None and saved_profile is None):
            doctor.fail("remote UI profile is not configured; rerun setup-tfr")
        else:
            if token_file is not None:
                doctor.check_private_file(token_file.expanduser(), "gateway token")
            try:
                auth_token = (
                    load_gateway_token(token_file)
                    if token_file is not None
                    else profile_store.credentials.load(saved_profile.credential_reference)  # type: ignore[union-attr]
                )
                client = await GatewayClient.connect_tcp(
                    gateway_host,
                    gateway_port,
                    auth_token=auth_token,
                    tls_context=create_gateway_client_tls_context(tls_ca),
                    server_hostname=tls_server_name,
                    client_id=saved_profile.ui_id if saved_profile is not None else None,
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


async def _check_web_status(doctor: Doctor, client: GatewayClient) -> None:
    try:
        status = await client.web_status()
    except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
        doctor.warning(f"Gateway web backend status is unavailable: {exc}")
        return
    if not status.get("enabled"):
        doctor.ok("Gateway web backend is disabled")
        return
    transport = str(status.get("transport", "unknown"))
    if status.get("active"):
        doctor.ok(f"Gateway web backend is active ({transport})")
    else:
        doctor.fail(f"Gateway web backend is configured but inactive ({transport})")
    if transport == "tailscale":
        doctor.ok(f"Tailscale Serve origin is configured: {status.get('origin', 'unknown')}")
