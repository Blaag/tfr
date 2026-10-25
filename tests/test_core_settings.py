from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from tfr.config import AgentsConfig, MainConfig, WorldDefaults, WorldsConfig
from tfr.configuration_registry import NO_DEFAULT, ConfigurationRegistry, SettingScope
from tfr.core_settings import core_configuration_registry, core_setting_descriptors


def _leaves(value: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    leaves: dict[str, object] = {}
    for name, item in value.items():
        key = f"{prefix}.{name}" if prefix else name
        if isinstance(item, dict) and item:
            leaves.update(_leaves(item, key))
        else:
            leaves[key] = item
    return leaves


def test_core_registry_tracks_current_main_configuration_defaults() -> None:
    registry = core_configuration_registry()
    defaults = _leaves(MainConfig().model_dump(mode="json"))
    defaults.pop("schema_url")

    for key, expected in defaults.items():
        descriptor = registry.get(key)
        assert descriptor is not None, key
        assert descriptor.default is not NO_DEFAULT, key
        assert descriptor.as_dict()["default"] == expected, key


def test_core_registry_tracks_world_defaults_and_dynamic_setting_templates() -> None:
    registry = core_configuration_registry()
    world_defaults = _leaves(WorldDefaults().model_dump(mode="json"), prefix="worlds.defaults")

    for key, expected in world_defaults.items():
        descriptor = registry.get(key)
        assert descriptor is not None
        assert descriptor.as_dict()["default"] == expected

    host = registry.get("worlds.production.host")
    password = registry.get("worlds.production.login.password")
    model = registry.get("agents.greeter.model")
    assert host is not None and host.key == "worlds.*.host"
    assert host.as_dict()["required"] is True
    assert password is not None and password.sensitivity is True
    assert password.scope is SettingScope.SECRET
    assert model is not None and model.key == "agents.*.model"


def test_core_registry_is_unique_sorted_and_read_only_metadata() -> None:
    descriptors = core_setting_descriptors()
    registry = core_configuration_registry()

    assert len(descriptors) >= 90
    assert len({descriptor.key for descriptor in descriptors}) == len(descriptors)
    assert [descriptor.key for descriptor in registry.descriptors()] == sorted(
        descriptor.key for descriptor in descriptors
    )
    with pytest.raises(ValueError, match="duplicate setting descriptor"):
        registry.register(descriptors[0])


def test_registry_prefers_exact_keys_and_rejects_ambiguous_templates() -> None:
    descriptors = core_setting_descriptors()
    registry = core_configuration_registry()
    exact = next(item for item in descriptors if item.key == "ui.mouse_mode")
    assert registry.get("ui.mouse_mode") == exact
    assert registry.get("not.a.setting") is None

    template = next(item for item in descriptors if item.key == "worlds.*.host")
    ambiguous = ConfigurationRegistry((template, replace(template, key="worlds.production.*")))
    with pytest.raises(ValueError, match="ambiguous setting descriptor"):
        ambiguous.get("worlds.production.host")


def test_registry_accounts_for_every_supported_world_setting() -> None:
    registry = core_configuration_registry()
    worlds = WorldsConfig.model_validate(
        {
            "schema_version": 1,
            "defaults": {
                "encoding": "utf-8",
                "reconnect": True,
                "scrollback_lines": 100,
            },
            "worlds": {
                "alpha": {
                    "host": "example.invalid",
                    "port": 4201,
                    "aliases": ["a"],
                    "server": "tinymush",
                    "encoding": "latin-1",
                    "reconnect": False,
                    "scrollback_lines": 200,
                    "tls": {
                        "enabled": True,
                        "verify": True,
                        "ca_file": "/ca.pem",
                        "server_hostname": "example.invalid",
                    },
                    "login": {"character": "Black", "password": "secret"},
                    "autoconnect": True,
                    "provenance": {"nospoof": True, "show_prefix": False},
                    "capabilities": {"unicode": True},
                    "idle": {"after_seconds": 60, "command": "IDLE"},
                    "gag": {"patterns": ["noise"]},
                    "startup_commands": ["look"],
                }
            },
        }
    )
    serialized = worlds.model_dump(mode="json")
    leaves = {
        **_leaves(serialized["defaults"], prefix="worlds.defaults"),
        **_leaves(serialized["worlds"], prefix="worlds"),
        "worlds.schema_version": serialized["schema_version"],
    }

    missing = sorted(key for key in leaves if registry.get(key) is None)

    assert missing == []


def test_registry_accounts_for_every_supported_provider_and_agent_setting() -> None:
    registry = core_configuration_registry()
    agents = AgentsConfig.model_validate(
        {
            "schema_version": 1,
            "providers": {
                "openai": {
                    "base_url": "https://api.example.invalid/v1",
                    "api_key": "secret",
                }
            },
            "agents": {
                "helper": {
                    "world": "agent-world",
                    "provider": "openai",
                    "model": "example-model",
                    "system_prompt": "Be helpful.",
                    "triggers": {"speech": True, "pages": False, "manual": True},
                    "allowed_actions": ["say", "pose"],
                    "context": {
                        "maximum_events": 50,
                        "maximum_characters": 10_000,
                        "include_recent_look": False,
                    },
                    "limits": {
                        "maximum_messages_per_minute": 4,
                        "minimum_seconds_between_turns": 2.0,
                    },
                }
            },
        }
    )
    serialized = agents.model_dump(mode="json")
    leaves = {
        **_leaves(serialized["providers"], prefix="providers"),
        **_leaves(serialized["agents"], prefix="agents"),
        "agents.schema_version": serialized["schema_version"],
    }

    missing = sorted(key for key in leaves if registry.get(key) is None)

    assert missing == []
