from __future__ import annotations

from dataclasses import replace

from tfr.configuration_registry import SettingDescriptor

SPEAKER_EFFECTS_RULES_FORM: dict[str, object] = {
    "schema_version": 1,
    "editor": "speaker-rules",
    "label": "Speaker effects",
    "description": "Color and animate attributed speaker names.",
    "preview": "effects-lab",
    "item": {
        "label_field": "speaker",
        "fields": [
            {"key": "speaker", "label": "Speaker name", "type": "string", "required": True},
            {
                "key": "effect",
                "label": "Effect",
                "type": "effect",
                "required": True,
            },
            {"key": "color", "label": "Base color", "type": "color"},
            {"key": "accent_color", "label": "Accent color", "type": "color"},
            {
                "key": "duration_seconds",
                "label": "Duration",
                "type": "number",
                "minimum": 0.01,
                "visible_when": {"effect": ["comet", "sparkle", "underline_sweep"]},
            },
            {
                "key": "trail_width",
                "label": "Trail width",
                "type": "number",
                "minimum": 0.01,
                "visible_when": {"effect": ["comet"]},
            },
            {"key": "worlds", "label": "Worlds", "type": "world-multi-select"},
            {
                "key": "kinds",
                "label": "Message kinds",
                "type": "multi-select",
                "choices": ["say", "pose"],
            },
        ],
    },
}


def with_builtin_form_metadata(descriptor: SettingDescriptor) -> SettingDescriptor:
    if descriptor.form is not None:
        return descriptor
    if descriptor.key == "plugins.config.speaker_effects.rules":
        return replace(descriptor, form=SPEAKER_EFFECTS_RULES_FORM)
    return descriptor
