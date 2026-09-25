from __future__ import annotations

import os
from pathlib import Path

import pytest

from tfr.config import (
    ConfigurationError,
    WorldConfig,
    WorldsConfig,
    credential_permission_warning,
    default_config_directory,
    default_config_path,
    load_configuration,
    load_ui_configuration,
)

MAIN = """
{
  // Referenced files are relative to this file.
  "$schema": "config.schema.json",
  "schema_version": 1,
  "worlds_file": "worlds.jsonc",
  "agents_file": "agents.jsonc",
  "ui": {"scrollback_lines": 500,},
}
"""

WORLDS = """
{
  "schema_version": 1,
  "worlds": {
    "bot-world": {
      "host": "localhost",
      "port": 4201,
      "login": {"character": "ExampleBot", "password": "world-secret"},
    },
  },
}
"""

AGENTS = """
{
  "schema_version": 1,
  "providers": {
    "local": {"base_url": "http://localhost:11434/v1", "api_key": "api-secret"},
  },
  "agents": {
    "bot": {
      "world": "bot-world",
      "provider": "local",
      "model": "styled-model",
      "system_prompt": "Stay in character.",
    },
  },
}
"""


def write_configuration(directory: Path) -> Path:
    main_path = directory / "config.jsonc"
    main_path.write_text(MAIN, encoding="utf-8")
    (directory / "worlds.jsonc").write_text(WORLDS, encoding="utf-8")
    (directory / "agents.jsonc").write_text(AGENTS, encoding="utf-8")
    for path in directory.iterdir():
        path.chmod(0o600)
    return main_path


def test_default_config_path_uses_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg_config = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

    assert default_config_directory() == xdg_config / "tfr"
    assert default_config_path() == xdg_config / "tfr" / "config.jsonc"


def test_default_config_path_falls_back_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("tfr.config.Path.home", lambda: tmp_path)

    assert default_config_directory() == tmp_path / ".config" / "tfr"
    assert default_config_path() == tmp_path / ".config" / "tfr" / "config.jsonc"


def test_loads_jsonc_and_resolves_references(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)

    bundle = load_configuration(main_path)

    assert bundle.main.ui.scrollback_lines == 500
    assert bundle.main.ui.recent_input_lines == 3
    assert bundle.main.ui.output_color is None
    assert bundle.main.ui.theme.preset == "default"
    assert bundle.main.ui.theme.colors.text is None
    assert bundle.main.ui.animations_enabled is True
    assert bundle.main.ui.low_bandwidth is False
    assert bundle.main.ui.screen_clear.mode == "cycle"
    assert bundle.main.ui.screen_clear.effect is None
    assert bundle.main.ui.boss.mode == "cycle"
    assert bundle.main.ui.boss.screen is None
    assert bundle.main.logging.directory == Path("~/.local/state/tfr/logs").expanduser().resolve()
    assert bundle.main.updates.enabled is True
    assert bundle.main.updates.check_interval_seconds == 21_600
    assert (
        bundle.main.updates.state_directory
        == Path("~/.local/state/tfr/updates").expanduser().resolve()
    )
    assert bundle.main.web_gateway.enabled is False
    assert bundle.main.web_gateway.origin is None
    assert (
        bundle.main.web_gateway.state_directory
        == Path("~/.local/state/tfr/web").expanduser().resolve()
    )
    assert bundle.worlds_path == (tmp_path / "worlds.jsonc").resolve()
    assert bundle.agents_path == (tmp_path / "agents.jsonc").resolve()
    assert bundle.worlds.worlds["bot-world"].login is not None
    assert bundle.worlds.worlds["bot-world"].capabilities.unicode is False
    assert bundle.worlds.worlds["bot-world"].login.password.get_secret_value() == "world-secret"
    assert bundle.agents.providers["local"].api_key.get_secret_value() == "api-secret"
    assert "world-secret" not in repr(bundle)
    assert "api-secret" not in repr(bundle)
    assert bundle.warnings == ()


def test_world_unicode_capability_is_explicitly_enabled() -> None:
    world = WorldConfig(
        host="localhost",
        port=4201,
        capabilities={"unicode": True},
    )

    assert world.capabilities.unicode is True


def test_world_switch_aliases_are_normalized(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    (tmp_path / "worlds.jsonc").write_text(
        WORLDS.replace(
            '"host": "localhost",',
            '"aliases": ["BW", "bot_world"],\n      "host": "localhost",',
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert bundle.worlds.worlds["bot-world"].aliases == ("bw", "bot_world")


def test_rejects_invalid_and_duplicate_world_switch_aliases(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    worlds_path = tmp_path / "worlds.jsonc"
    worlds_path.write_text(
        WORLDS.replace(
            '"host": "localhost",',
            '"aliases": ["/bw"],\n      "host": "localhost",',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="String should match pattern"):
        load_configuration(main_path)

    worlds_path.write_text(
        WORLDS.replace(
            '"bot-world": {',
            '"other-world": {"host": "localhost", "port": 4202, "aliases": ["bw"]},\n'
            '    "bot-world": {"aliases": ["BW"],',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate world-switch alias 'bw'"):
        load_configuration(main_path)


def test_world_switch_alias_limits_are_enforced() -> None:
    with pytest.raises(ValueError, match="at most 64 characters"):
        WorldConfig(host="localhost", port=4201, aliases=("a" * 65,))
    with pytest.raises(ValueError, match="at most 32 items"):
        WorldConfig(
            host="localhost",
            port=4201,
            aliases=tuple(f"alias_{index}" for index in range(33)),
        )

    worlds = {
        f"world-{world_index}": WorldConfig(
            host="localhost",
            port=4201,
            aliases=tuple(f"a{world_index}_{alias_index}" for alias_index in range(32)),
        )
        for world_index in range(9)
    }
    with pytest.raises(ValueError, match="more than 256 switch aliases"):
        WorldsConfig(worlds=worlds)


def test_ui_configuration_does_not_read_world_or_agent_files(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    (tmp_path / "worlds.jsonc").unlink()
    (tmp_path / "agents.jsonc").unlink()

    configuration = load_ui_configuration(main_path)

    assert configuration.main.ui.scrollback_lines == 500


def test_resolves_relative_log_directory_from_main_config(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500,}, "logging": {"directory": "logs"},',
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert bundle.main.logging.directory == (tmp_path / "logs").resolve()


def test_plugins_state_directory_defaults_and_resolves_like_logging(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)

    default_bundle = load_configuration(main_path)

    assert (
        default_bundle.main.plugins.state_directory
        == Path("~/.local/state/tfr/plugins").expanduser().resolve()
    )

    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500,}, "plugins": {"state_directory": "plugins"},',
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert bundle.main.plugins.state_directory == (tmp_path / "plugins").resolve()


def test_updates_require_https_and_resolve_state_directory(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500,}, "updates": {"state_directory": "updates"},',
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert bundle.main.updates.state_directory == (tmp_path / "updates").resolve()

    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500,}, '
            '"updates": {"manifest_url": "http://example.com/manifest.json"},',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="updates.manifest_url must use HTTPS"):
        load_configuration(main_path)


def test_web_gateway_requires_root_https_origin_and_resolves_state(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500,}, "web_gateway": {'
            '"enabled": true, "origin": "https://gateway.example.ts.net", '
            '"state_directory": "web-state"},',
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert bundle.main.web_gateway.canonical_origin == "https://gateway.example.ts.net"
    assert bundle.main.web_gateway.state_directory == (tmp_path / "web-state").resolve()

    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500,}, "web_gateway": {"enabled": true},',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="origin is required"):
        load_configuration(main_path)


def test_plugins_sources_parse_github_shorthand_and_pinned_ref(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500,}, "plugins": {"sources": ['
            '{"repo": "someone/tfr-plugins-fun"},'
            '{"repo": "someone/tfr-plugins-pinned", '
            '"ref": "0123456789abcdef0123456789abcdef01234567", "auto_update": true}'
            "]},",
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert len(bundle.main.plugins.sources) == 2
    first, second = bundle.main.plugins.sources
    assert first.repo == "someone/tfr-plugins-fun"
    assert first.ref is None
    assert first.auto_update is False
    assert second.ref == "0123456789abcdef0123456789abcdef01234567"
    assert second.auto_update is True


def test_plugins_sources_reject_unsafe_repo_value(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500,}, "plugins": {"sources": [{"repo": "-not-a-flag"}]},',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError):
        load_configuration(main_path)


def test_gateway_reconnect_defaults_and_overrides(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)

    default_bundle = load_configuration(main_path)
    defaults = default_bundle.main.ui.gateway_reconnect
    assert defaults.enabled is True
    assert defaults.heartbeat_seconds == 20.0
    assert defaults.ping_timeout_seconds == 8.0
    assert defaults.max_attempts == 5
    assert defaults.retry_interval_seconds == 3.0

    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500, "gateway_reconnect": {'
            '"enabled": false, "heartbeat_seconds": 5, "ping_timeout_seconds": 2, '
            '"max_attempts": 2, "retry_interval_seconds": 1'
            "}},",
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)
    reconnect = bundle.main.ui.gateway_reconnect
    assert reconnect.enabled is False
    assert reconnect.heartbeat_seconds == 5
    assert reconnect.ping_timeout_seconds == 2
    assert reconnect.max_attempts == 2
    assert reconnect.retry_interval_seconds == 1


def test_gateway_reconnect_rejects_non_positive_intervals(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500, "gateway_reconnect": {"heartbeat_seconds": 0}},',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError):
        load_configuration(main_path)


def test_resolves_tls_ca_file_relative_to_worlds_file(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    worlds_path = tmp_path / "worlds.jsonc"
    worlds_path.write_text(
        WORLDS.replace(
            '"port": 4201,',
            '"port": 4201, "tls": {"enabled": true, "ca_file": "certs/ca.pem"},',
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert (
        bundle.worlds.worlds["bot-world"].tls.ca_file == (tmp_path / "certs" / "ca.pem").resolve()
    )


def test_rejects_unknown_keys_with_a_configuration_path(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace('"scrollback_lines": 500,', '"typo": true,'), encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match=r"ui\.typo: Extra inputs are not permitted"):
        load_configuration(main_path)


def test_rejects_invalid_output_color(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace('"scrollback_lines": 500,', '"output_color": "white",'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=r"ui\.output_color: String should match pattern"):
        load_configuration(main_path)


def test_loads_theme_preset_and_semantic_color_overrides(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"theme": {"preset": "catppuccin-mocha", '
            '"colors": {"accent": "#112233", "warning": "#AABBCC"}}},',
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert bundle.main.ui.theme.preset == "catppuccin-mocha"
    assert bundle.main.ui.theme.colors.accent == "#112233"
    assert bundle.main.ui.theme.colors.warning == "#AABBCC"


def test_rejects_unknown_theme_and_invalid_override(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"theme": {"preset": "dracula", "colors": {"accent": "blue"}}},',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=r"ui\.theme\.(preset|colors\.accent)"):
        load_configuration(main_path)


def test_loads_locked_screen_clear_effect(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500, '
            '"screen_clear": {"mode": "locked", "effect": "flame"}},',
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert bundle.main.ui.screen_clear.mode == "locked"
    assert bundle.main.ui.screen_clear.effect == "flame"


def test_screen_clear_effect_accepts_entry_point_name_characters(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"screen_clear": {"mode": "locked", "effect": "Flame.Clear"}},',
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert bundle.main.ui.screen_clear.effect == "Flame.Clear"


def test_rejects_locked_screen_clear_without_effect(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"screen_clear": {"mode": "locked"}},',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="effect is required"):
        load_configuration(main_path)


def test_loads_locked_boss_screen(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"scrollback_lines": 500, '
            '"boss": {"mode": "locked", "screen": "build-dashboard"}},',
        ),
        encoding="utf-8",
    )

    bundle = load_configuration(main_path)

    assert bundle.main.ui.boss.mode == "locked"
    assert bundle.main.ui.boss.screen == "build-dashboard"


def test_rejects_locked_boss_without_screen(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"boss": {"mode": "locked"}},',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="screen is required"):
        load_configuration(main_path)


def test_rejects_unregistrable_locked_boss_name(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    main_path.write_text(
        MAIN.replace(
            '"ui": {"scrollback_lines": 500,},',
            '"ui": {"boss": {"mode": "locked", "screen": "bad.name"}},',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="ui.boss.screen"):
        load_configuration(main_path)


def test_rejects_unknown_agent_world(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    agents_path = tmp_path / "agents.jsonc"
    agents_path.write_text(AGENTS.replace('"bot-world"', '"missing-world"'), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unknown world"):
        load_configuration(main_path)


def test_rejects_unknown_agent_provider(tmp_path: Path) -> None:
    main_path = write_configuration(tmp_path)
    agents_path = tmp_path / "agents.jsonc"
    agents_path.write_text(
        AGENTS.replace('"provider": "local"', '"provider": "missing"'), encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match="unknown provider"):
        load_configuration(main_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are required")
def test_warns_about_open_credential_permissions(tmp_path: Path) -> None:
    path = tmp_path / "worlds.jsonc"
    path.write_text(WORLDS, encoding="utf-8")
    path.chmod(0o644)

    warning = credential_permission_warning(path, contains_credentials=True)

    assert warning is not None
    assert "0644" in warning
    assert "0600" in warning


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are required")
def test_accepts_private_credential_permissions(tmp_path: Path) -> None:
    path = tmp_path / "worlds.jsonc"
    path.write_text(WORLDS, encoding="utf-8")
    path.chmod(0o600)

    assert credential_permission_warning(path, contains_credentials=True) is None
