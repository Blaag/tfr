from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tfr.cli import build_parser, run


def write_configuration(directory: Path, *, with_world: bool) -> Path:
    main = directory / "config.jsonc"
    worlds = directory / "worlds.jsonc"
    agents = directory / "agents.jsonc"
    main.write_text(
        '{"schema_version": 1, "worlds_file": "worlds.jsonc", '
        '"agents_file": "agents.jsonc", "logging": {"enabled": false}}',
        encoding="utf-8",
    )
    world_value = '"local": {"host": "localhost", "port": 4201}' if with_world else ""
    worlds.write_text(
        f'{{"schema_version": 1, "worlds": {{{world_value}}}}}',
        encoding="utf-8",
    )
    agents.write_text('{"schema_version": 1}', encoding="utf-8")
    return main


def test_parser_uses_xdg_default_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    args = build_parser().parse_args([])

    assert args.config == tmp_path / "tfr" / "config.jsonc"


def test_check_config_reports_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_configuration(tmp_path, with_world=True)

    result = run(["--check-config", "--config", str(config)])

    assert result == 0
    assert "1 world(s), 0 agent(s)" in capsys.readouterr().out


def test_start_rejects_an_empty_world_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_configuration(tmp_path, with_world=False)

    result = run(["--config", str(config)])

    assert result == 2
    assert "no worlds are configured" in capsys.readouterr().err


def test_start_runs_the_terminal_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_configuration(tmp_path, with_world=True)
    called = False

    async def fake_run_client(_bundle: object) -> int:
        nonlocal called
        called = True
        return 7

    monkeypatch.setattr("tfr.tui.run_client", fake_run_client)

    assert run(["--config", str(config)]) == 7
    assert called


def test_gateway_mode_runs_the_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_configuration(tmp_path, with_world=True)
    socket_path = tmp_path / "gateway.sock"
    called_with: Path | None = None

    async def fake_run_gateway(_bundle: object, path: Path | None) -> int:
        nonlocal called_with
        called_with = path
        return 8

    monkeypatch.setattr("tfr.gateway.run_gateway", fake_run_gateway)

    assert run(["gateway", "--config", str(config), "--socket", str(socket_path)]) == 8
    assert called_with == socket_path


def test_ui_mode_attaches_to_the_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_configuration(tmp_path, with_world=True)
    (tmp_path / "worlds.jsonc").unlink()
    (tmp_path / "agents.jsonc").unlink()
    socket_path = tmp_path / "gateway.sock"
    called_with: Path | None = None

    async def fake_run_gateway_ui(_bundle: object, path: Path | None) -> int:
        nonlocal called_with
        called_with = path
        return 6

    monkeypatch.setattr("tfr.gateway_client.run_gateway_ui", fake_run_gateway_ui)

    assert run(["ui", "--config", str(config), "--socket", str(socket_path)]) == 6
    assert called_with == socket_path


def test_pair_mode_requests_a_device_link_without_loading_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    socket_path = tmp_path / "gateway.sock"
    received: list[object] = []

    async def fake_pair(path: Path | None, label: str) -> str:
        received.extend((path, label))
        return "https://gateway.example.ts.net/#pair=secret"

    monkeypatch.setattr("tfr.gateway_admin.create_device_pairing_url", fake_pair)

    result = run(["pair", "--socket", str(socket_path), "--device-name", "My iPhone"])

    assert result == 0
    assert received == [socket_path, "My iPhone"]
    assert capsys.readouterr().out == "https://gateway.example.ts.net/#pair=secret\n"


def test_devices_mode_lists_paired_devices(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_list(_path: Path | None) -> list[dict[str, object]]:
        return [
            {
                "device_id": "eb4fd272-a20a-444e-8b4d-93f2e0ad2713",
                "label": "My iPhone",
                "scope": "chat",
                "created_at": "2026-09-23T12:00:00Z",
                "expires_at": "2027-03-22T12:00:00Z",
                "allowed_worlds": ["alpha"],
                "tailscale_login": "black@example.com",
            }
        ]

    monkeypatch.setattr("tfr.gateway_admin.list_web_devices", fake_list)

    assert run(["devices"]) == 0
    assert "My iPhone  black@example.com  chat  alpha" in capsys.readouterr().out


def test_gateway_mode_passes_authenticated_tls_listener_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_configuration(tmp_path, with_world=True)
    values: dict[str, object] = {}

    async def fake_run_gateway(
        _bundle: object,
        path: Path | None,
        **options: object,
    ) -> int:
        values.update(options)
        values["path"] = path
        return 8

    monkeypatch.setattr("tfr.gateway.run_gateway", fake_run_gateway)

    result = run(
        [
            "gateway",
            "--config",
            str(config),
            "--listen-host",
            "100.64.0.1",
            "--listen-port",
            "7443",
            "--token-file",
            str(tmp_path / "gateway.token"),
            "--tls-cert",
            str(tmp_path / "gateway.pem"),
            "--tls-key",
            str(tmp_path / "gateway.key"),
        ]
    )

    assert result == 8
    assert values["listen_host"] == "100.64.0.1"
    assert values["listen_port"] == 7443
    assert values["token_file"] == tmp_path / "gateway.token"


def test_ui_mode_passes_remote_gateway_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_configuration(tmp_path, with_world=True)
    (tmp_path / "worlds.jsonc").unlink()
    (tmp_path / "agents.jsonc").unlink()
    values: dict[str, object] = {}

    async def fake_run_gateway_ui(_bundle: object, **options: object) -> int:
        values.update(options)
        return 6

    monkeypatch.setattr("tfr.gateway_client.run_gateway_ui", fake_run_gateway_ui)

    result = run(
        [
            "ui",
            "--config",
            str(config),
            "--gateway-host",
            "gateway.example.test",
            "--gateway-port",
            "7443",
            "--token-file",
            str(tmp_path / "gateway.token"),
            "--tls-ca",
            str(tmp_path / "ca.pem"),
            "--tls-server-name",
            "gateway.example.test",
        ]
    )

    assert result == 6
    assert values["gateway_host"] == "gateway.example.test"
    assert values["gateway_port"] == 7443
    assert values["token_file"] == tmp_path / "gateway.token"


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["ui", "--gateway-host", "remote"], "requires --token-file"),
        (
            ["ui", "--gateway-host", "remote", "--token-file", "token", "--socket", "local"],
            "either --socket or --gateway-host",
        ),
        (["gateway", "--listen-host", "127.0.0.1"], "requires --token-file"),
    ],
)
def test_cli_rejects_incomplete_network_gateway_options(
    arguments: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(arguments) == 2
    assert message in capsys.readouterr().err


def test_replay_runs_without_loading_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "events.jsonl"
    called_with: Path | None = None

    async def fake_run_replay(path: Path) -> int:
        nonlocal called_with
        called_with = path
        return 9

    monkeypatch.setattr("tfr.replay.run_replay", fake_run_replay)

    assert run(["--replay", str(transcript)]) == 9
    assert called_with == transcript


def test_replay_and_config_check_are_mutually_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run(["--replay", str(tmp_path / "events.jsonl"), "--check-config"])

    assert result == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_config_check_rejects_transport_options(capsys: pytest.CaptureFixture[str]) -> None:
    result = run(["gateway", "--check-config", "--listen-host", "127.0.0.1"])

    assert result == 2
    assert "transport options cannot be combined" in capsys.readouterr().err


def test_plugin_rollback_uses_configured_stable_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_configuration(tmp_path, with_world=False)
    config.write_text(
        '{"schema_version": 1, "worlds_file": "worlds.jsonc", '
        '"agents_file": "agents.jsonc", "logging": {"enabled": false}, '
        '"plugins": {"state_directory": "plugin-state", "sources": [{'
        '"repo": "owner/plugins", "policy": "stable-notify", '
        '"manifest_url": "https://example.invalid/plugin-manifest.json"}]}}',
        encoding="utf-8",
    )
    called: list[object] = []

    def fake_rollback(layout: object, **options: object) -> Path:
        called.extend((layout, options))
        return tmp_path / "checkout"

    monkeypatch.setattr("tfr.plugin_releases.rollback_plugin_release", fake_rollback)
    monkeypatch.setattr(
        "tfr.plugin_releases.current_plugin_release",
        lambda *_args, **_kwargs: (
            tmp_path / "checkout",
            SimpleNamespace(version="0.1.0", commit="a" * 40),
        ),
    )

    result = run(
        ["--config", str(config), "--rollback-plugin", "owner/plugins"]
    )

    assert result == 0
    assert called
    assert "Rolled back owner/plugins to 0.1.0" in capsys.readouterr().out


def test_plugin_rollback_rejects_runtime_mode(capsys: pytest.CaptureFixture[str]) -> None:
    result = run(["gateway", "--rollback-plugin", "owner/plugins"])

    assert result == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_plugin_rollback_accepts_a_path_qualified_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_configuration(tmp_path, with_world=False)
    config.write_text(
        '{"schema_version": 1, "worlds_file": "worlds.jsonc", '
        '"agents_file": "agents.jsonc", "logging": {"enabled": false}, '
        '"plugins": {"state_directory": "plugin-state", "sources": ['
        '{"repo": "owner/plugins", "path": "packages/one", "policy": "stable-notify", '
        '"manifest_url": "https://example.invalid/one.json"},'
        '{"repo": "owner/plugins", "path": "packages/two", "policy": "stable-notify", '
        '"manifest_url": "https://example.invalid/two.json"}]}}',
        encoding="utf-8",
    )
    options: dict[str, object] = {}

    def fake_rollback(_layout: object, **received: object) -> Path:
        options.update(received)
        return tmp_path / "checkout"

    monkeypatch.setattr("tfr.plugin_releases.rollback_plugin_release", fake_rollback)
    monkeypatch.setattr(
        "tfr.plugin_releases.current_plugin_release",
        lambda *_args, **_kwargs: (
            tmp_path / "checkout",
            SimpleNamespace(version="0.1.0", commit="a" * 40),
        ),
    )

    result = run(
        ["--config", str(config), "--rollback-plugin", "owner/plugins:packages/two"]
    )

    assert result == 0
    assert options["source_path"] == "packages/two"
    assert options["manifest_url"] == "https://example.invalid/two.json"


def test_version_includes_packaged_commit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "tfr.cli.current_build",
        lambda: SimpleNamespace(version="1.2.3", commit="a" * 40),
    )

    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args(["--version"])

    assert capsys.readouterr().out == "tfr 1.2.3 (aaaaaaaaaaaa)\n"
