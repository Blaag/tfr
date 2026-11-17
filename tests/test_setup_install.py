from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tfr.config import WorldsConfig, load_gateway_bootstrap
from tfr.service_management import service_definition
from tfr.setup_install import (
    SCHEMA_ROOT,
    _apply_recovery_import,
    _configure_gateway_service,
    _configure_recovery_bootstrap,
    _configure_worlds,
    _install_release,
    _load_gateway_recovery,
    _load_recovery_progress,
    _phase_complete,
    _save_deployment,
    _save_recovery_progress,
    _world_entry,
    _write_launchers,
    generate_worlds_config,
    launcher_script,
    run,
)


def test_setup_wrapper_uses_locked_project_dependencies() -> None:
    wrapper = (Path(__file__).parents[1] / "scripts/setup-tfr").read_text(encoding="utf-8")

    assert '--isolated --frozen --project "$repository"' in wrapper
    assert "--no-project" not in wrapper


def test_generated_worlds_config_is_valid_and_escapes_credentials() -> None:
    content = generate_worlds_config(
        {
            "example": {
                "aliases": ["ex"],
                "host": "world.example.org",
                "port": 4201,
                "server": "tinymux",
                "capabilities": {"unicode": True},
                "tls": {"enabled": True, "verify": True},
                "login": {"character": 'A "Name"', "password": "secret\\value"},
                "autoconnect": True,
                "provenance": {"nospoof": True, "show_prefix": False},
                "startup_commands": [],
            }
        }
    )

    value = json.loads(content)
    worlds = WorldsConfig.model_validate(value)

    assert worlds.worlds["example"].login is not None
    assert worlds.worlds["example"].login.password.get_secret_value() == "secret\\value"


def test_generated_launcher_quotes_paths_and_forwards_arguments(tmp_path: Path) -> None:
    executable = tmp_path / "TFR bin" / "tfr"
    token = tmp_path / "a token"
    script = launcher_script([str(executable), "ui", "--token-file", str(token)])

    assert f"'{executable}'" in script
    assert f"'{token}'" in script
    assert "\n+  " not in script
    assert script.endswith('"$@"\n')


def test_local_split_launchers_share_explicit_stable_socket(tmp_path: Path) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    socket = tmp_path / "state" / "run" / "gateway.sock"

    written = _write_launchers(
        "local-split",
        binary,
        tmp_path / "tfr",
        tmp_path / "config.jsonc",
        None,
        socket,
    )

    assert {path.name for path in written} == {"tfr-gateway", "tfr-ui", "tfr-doctor"}
    for path in written:
        content = path.read_text(encoding="utf-8")
        assert "--socket" in content
        assert str(socket) in content


def test_remote_ui_deployment_marker_remains_compatible_with_older_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    _save_deployment("remote-ui", tmp_path / "ignored.sock")

    value = json.loads((tmp_path / "tfr/deployment.json").read_text(encoding="utf-8"))
    assert value == {"schema_version": 1, "profile": "remote-ui"}


def test_gateway_recovery_archive_must_be_private(tmp_path: Path) -> None:
    archive = tmp_path / "recovery.json"
    archive.write_text("{}\n", encoding="utf-8")
    archive.chmod(0o644)

    with pytest.raises(ValueError, match="mode 0600 or stricter"):
        _load_gateway_recovery(archive)


def test_recovery_progress_is_private_resumable_and_archive_bound(tmp_path: Path) -> None:
    path = tmp_path / "settings/recovery-setup.json"

    _save_recovery_progress(path, "a" * 64, "service-started")

    assert path.stat().st_mode & 0o777 == 0o600
    assert _load_recovery_progress(path, "a" * 64) == "service-started"
    assert _phase_complete("service-started", "state-restored") is True
    assert _phase_complete("service-started", "configuration-imported") is False
    with pytest.raises(ValueError, match="does not match"):
        _load_recovery_progress(path, "b" * 64)


def test_setup_defers_import_restart_to_service_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = tmp_path / "gateway.sock"
    socket.touch()
    calls: list[tuple[str, ...]] = []

    def execute(arguments: list[str], **_values: object):
        calls.append(tuple(arguments))
        return subprocess.CompletedProcess(arguments, 0, "Imported\n", "")

    monkeypatch.setattr("tfr.setup_install.subprocess.run", execute)

    _apply_recovery_import(tmp_path / "tfr", socket, tmp_path / "recovery.json")

    assert "--defer-restart" in calls[0]


def test_recovery_setup_resumes_after_completed_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    recovery_path = tmp_path / "recovery.json"
    recovery_path.write_text("{}", encoding="utf-8")
    details = {
        "listen_host": "100.64.0.1",
        "port": 7347,
        "certificate": tmp_path / "gateway.crt",
        "private_key": tmp_path / "gateway.key",
        "token": tmp_path / "gateway.token",
        "certificate_host": "gateway.example.ts.net",
        "ca_file": None,
        "web_origin": "https://gateway.example.ts.net",
        "web_transport": "tailscale",
        "web_target": "http://127.0.0.1:7348",
        "web": {},
    }
    calls: list[str] = []
    monkeypatch.setattr("tfr.setup_install.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("tfr.setup_install._profile", lambda: "gateway")
    monkeypatch.setattr(
        "tfr.setup_install._load_gateway_recovery",
        lambda _path: ({}, {"recovery": True}, "a" * 64),
    )
    monkeypatch.setattr(
        "tfr.setup_install._install_release", lambda *_args, **_kwargs: calls.append("install")
    )
    monkeypatch.setattr(
        "tfr.setup_install.restore_gateway_recovery",
        lambda *_args, **_kwargs: calls.append("restore") or details,
    )
    monkeypatch.setattr(
        "tfr.setup_install.gateway_recovery_details", lambda *_args, **_kwargs: details
    )
    monkeypatch.setattr("tfr.setup_install._configure_recovery_bootstrap", lambda *_args: None)
    monkeypatch.setattr("tfr.setup_install._gateway_handoff", lambda *_args, **_kwargs: None)

    def launchers(*_args: object) -> list[Path]:
        calls.append("launchers")
        launcher = tmp_path / ".local/bin/tfr-gateway"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.touch()
        return [launcher]

    monkeypatch.setattr("tfr.setup_install._write_launchers", launchers)
    monkeypatch.setattr(
        "tfr.setup_install._configure_gateway_service",
        lambda *_args, **_kwargs: calls.append("service") or True,
    )
    monkeypatch.setattr("tfr.setup_install._save_deployment", lambda *_args: None)
    monkeypatch.setattr(
        "tfr.setup_install._apply_recovery_import", lambda *_args: calls.append("import")
    )
    monkeypatch.setattr(
        "tfr.setup_install._restart_recovered_service", lambda *_args: calls.append("restart")
    )
    monkeypatch.setattr(
        "tfr.setup_install._restore_tailscale_route", lambda *_args: calls.append("route")
    )
    monkeypatch.setattr(
        "tfr.setup_install._verify_recovered_gateway", lambda *_args: calls.append("verify")
    )

    assert run(tmp_path, recovery_path=recovery_path, candidate=True) == 0
    assert run(tmp_path, recovery_path=recovery_path, candidate=True) == 0

    assert calls == [
        "install",
        "restore",
        "launchers",
        "service",
        "import",
        "restart",
        "route",
        "verify",
    ]


def test_candidate_setup_installs_exact_tagged_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def execute(arguments: tuple[str, ...] | list[str], **_values: object):
        command = tuple(arguments)
        calls.append(command)
        if "tag" in command:
            return subprocess.CompletedProcess(command, 0, "v0.1.124-candidate.1\n", "")
        if "ls-remote" in command:
            return subprocess.CompletedProcess(command, 0, "candidate tag\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tfr.setup_install.subprocess.run", execute)
    monkeypatch.setattr("tfr.setup_install.shutil.which", lambda _name: "/usr/bin/git")

    _install_release(tmp_path, candidate=True)

    assert calls[-1] == (str(tmp_path / "scripts/install-from-checkout"),)


def test_candidate_setup_rejects_untagged_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tfr.setup_install.subprocess.run",
        lambda arguments, **_values: subprocess.CompletedProcess(arguments, 0, "", ""),
    )
    monkeypatch.setattr("tfr.setup_install.shutil.which", lambda _name: "/usr/bin/git")

    with pytest.raises(RuntimeError, match="published candidate tag"):
        _install_release(tmp_path, candidate=True)


def test_candidate_setup_rejects_local_only_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def execute(arguments: tuple[str, ...], **_values: object):
        if "tag" in arguments:
            return subprocess.CompletedProcess(arguments, 0, "v0.1.124-candidate.1\n", "")
        return subprocess.CompletedProcess(arguments, 2, "", "not found")

    monkeypatch.setattr("tfr.setup_install.subprocess.run", execute)
    monkeypatch.setattr("tfr.setup_install.shutil.which", lambda _name: "/usr/bin/git")

    with pytest.raises(RuntimeError, match="not published on origin"):
        _install_release(tmp_path, candidate=True)


def test_recovery_bootstrap_is_canonical_and_uses_standard_state(tmp_path: Path) -> None:
    config = tmp_path / "config"
    state = tmp_path / "state"
    config.mkdir()

    _configure_recovery_bootstrap(
        config,
        state,
        {
            "web": {
                "enabled": True,
                "mode": "on-demand",
                "transport": "direct",
                "origin": None,
                "listen_host": "0.0.0.0",
                "listen_port": 7348,
                "direct_host": "gateway.example.com",
                "direct_port": 7443,
                "tls_certificate": "certificate",
                "tls_private_key": "key",
            }
        },
    )

    document = json.loads((config / "config.jsonc").read_text(encoding="utf-8"))
    assert document == {
        "$schema": f"{SCHEMA_ROOT}/config.schema.json",
        "schema_version": 1,
        "state_directory": str(state / "settings"),
        "web_tls_cert": str(config / "web-gateway.crt"),
        "web_tls_key": str(config / "web-gateway.key"),
    }
    bootstrap = load_gateway_bootstrap(config / "config.jsonc")
    assert bootstrap.legacy is False
    assert bootstrap.warnings == ()
    assert bootstrap.web_tls_cert == config / "web-gateway.crt"
    assert bootstrap.web_tls_key == config / "web-gateway.key"


def test_gateway_service_requires_separate_start_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "bin/tfr-gateway"
    launcher.parent.mkdir()
    launcher.write_text(f"#!/bin/sh\n# {tmp_path / 'state/run/gateway.sock'}\n", encoding="utf-8")
    launcher.chmod(0o700)
    definition = service_definition(
        launcher,
        tmp_path / "state",
        platform_name="linux",
        home=tmp_path,
        uid=501,
    )
    assert definition is not None
    answers = iter((True, False))
    started = False

    def start(_definition: object) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr("tfr.setup_install.service_definition", lambda *_args: definition)
    monkeypatch.setattr("tfr.setup_install.shutil.which", lambda _name: "/bin/systemctl")
    monkeypatch.setattr("tfr.setup_install._yes_no", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr("tfr.setup_install.install_service", start)

    _configure_gateway_service("gateway", launcher=launcher, state_directory=tmp_path / "state")

    assert definition.path.read_text(encoding="utf-8") == definition.content
    assert started is False


def test_gateway_service_refuses_foreign_definition(
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
    definition.path.write_text("foreign service\n", encoding="utf-8")
    monkeypatch.setattr("tfr.setup_install.service_definition", lambda *_args: definition)
    monkeypatch.setattr("tfr.setup_install.shutil.which", lambda _name: "/bin/systemctl")
    monkeypatch.setattr("tfr.setup_install._yes_no", lambda *_args, **_kwargs: True)

    with pytest.raises(RuntimeError, match="refusing to replace non-TFR"):
        _configure_gateway_service(
            "gateway",
            launcher=tmp_path / "bin/tfr-gateway",
            state_directory=tmp_path / "state",
        )


def test_rebuilding_worlds_confirms_each_replacement_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    examples = repository / "examples"
    examples.mkdir(parents=True)
    (examples / "worlds.jsonc").write_text(
        '{"$schema": "../schemas/worlds.schema.json", "schema_version": 1, '
        '"defaults": {}, "worlds": {}}\n',
        encoding="utf-8",
    )
    (examples / "agents.jsonc").write_text("{}\n", encoding="utf-8")
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    worlds = config_directory / "worlds.jsonc"
    worlds.write_text("old worlds\n", encoding="utf-8")

    answers = iter([True, False])
    prompts: list[str] = []

    def answer(prompt: str, *, default: bool = True) -> bool:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("tfr.setup_install._yes_no", answer)

    _configure_worlds(repository, config_directory)

    assert len(prompts) == 2
    assert prompts[0].startswith("Replace or rebuild existing worlds file")
    assert prompts[1] == "Configure one or more worlds interactively now?"
    document = json.loads(worlds.read_text(encoding="utf-8"))
    assert document["$schema"] == f"{SCHEMA_ROOT}/worlds.schema.json"
    assert list(config_directory.glob("worlds.jsonc.backup-*"))


def test_world_entry_reprompts_for_invalid_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(
        [
            "example",
            "world.example.org",
            "4201",
            "1",
            "1bad,EX,ex",
            "ex,other-world",
            "n",
            "",
            "n",
            "n",
            "y",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    name, entry = _world_entry(set())

    assert name == "example"
    assert entry["aliases"] == ["ex", "other-world"]
