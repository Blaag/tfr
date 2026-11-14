from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from tfr.service_management import (
    install_service,
    remove_service,
    service_definition,
    service_file_is_managed,
    stable_gateway_socket,
)

Profile = Literal["all-in-one", "local-split", "gateway", "remote-ui"]

PUBLIC_PLUGINS_URL = "https://github.com/Blaag/tfr-plugins-public"
DEFAULT_GATEWAY_PORT = 7347
SCHEMA_ROOT = "https://raw.githubusercontent.com/Blaag/tfr/main/schemas"
WORLD_ALIAS_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def _yes_no(prompt: str, *, default: bool = True) -> bool:
    choice = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{choice}]: ").strip().casefold()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def _choice(prompt: str, choices: dict[str, str], *, default: str) -> str:
    while True:
        print(prompt)
        for key, label in choices.items():
            marker = " (recommended)" if key == default else ""
            print(f"  {key}) {label}{marker}")
        value = input(f"Choose [{default}]: ").strip() or default
        if value in choices:
            return value
        print("Choose one of: " + ", ".join(choices))


def _port(prompt: str, default: int) -> int:
    while True:
        try:
            value = int(_ask(prompt, str(default)))
        except ValueError:
            value = 0
        if 1 <= value <= 65535:
            return value
        print("Port must be between 1 and 65535.")


def _profile() -> Profile:
    print("\nA Gateway is optional. Start with all-in-one unless you need persistent")
    print("world connections or UIs on separate machines. Rerun this installer later")
    print("to change the deployment without reinstalling your configuration.")
    selected = _choice(
        "\nWhat do you want to configure?",
        {
            "1": "All-in-one: one local process, easiest way to try TFR",
            "2": "Local Gateway and UI: two processes on this machine",
            "3": "Gateway server: worlds live here; remote UIs connect over TLS",
            "4": "Remote UI: connect this machine to a Gateway elsewhere",
        },
        default="1",
    )
    return {
        "1": "all-in-one",
        "2": "local-split",
        "3": "gateway",
        "4": "remote-ui",
    }[selected]  # type: ignore[return-value]


def _backup(path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.backup-{timestamp}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup-{timestamp}-{counter}")
        counter += 1
    shutil.copy2(path, backup)
    backup.chmod(0o600)
    return backup


def _write_file(
    path: Path,
    content: str,
    *,
    mode: int,
    description: str,
    replace_confirmed: bool = False,
) -> bool:
    if path.exists():
        if not replace_confirmed and not _yes_no(
            f"Replace existing {description} at {path}?", default=False
        ):
            print(f"Keeping {path}")
            return False
        backup = _backup(path)
        print(f"Backed up existing file to {backup}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Wrote {path}")
    return True


def _world_entry(existing_names: set[str]) -> tuple[str, dict[str, object]]:
    while True:
        name = _ask("World name used inside TFR")
        if name and name not in existing_names:
            break
        print("Enter a non-empty, unique world name.")
    host = _ask("World hostname")
    while not host:
        host = _ask("World hostname")
    port = _port("World port", 4201)
    server_number = _choice(
        "World server type:",
        {
            "1": "TinyMUX",
            "2": "TinyMUSH",
            "3": "RhostMUSH",
            "4": "Generic",
            "5": "Bare (minimal parsing)",
        },
        default="1",
    )
    server = {"1": "tinymux", "2": "tinymush", "3": "rhost", "4": "generic", "5": "bare"}[
        server_number
    ]
    while True:
        alias_value = _ask("Switch aliases, comma-separated (optional)")
        aliases = [item.strip() for item in alias_value.split(",") if item.strip()]
        normalized_aliases = [alias.casefold() for alias in aliases]
        if len(aliases) > 32:
            print("Enter no more than 32 aliases.")
        elif any(WORLD_ALIAS_PATTERN.fullmatch(alias) is None for alias in aliases):
            print(
                "Aliases must start with a letter and use at most 64 letters, digits, "
                "underscores, or hyphens."
            )
        elif len(set(normalized_aliases)) != len(aliases):
            print("Aliases must be unique, ignoring case.")
        else:
            break
    tls_enabled = _yes_no("Does this world use TLS?", default=True)
    character = _ask("Character name (leave blank for no automatic login)")
    login: dict[str, str] | None = None
    if character:
        password = getpass.getpass("Character password: ")
        login = {"character": character, "password": password}
    provenance = server in {"tinymux", "tinymush", "rhost"} and _yes_no(
        "Enable NOSPOOF detection for this world?", default=True
    )
    unicode = _yes_no(
        "Does the world preserve Unicode? (enables Braille /image output)", default=False
    )
    entry: dict[str, object] = {
        "aliases": aliases,
        "host": host,
        "port": port,
        "server": server,
        "capabilities": {"unicode": unicode},
        "tls": {"enabled": tls_enabled, "verify": True},
        "autoconnect": _yes_no("Connect to this world when TFR starts?", default=True),
        "provenance": {"nospoof": provenance, "show_prefix": False},
        "startup_commands": [],
    }
    if login is not None:
        entry["login"] = login
    return name, entry


def generate_worlds_config(worlds: dict[str, dict[str, object]]) -> str:
    document = {
        "$schema": f"{SCHEMA_ROOT}/worlds.schema.json",
        "schema_version": 1,
        "defaults": {
            "encoding": "utf-8",
            "reconnect": True,
            "scrollback_lines": 20000,
        },
        "worlds": worlds,
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _default_config_directory() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".config"
    return base / "tfr"


def _deployment_path() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return base / "tfr" / "deployment.json"


def _save_deployment(profile: Profile, gateway_socket: Path) -> None:
    value: dict[str, object]
    if profile == "remote-ui":
        # Keep this exact schema-1 marker readable by managed releases predating
        # service metadata. Remote UIs do not use the local Gateway socket.
        value = {"schema_version": 1, "profile": profile}
    else:
        value = {
            "schema_version": 2,
            "profile": profile,
            "gateway_socket": str(gateway_socket),
        }
    _write_file(
        _deployment_path(),
        json.dumps(value, separators=(",", ":")) + "\n",
        mode=0o600,
        description="deployment selection",
        replace_confirmed=True,
    )


def empty_agents_config() -> str:
    return (
        "{\n"
        f'  "$schema": "{SCHEMA_ROOT}/agents.schema.json",\n'
        '  "schema_version": 1,\n'
        '  "providers": {},\n'
        '  "agents": {},\n'
        "}\n"
    )


def launcher_script(arguments: list[str]) -> str:
    command = " \\\n  ".join(shlex.quote(argument) for argument in arguments)
    return f'#!/bin/sh\nset -eu\n\nexec {command} "$@"\n'


def _configure_worlds(repository: Path, config_directory: Path) -> None:
    worlds_path = config_directory / "worlds.jsonc"
    agents_path = config_directory / "agents.jsonc"
    replace_worlds = worlds_path.exists()
    if replace_worlds:
        replace_worlds = _yes_no(
            f"Replace or rebuild existing worlds file {worlds_path}?", default=False
        )
        if not replace_worlds:
            print(f"Keeping {worlds_path}")
            if not agents_path.exists():
                _write_file(
                    agents_path,
                    empty_agents_config(),
                    mode=0o600,
                    description="agents config",
                )
            return
    if _yes_no("Configure one or more worlds interactively now?", default=True):
        worlds: dict[str, dict[str, object]] = {}
        while True:
            name, entry = _world_entry(set(worlds))
            worlds[name] = entry
            if not _yes_no("Add another world?", default=False):
                break
        _write_file(
            worlds_path,
            generate_worlds_config(worlds),
            mode=0o600,
            description="worlds config",
            replace_confirmed=replace_worlds,
        )
        replace_agents = agents_path.exists() and _yes_no(
            "Replace agents.jsonc with an empty agent configuration?", default=False
        )
        if not agents_path.exists() or replace_agents:
            _write_file(
                agents_path,
                empty_agents_config(),
                mode=0o600,
                description="agents config",
                replace_confirmed=replace_agents,
            )
        return
    source = repository / "examples" / "worlds.jsonc"
    content = source.read_text(encoding="utf-8").replace(
        '"$schema": "../schemas/worlds.schema.json"',
        f'"$schema": "{SCHEMA_ROOT}/worlds.schema.json"',
        1,
    )
    _write_file(
        worlds_path,
        content,
        mode=0o600,
        description="worlds config",
        replace_confirmed=replace_worlds,
    )
    if not agents_path.exists():
        agents_content = (
            (repository / "examples" / "agents.jsonc")
            .read_text(encoding="utf-8")
            .replace(
                '"$schema": "../schemas/agents.schema.json"',
                f'"$schema": "{SCHEMA_ROOT}/agents.schema.json"',
                1,
            )
        )
        _write_file(
            agents_path,
            agents_content,
            mode=0o600,
            description="agents config",
        )
    print("\nExample worlds.jsonc:\n")
    print(source.read_text(encoding="utf-8"))
    print(f"Edit {worlds_path}")
    print("At minimum, replace each host, port, server type, character, and password.")
    print("Review TLS, autoconnect, NOSPOOF, and capabilities.unicode for each world.")


def _ensure_token(path: Path) -> None:
    if path.exists():
        print(f"Using existing gateway token {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as token_file:
        token_file.write(secrets.token_hex(32) + "\n")
    print(f"Generated gateway token {path}")


def _path(prompt: str, default: Path | None = None, *, required: bool = True) -> Path | None:
    value = _ask(prompt, str(default) if default is not None else None)
    if not value and not required:
        return None
    while not value:
        value = _ask(prompt)
    return Path(value).expanduser().resolve()


def _gateway_details(config_directory: Path) -> dict[str, object]:
    print("\nRemote Gateway setup requires a private bind address, a TLS certificate")
    print("for the hostname remote UIs use, its private key, and a shared token.")
    print("Prefer a Tailscale IP/private network. Wildcard bind addresses are rejected.")
    listen_host = _ask("Gateway bind address (for example, its Tailscale IP)")
    while not listen_host:
        listen_host = _ask("Gateway bind address")
    port = _port("Gateway TCP port", DEFAULT_GATEWAY_PORT)
    certificate = _path("TLS certificate chain path")
    private_key = _path("TLS private key path")
    token = _path("Gateway token path", config_directory / "gateway.token")
    assert certificate is not None and private_key is not None and token is not None
    _ensure_token(token)
    certificate_host = _ask("Hostname in the TLS certificate (used by remote UIs)")
    while not certificate_host:
        certificate_host = _ask("Hostname in the TLS certificate")
    ca_file = _path(
        "Public CA certificate remote UIs need (blank for a publicly trusted certificate)",
        required=False,
    )
    return {
        "listen_host": listen_host,
        "port": port,
        "certificate": certificate,
        "private_key": private_key,
        "token": token,
        "certificate_host": certificate_host,
        "ca_file": ca_file,
    }


def _remote_ui_details(config_directory: Path) -> dict[str, object]:
    from tfr.gateway_profiles import (
        GatewayConnectionProfile,
        GatewayProfileStore,
        redeem_ui_pairing,
    )

    profiles = GatewayProfileStore()
    if profiles.active is not None:
        raise ValueError(
            "a Gateway profile already exists; use /gateway repair or remove it before fresh setup"
        )
    recovery = _path("UI recovery bundle path (blank to pair from an existing UI)", required=False)
    if recovery is not None:
        same_ui = _yes_no(
            "Is this restoring the same UI identity rather than adding a new UI?",
            default=True,
        )
        platform = socket.gethostname().strip() or "Unknown device"
        ui_name = None
        if not same_ui:
            proposed = f"{platform} · Terminal"[:80]
            ui_name = _ask("New UI display name", proposed)
        profile = profiles.restore_recovery(
            recovery,
            recover_identity=same_ui,
            ui_name=ui_name,
        )
        print(f"Restored Gateway profile {profile.name} from {recovery}.")
        print("The recovery bundle contains a live Gateway credential; keep or delete it securely.")
        return {
            "host": profile.host,
            "port": profile.port,
            "ca_file": Path(profile.tls_ca) if profile.tls_ca else None,
            "tls_server_name": profile.tls_server_name,
        }
    print("\nOpen an existing authenticated TFR UI and run /ui pair.")
    print("That UI will display a single-use pairing code valid for 30 minutes.")
    print("No Gateway token file or SSH access is needed on this machine.")
    host = _ask("Gateway host or private IP")
    while not host:
        host = _ask("Gateway host or private IP")
    port = _port("Gateway TCP port", DEFAULT_GATEWAY_PORT)
    ca_file = _path("Copied public CA certificate path (blank if not needed)", required=False)
    tls_server_name = _ask("TLS certificate hostname (blank when it is the same as Gateway host)")
    pairing_code = _ask("Pairing code from the existing UI")
    while not pairing_code:
        pairing_code = _ask("Pairing code from the existing UI")
    ui_id = uuid4()
    server_name = tls_server_name or host
    token = asyncio.run(
        redeem_ui_pairing(
            host,
            port,
            pairing_code,
            ui_id,
            tls_server_name=server_name,
            tls_ca=ca_file,
        )
    )
    platform = socket.gethostname().strip() or "Unknown device"
    ui_name = f"{platform} · Terminal"[:80]
    profile = GatewayConnectionProfile(
        name="default",
        host=host,
        port=port,
        tls_server_name=server_name,
        tls_ca=str(ca_file) if ca_file is not None else None,
        credential_reference="credential:pending",
        ui_id=ui_id,
        ui_name=ui_name,
    )
    profiles.add(profile, token)
    print(f"Authenticated to Gateway {host}:{port} and saved the remote UI profile.")
    return {
        "host": host,
        "port": port,
        "ca_file": ca_file,
        "tls_server_name": server_name,
    }


def _install_release(repository: Path) -> None:
    print("\nInstalling the latest verified stable TFR release...")
    result = subprocess.run(
        [str(repository / "scripts" / "install-from-checkout"), "--latest-stable"],
        check=False,
    )
    if result.returncode:
        raise RuntimeError("stable TFR installation failed")


def _configure_main(repository: Path, config_directory: Path) -> None:
    source = repository / "examples" / "config.jsonc"
    target = config_directory / "config.jsonc"
    content = source.read_text(encoding="utf-8").replace(
        '"$schema": "../schemas/config.schema.json"',
        f'"$schema": "{SCHEMA_ROOT}/config.schema.json"',
        1,
    )
    _write_file(target, content, mode=0o600, description="main config")
    print(f"Public plugin options: {PUBLIC_PLUGINS_URL}")


def _launcher_arguments(
    profile: Profile,
    tfr: Path,
    config: Path,
    details: dict[str, object] | None,
    gateway_socket: Path,
) -> tuple[str, list[str], list[str]]:
    base = [str(tfr)]
    doctor = [
        str(tfr),
        "doctor",
        "--profile",
        profile,
        "--config",
        str(config),
        "--socket",
        str(gateway_socket),
    ]
    if profile == "all-in-one":
        return "tfr-local", [*base, "--config", str(config)], doctor
    if profile == "local-split":
        raise ValueError("local-split has two launchers")
    if profile == "gateway":
        assert details is not None
        shared = [
            "--listen-host",
            str(details["listen_host"]),
            "--listen-port",
            str(details["port"]),
            "--token-file",
            str(details["token"]),
            "--tls-cert",
            str(details["certificate"]),
            "--tls-key",
            str(details["private_key"]),
        ]
        return (
            "tfr-gateway",
            [
                *base,
                "gateway",
                "--config",
                str(config),
                "--socket",
                str(gateway_socket),
                *shared,
            ],
            [*doctor, *shared],
        )
    assert details is not None
    return (
        "tfr-remote-ui",
        [*base, "ui"],
        doctor,
    )


def _write_launchers(
    profile: Profile,
    bin_directory: Path,
    tfr: Path,
    config: Path,
    details: dict[str, object] | None,
    gateway_socket: Path,
) -> list[Path]:
    launchers: list[tuple[str, list[str]]]
    if profile == "local-split":
        launchers = [
            (
                "tfr-gateway",
                [
                    str(tfr),
                    "gateway",
                    "--config",
                    str(config),
                    "--socket",
                    str(gateway_socket),
                ],
            ),
            (
                "tfr-ui",
                [str(tfr), "ui", "--config", str(config), "--socket", str(gateway_socket)],
            ),
            (
                "tfr-doctor",
                [
                    str(tfr),
                    "doctor",
                    "--profile",
                    profile,
                    "--config",
                    str(config),
                    "--socket",
                    str(gateway_socket),
                ],
            ),
        ]
    else:
        name, command, doctor = _launcher_arguments(profile, tfr, config, details, gateway_socket)
        launchers = [(name, command), ("tfr-doctor", doctor)]
    written: list[Path] = []
    for name, command in launchers:
        path = bin_directory / name
        if _write_file(
            path,
            launcher_script(command),
            mode=0o700,
            description=f"{name} launcher",
            replace_confirmed=name == "tfr-doctor",
        ):
            written.append(path)
    return written


def _configure_gateway_service(
    profile: Profile,
    *,
    launcher: Path,
    state_directory: Path,
) -> bool:
    definition = service_definition(launcher, state_directory)
    if definition is None:
        if profile in {"local-split", "gateway"}:
            print("Automatic Gateway services are unavailable on this platform.")
        return False
    if profile not in {"local-split", "gateway"}:
        if (
            definition.path.exists()
            and service_file_is_managed(definition.path)
            and _yes_no(
                f"Disable and remove the existing TFR Gateway service at {definition.path}?",
                default=False,
            )
        ):
            remove_service(definition)
            print(f"Removed {definition.path}")
        return False
    manager = definition.status_command[0]
    if shutil.which(manager) is None:
        print(f"Automatic Gateway service setup is unavailable because {manager} was not found.")
        return False
    if not _yes_no("Run the Gateway automatically as a user service?", default=True):
        print("Keeping launcher-only Gateway operation.")
        return False
    if definition.path.exists():
        if not service_file_is_managed(definition.path):
            raise RuntimeError(
                f"refusing to replace non-TFR service definition at {definition.path}"
            )
        current = definition.path.read_text(encoding="utf-8")
        if current != definition.content and not _yes_no(
            f"Replace changed TFR Gateway service definition at {definition.path}?",
            default=False,
        ):
            print(f"Keeping {definition.path}")
            return False
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RuntimeError(f"Gateway launcher is unavailable or not executable: {launcher}")
    if str(stable_gateway_socket(state_directory)) not in launcher.read_text(encoding="utf-8"):
        raise RuntimeError(
            "Gateway launcher does not use the stable service socket; rerun setup and "
            "approve launcher replacement"
        )
    if (
        not definition.path.exists()
        or definition.path.read_text(encoding="utf-8") != definition.content
    ):
        _write_file(
            definition.path,
            definition.content,
            mode=0o644,
            description="TFR Gateway service definition",
            replace_confirmed=True,
        )
    print("Service commands:")
    for command in definition.install_commands:
        print("  " + shlex.join(command))
    if not _yes_no("Enable and start the TFR Gateway user service now?", default=False):
        print("Service definition installed but not started.")
        return False
    install_service(definition)
    print("TFR Gateway user service is enabled and started.")
    if definition.kind == "systemd-user":
        print("For operation after logout, enable lingering if needed:")
        print(f"  loginctl enable-linger {shlex.quote(os.environ.get('USER', str(os.getuid())))}")
    print(f"Service logs: {definition.log_hint}")
    return True


def _gateway_handoff(config_directory: Path, details: dict[str, object]) -> None:
    ca_file = details.get("ca_file")
    content = (
        "TFR remote UI handoff (this file contains no token value)\n"
        f"Gateway private bind address: {details['listen_host']}\n"
        f"Gateway TLS hostname: {details['certificate_host']}\n"
        f"Gateway port: {details['port']}\n"
        f"Gateway token source: {details['token']}\n"
        f"Public CA source: {ca_file or 'not required (publicly trusted certificate)'}\n\n"
        "Copy only the token and, when listed, the public CA certificate to the UI host.\n"
        "Do not copy the Gateway TLS private key, worlds.jsonc, agents.jsonc, world\n"
        "passwords, or provider keys. Restrict token permissions to 0600. Keep Gateway\n"
        "and UI TFR versions compatible and allow the configured port through your\n"
        "private-network firewall/Tailscale ACL.\n"
    )
    _write_file(
        config_directory / "remote-ui-handoff.txt",
        content,
        mode=0o600,
        description="remote UI handoff",
    )


def run(repository: Path) -> int:
    if not sys.stdin.isatty():
        print("setup-tfr: interactive terminal input is required", file=sys.stderr)
        return 2
    profile = _profile()
    config_directory = _default_config_directory()
    state_directory = _deployment_path().parent
    gateway_socket = stable_gateway_socket(state_directory)
    bin_directory = Path.home() / ".local" / "bin"
    tfr = bin_directory / "tfr"
    try:
        _install_release(repository)
        config_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_directory.chmod(0o700)
        # Remote UIs use generated connection bootstrap only; they never need
        # a preferences JSONC file. Gateway/standalone profiles retain the
        # installer input as a one-time managed-configuration import source.
        if profile != "remote-ui":
            _configure_main(repository, config_directory)
        details: dict[str, object] | None = None
        if profile != "remote-ui":
            _configure_worlds(repository, config_directory)
        if profile == "gateway":
            details = _gateway_details(config_directory)
            _gateway_handoff(config_directory, details)
        elif profile == "remote-ui":
            details = _remote_ui_details(config_directory)
        launchers = _write_launchers(
            profile,
            bin_directory,
            tfr,
            config_directory / "config.jsonc",
            details,
            gateway_socket,
        )
        gateway_launcher = bin_directory / "tfr-gateway"
        service_started = _configure_gateway_service(
            profile,
            launcher=gateway_launcher,
            state_directory=state_directory,
        )
        _save_deployment(profile, gateway_socket)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"setup-tfr: {exc}", file=sys.stderr)
        return 2

    print(
        "\nSetup complete. Gateway user service started."
        if service_started
        else "\nSetup complete. Nothing was started automatically."
    )
    if profile == "all-in-one":
        print(f"Start TFR: {bin_directory / 'tfr-local'}")
    elif profile == "local-split":
        if service_started:
            print("Gateway:        running as a user service")
            print(f"Start UI:       {bin_directory / 'tfr-ui'}")
        else:
            print(f"First terminal:  {bin_directory / 'tfr-gateway'}")
            print(f"Second terminal: {bin_directory / 'tfr-ui'}")
    elif profile == "gateway":
        if service_started:
            print("Gateway:      running as a user service")
        else:
            print(f"Start Gateway: {bin_directory / 'tfr-gateway'}")
        print(f"UI handoff:   {config_directory / 'remote-ui-handoff.txt'}")
    else:
        print(f"Start remote UI: {bin_directory / 'tfr-remote-ui'}")
        print("UI preferences are Gateway-managed; no local config.jsonc was generated.")
    print(f"Check setup: {bin_directory / 'tfr-doctor'}")
    if str(bin_directory) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Add {bin_directory} to PATH for launcher names without full paths.")
    if launchers:
        print("Generated launchers: " + ", ".join(str(path) for path in launchers))
    print("Rerun setup-tfr later to switch deployment profiles.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive stable TFR setup")
    parser.add_argument("--repository", type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.repository.resolve()))


if __name__ == "__main__":
    main()
