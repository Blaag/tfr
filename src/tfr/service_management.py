from __future__ import annotations

import contextlib
import os
import plistlib
import shlex
import shutil
import stat
# Fixed service-manager argument arrays are passed directly and never through a shell.
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ServiceKind = Literal["systemd-user", "launch-agent"]

SYSTEMD_UNIT = "tfr-gateway.service"
LAUNCHD_LABEL = "org.tfr.gateway"
MANAGED_MARKER = "Managed by TFR setup"


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    kind: ServiceKind
    path: Path
    content: str
    install_commands: tuple[tuple[str, ...], ...]
    remove_commands: tuple[tuple[str, ...], ...]
    status_command: tuple[str, ...]
    log_hint: str


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    kind: ServiceKind | None
    installed: bool
    managed: bool
    available: bool
    active: bool | None
    enabled: bool | None
    linger: bool | None
    detail: str
    log_hint: str | None


def stable_gateway_socket(state_directory: Path) -> Path:
    return state_directory / "run" / "gateway.sock"


def service_definition(
    launcher: Path,
    state_directory: Path,
    *,
    platform_name: str | None = None,
    home: Path | None = None,
    uid: int | None = None,
) -> ServiceDefinition | None:
    platform_name = platform_name or sys.platform
    home = home or Path.home()
    uid = os.getuid() if uid is None and hasattr(os, "getuid") else (uid or 0)
    if platform_name.startswith("linux"):
        executable = _systemd_quote(str(launcher))
        content = (
            f"# {MANAGED_MARKER}\n"
            "[Unit]\n"
            "Description=TFR Gateway\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={executable}\n"
            "Restart=on-failure\n"
            "RestartSec=5s\n"
            "TimeoutStopSec=30s\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        path = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        return ServiceDefinition(
            kind="systemd-user",
            path=path,
            content=content,
            install_commands=(
                ("systemctl", "--user", "daemon-reload"),
                ("systemctl", "--user", "enable", SYSTEMD_UNIT),
                ("systemctl", "--user", "restart", SYSTEMD_UNIT),
            ),
            remove_commands=(
                ("systemctl", "--user", "disable", "--now", SYSTEMD_UNIT),
                ("systemctl", "--user", "daemon-reload"),
            ),
            status_command=(
                "systemctl",
                "--user",
                "show",
                SYSTEMD_UNIT,
                "--property=ActiveState,UnitFileState",
            ),
            log_hint="journalctl --user-unit=tfr-gateway.service",
        )
    if platform_name == "darwin":
        log_directory = state_directory / "logs"
        document = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [str(launcher)],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Interactive",
            "StandardOutPath": str(log_directory / "gateway-service.log"),
            "StandardErrorPath": str(log_directory / "gateway-service-error.log"),
            "TFRManaged": MANAGED_MARKER,
        }
        content = plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True).decode()
        path = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        domain = f"gui/{uid}"
        service = f"{domain}/{LAUNCHD_LABEL}"
        return ServiceDefinition(
            kind="launch-agent",
            path=path,
            content=content,
            install_commands=(
                ("launchctl", "bootout", service),
                ("launchctl", "bootstrap", domain, str(path)),
                ("launchctl", "enable", service),
                ("launchctl", "kickstart", "-k", service),
            ),
            remove_commands=(("launchctl", "bootout", service),),
            status_command=("launchctl", "print", service),
            log_hint=(
                f"{log_directory / 'gateway-service.log'} and "
                f"{log_directory / 'gateway-service-error.log'}"
            ),
        )
    return None


def service_file_is_managed(path: Path) -> bool:
    try:
        details = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(details.st_mode):
            return False
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return MANAGED_MARKER in content


def install_service(definition: ServiceDefinition) -> None:
    definition.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if definition.kind == "launch-agent":
        Path(definition.log_hint.split(" and ", 1)[0]).parent.mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
    for index, command in enumerate(definition.install_commands):
        allow_absent = definition.kind == "launch-agent" and index == 0
        # Every command is an internally generated fixed tuple.
        result = subprocess.run(  # nosec B603
            command, check=False, capture_output=True, text=True
        )
        if result.returncode and not allow_absent:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"service command failed ({shlex.join(command)}): {detail or result.returncode}"
            )


def remove_service(definition: ServiceDefinition) -> None:
    for command in definition.remove_commands:
        with contextlib.suppress(OSError):
            # Every command is an internally generated fixed tuple.
            subprocess.run(  # nosec B603
                command, check=False, capture_output=True, text=True
            )
    definition.path.unlink(missing_ok=True)


def service_status(definition: ServiceDefinition | None) -> ServiceStatus:
    if definition is None:
        return ServiceStatus(None, False, False, False, None, None, None, "unsupported", None)
    installed = definition.path.exists()
    managed = installed and service_file_is_managed(definition.path)
    executable = definition.status_command[0]
    available = shutil.which(executable) is not None
    active: bool | None = None
    enabled: bool | None = None
    detail = "not installed" if not installed else "installed"
    if installed and available:
        # The status command is internally generated for the selected manager.
        result = subprocess.run(  # nosec B603
            definition.status_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = (result.stdout or result.stderr).strip()
        if definition.kind == "systemd-user":
            values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            active = values.get("ActiveState") == "active"
            enabled = values.get("UnitFileState") in {"enabled", "enabled-runtime"}
        else:
            active = result.returncode == 0
            enabled = active
        detail = output[:500] or f"status command exited {result.returncode}"
    linger = _systemd_linger() if definition.kind == "systemd-user" else None
    return ServiceStatus(
        definition.kind,
        installed,
        managed,
        available,
        active,
        enabled,
        linger,
        detail,
        definition.log_hint,
    )


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_linger() -> bool | None:
    if shutil.which("loginctl") is None:
        return None
    try:
        # loginctl and every argument are fixed locally.
        result = subprocess.run(  # nosec B603
            ("loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    value = result.stdout.strip().casefold()
    return True if value == "yes" else False if value == "no" else None
