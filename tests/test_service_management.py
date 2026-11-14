from __future__ import annotations

import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tfr.service_management import (
    LAUNCHD_LABEL,
    MANAGED_MARKER,
    SYSTEMD_UNIT,
    install_service,
    service_definition,
    service_file_is_managed,
    service_status,
    stable_gateway_socket,
)


def test_systemd_user_definition_is_bounded_and_shell_free(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launcher = home / ".local" / "bin" / "TFR Gateway"
    state = home / ".local" / "state" / "tfr"

    definition = service_definition(launcher, state, platform_name="linux", home=home, uid=501)

    assert definition is not None and definition.kind == "systemd-user"
    assert definition.path == home / ".config/systemd/user" / SYSTEMD_UNIT
    assert f"# {MANAGED_MARKER}" in definition.content
    assert f'ExecStart="{launcher}"' in definition.content
    assert "Restart=on-failure" in definition.content
    assert definition.install_commands[-1] == (
        "systemctl",
        "--user",
        "restart",
        SYSTEMD_UNIT,
    )
    assert stable_gateway_socket(state) == state / "run/gateway.sock"


def test_launch_agent_definition_uses_arguments_and_private_logs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launcher = home / ".local/bin/tfr-gateway"
    state = home / ".local/state/tfr"

    definition = service_definition(launcher, state, platform_name="darwin", home=home, uid=501)

    assert definition is not None and definition.kind == "launch-agent"
    value = plistlib.loads(definition.content.encode())
    assert value["Label"] == LAUNCHD_LABEL
    assert value["ProgramArguments"] == [str(launcher)]
    assert value["TFRManaged"] == MANAGED_MARKER
    assert value["KeepAlive"] == {"SuccessfulExit": False}
    assert definition.install_commands[1] == (
        "launchctl",
        "bootstrap",
        "gui/501",
        str(definition.path),
    )


def test_service_marker_rejects_foreign_definition(tmp_path: Path) -> None:
    path = tmp_path / "service"
    path.write_text("foreign\n", encoding="utf-8")
    assert service_file_is_managed(path) is False
    path.write_text(f"# {MANAGED_MARKER}\n", encoding="utf-8")
    assert service_file_is_managed(path) is True


def test_install_launch_agent_tolerates_absent_bootout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = service_definition(
        tmp_path / "bin/tfr-gateway",
        tmp_path / "state",
        platform_name="darwin",
        home=tmp_path,
        uid=501,
    )
    assert definition is not None
    definition.path.parent.mkdir(parents=True)
    definition.path.write_text(definition.content, encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0, stdout="", stderr="")

    monkeypatch.setattr("tfr.service_management.subprocess.run", run)
    install_service(definition)
    assert calls == list(definition.install_commands)


def test_systemd_status_reports_active_enabled_and_linger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = service_definition(
        tmp_path / "bin/tfr-gateway",
        tmp_path / "state",
        platform_name="linux",
        home=tmp_path,
        uid=501,
    )
    assert definition is not None
    definition.path.parent.mkdir(parents=True)
    definition.path.write_text(definition.content, encoding="utf-8")
    monkeypatch.setattr("tfr.service_management.shutil.which", lambda _name: "/bin/tool")
    monkeypatch.setattr("tfr.service_management._systemd_linger", lambda: True)
    monkeypatch.setattr(
        "tfr.service_management.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="ActiveState=active\nUnitFileState=enabled\n",
            stderr="",
        ),
    )

    status = service_status(definition)

    assert status.installed is True
    assert status.managed is True
    assert status.active is True
    assert status.enabled is True
    assert status.linger is True
