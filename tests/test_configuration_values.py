from __future__ import annotations

from pathlib import Path

import pytest

from tfr.config import AgentsConfig, ConfigurationBundle, MainConfig, WorldsConfig
from tfr.configuration_values import (
    ConfigurationValues,
    SettingProvenance,
    configuration_values_from_bundle,
)
from tfr.core_settings import core_configuration_registry
from tfr.managed_settings import ManagedSettingsDocument


def bundle(tmp_path: Path) -> ConfigurationBundle:
    return ConfigurationBundle(
        main_path=tmp_path / "config.jsonc",
        worlds_path=tmp_path / "worlds.jsonc",
        agents_path=tmp_path / "agents.jsonc",
        main=MainConfig.model_validate(
            {
                "ui": {"mouse_mode": "terminal", "spellcheck": {"enabled": False}},
                "web_gateway": {"snapshot_events": 1000},
            }
        ),
        worlds=WorldsConfig.model_validate(
            {
                "defaults": {"encoding": "latin-1"},
                "worlds": {
                    "alpha": {
                        "host": "example.invalid",
                        "port": 4201,
                        "login": {
                            "character": "Black",
                            "password": "super-private-value",
                        },
                        "gag": {"patterns": ["jsonc"]},
                    }
                },
            }
        ),
        agents=AgentsConfig(),
    )


def test_bundle_values_distinguish_defaults_jsonc_and_managed_overrides(tmp_path: Path) -> None:
    managed = ManagedSettingsDocument.model_validate(
        {
            "revision": 7,
            "ui": {"spellcheck": {"enabled": True}},
            "worlds": {"alpha": {"gag": {"patterns": ["managed"]}}},
        }
    )
    values = configuration_values_from_bundle(
        bundle(tmp_path), core_configuration_registry(), managed=managed
    )

    default = values.resolve("ui.recent_input_lines")
    jsonc = values.resolve("ui.mouse_mode")
    managed_ui = values.resolve("ui.spellcheck.enabled")
    managed_world = values.resolve("worlds.alpha.gag.patterns")

    assert default is not None and default.provenance is SettingProvenance.DEFAULT
    assert default.value == 3
    assert jsonc is not None and jsonc.provenance is SettingProvenance.JSONC
    assert jsonc.value == "terminal"
    assert jsonc.source_detail == str(tmp_path / "config.jsonc")
    assert managed_ui is not None and managed_ui.provenance is SettingProvenance.MANAGED
    assert managed_ui.value is True
    assert managed_ui.source_detail == "managed override revision 7"
    assert managed_world is not None and managed_world.value == ["managed"]


def test_runtime_values_have_highest_precedence(tmp_path: Path) -> None:
    values = configuration_values_from_bundle(bundle(tmp_path), core_configuration_registry())

    values.record_runtime("web_gateway.snapshot_events", 250, detail="active listener")
    effective = values.resolve("web_gateway.snapshot_events")

    assert effective is not None
    assert effective.value == 250
    assert effective.provenance is SettingProvenance.RUNTIME
    assert effective.source_detail == "active listener"


def test_sensitive_values_track_source_without_retaining_or_displaying_plaintext(
    tmp_path: Path,
) -> None:
    values = configuration_values_from_bundle(bundle(tmp_path), core_configuration_registry())

    password = values.resolve("worlds.alpha.login.password")

    assert password is not None
    assert password.value is None
    assert password.provenance is SettingProvenance.JSONC
    assert password.as_dict() == {
        "key": "worlds.alpha.login.password",
        "source": "jsonc",
        "source_detail": str(tmp_path / "worlds.jsonc"),
        "value": None,
        "redacted": True,
    }
    assert "super-private-value" not in repr(values)
    assert "super-private-value" not in repr(password)


def test_required_values_without_a_layer_are_unset() -> None:
    values = ConfigurationValues(core_configuration_registry())

    assert values.resolve("worlds.alpha.host") is None
    with pytest.raises(ValueError, match="unknown setting"):
        values.resolve("unknown.setting")
    with pytest.raises(ValueError, match="default values"):
        values.record(
            "ui.mouse_mode",
            "terminal",
            SettingProvenance.DEFAULT,
            source_detail="invalid",
        )


def test_summary_contains_counts_but_no_setting_values(tmp_path: Path) -> None:
    values = configuration_values_from_bundle(bundle(tmp_path), core_configuration_registry())

    summary = values.summary()

    assert summary["setting_count"] > 50  # type: ignore[operator]
    assert summary["template_count"] > 0  # type: ignore[operator]
    assert summary["sensitive_count"] > 0  # type: ignore[operator]
    assert summary["sources"]["jsonc"] > 0  # type: ignore[index]
    assert summary["sources"]["default"] > 0  # type: ignore[index]
    assert "value" not in repr(summary)
    assert "super-private-value" not in repr(summary)


def test_descriptor_list_filters_category_and_scope_without_secret_defaults(
    tmp_path: Path,
) -> None:
    values = configuration_values_from_bundle(bundle(tmp_path), core_configuration_registry())

    worlds = values.list_descriptors("worlds")
    secrets = values.list_descriptors("secret")

    assert worlds
    assert all(setting["category"] == "worlds" for setting in worlds)
    assert secrets
    assert all(setting["scope"] == "secret" for setting in secrets)
    assert all(setting["sensitive"] is True for setting in secrets)
    assert all("default" not in setting for setting in secrets)
    assert all(setting["default_redacted"] is True for setting in secrets)
    assert values.list_descriptors("missing") == []


def test_show_resolves_dynamic_keys_and_redacts_secrets(tmp_path: Path) -> None:
    values = configuration_values_from_bundle(bundle(tmp_path), core_configuration_registry())

    host = values.show("worlds.alpha.host")
    password = values.show("worlds.alpha.login.password")
    unset = ConfigurationValues(core_configuration_registry()).show("worlds.beta.host")

    assert host["descriptor_key"] == "worlds.*.host"
    assert host["value"] == "example.invalid"
    assert host["source"] == "jsonc"
    assert host["scope"] == "world"
    assert host["consumers"] == ["gateway"]
    assert host["restart"] == "live"
    assert password["sensitive"] is True
    assert password["value"] is None
    assert password["redacted"] is True
    assert "default" not in password
    assert unset["source"] == "unset"
    assert unset["value"] is None
    with pytest.raises(ValueError, match="unknown setting"):
        values.show("missing.setting")
    with pytest.raises(
        ValueError,
        match=r"Did you mean: ui\.mouse_mode.*Use /config list ui",
    ):
        values.show("ui.mouse_mod")
