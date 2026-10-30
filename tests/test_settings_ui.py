from __future__ import annotations

import pytest

from tfr.settings_ui import (
    editable_settings,
    parse_setting_value,
    setting_category,
    setting_destination,
    setting_label,
    speaker_rule_label,
    speaker_rules,
    validate_speaker_rule,
)


def test_settings_are_grouped_and_world_templates_bind_to_active_world() -> None:
    settings = [
        {"key": "ui.animations_enabled", "scope": "ui", "type": "boolean"},
        {"key": "worlds.*.reconnect", "scope": "world", "type": "boolean"},
        {"key": "providers.*.api_key", "scope": "secret", "sensitive": True},
        {
            "key": "plugins.config.speaker_effects.rules",
            "scope": "plugin",
            "type": "json",
        },
    ]

    editable = editable_settings(settings, worlds=("alpha",))

    assert [item["key"] for item in editable] == [
        "plugins.config.speaker_effects.rules",
        "ui.animations_enabled",
        "worlds.alpha.reconnect",
    ]
    assert setting_category(editable[0]) == "Plugin: Speaker Effects"
    assert setting_label("plugins.config.speaker_effects.rules") == "Rules"
    assert setting_destination("worlds.alpha.reconnect", "world") == ("world", "alpha")


@pytest.mark.parametrize(
    ("raw", "value_type", "expected"),
    [
        ("on", "boolean", True),
        ("42", "integer", 42),
        ("1.5", "number", 1.5),
        ('["alpha","beta"]', "string-list", ["alpha", "beta"]),
        ('{"enabled":true}', "object", {"enabled": True}),
    ],
)
def test_setting_value_parser(raw: str, value_type: str, expected: object) -> None:
    assert parse_setting_value(raw, value_type) == expected


def test_speaker_rules_validate_and_have_readable_labels() -> None:
    rule = validate_speaker_rule(
        {
            "speaker": " Barnaby ",
            "effect": "comet",
            "color": "#ffdd76",
            "accent_color": "#ffffff",
            "duration_seconds": 1.2,
            "trail_width": 3,
        }
    )

    assert rule["speaker"] == "Barnaby"
    assert speaker_rule_label(rule) == "Barnaby — Comet · #ffdd76"
    assert speaker_rules([rule]) == [rule]
    with pytest.raises(ValueError, match="#RRGGBB"):
        validate_speaker_rule({**rule, "color": "yellow"})
    with pytest.raises(ValueError, match="only Say and Pose"):
        validate_speaker_rule({**rule, "kinds": ["say", "page"]})
    with pytest.raises(ValueError, match=r"speaker rule 1 \(Barnaby\)"):
        speaker_rules([{**rule, "kinds": ["page"]}])


def test_speaker_rule_rejects_missing_base_color_and_effect_specific_fields() -> None:
    adrick = {
        "speaker": "Adrick",
        "effect": "color_pulse",
        "accent_color": "#5fd7ff",
        "duration_seconds": 1.2,
        "trail_width": 3,
    }

    with pytest.raises(ValueError, match="base color is required"):
        validate_speaker_rule(adrick)
    with pytest.raises(ValueError, match="trail width is supported only"):
        validate_speaker_rule({**adrick, "color": "#005f87"})
