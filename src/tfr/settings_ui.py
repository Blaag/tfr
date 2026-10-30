from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence

_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def setting_category(setting: Mapping[str, object]) -> str:
    key = str(setting.get("key", ""))
    parts = key.split(".")
    if len(parts) >= 3 and parts[:2] == ["plugins", "config"]:
        return f"Plugin: {parts[2].replace('_', ' ').title()}"
    return (parts[0] if parts else "other").replace("_", " ").title()


def setting_label(key: str) -> str:
    parts = key.split(".")
    if len(parts) >= 4 and parts[:2] == ["plugins", "config"]:
        parts = parts[3:]
    elif len(parts) > 1:
        parts = parts[1:]
    return " › ".join(part.replace("_", " ").title() for part in parts)


def editable_settings(
    settings: Sequence[Mapping[str, object]],
    *,
    worlds: Sequence[str] = (),
) -> list[dict[str, object]]:
    output = []
    for source in settings:
        scope = source.get("scope")
        if source.get("sensitive") is True or scope in {"bootstrap", "secret"}:
            continue
        item = copy.deepcopy(dict(source))
        key = str(item.get("key", ""))
        if "*" in key:
            if not worlds or not key.startswith("worlds.*."):
                continue
            for world in worlds:
                concrete = copy.deepcopy(item)
                concrete["key"] = key.replace("worlds.*.", f"worlds.{world}.", 1)
                concrete["template_key"] = key
                output.append(concrete)
            continue
        output.append(item)
    return sorted(output, key=lambda item: (setting_category(item), str(item.get("key", ""))))


def parse_setting_value(raw: str, value_type: str) -> object:
    if value_type == "string":
        return raw
    if value_type == "boolean":
        normalized = raw.strip().casefold()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
        raise ValueError("enter true or false")
    if value_type == "integer":
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError("enter a whole number") from exc
    if value_type == "number":
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError("enter a number") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        label = "a JSON string array" if value_type == "string-list" else "valid JSON"
        raise ValueError(f"enter {label}") from exc
    if value_type == "string-list" and (
        not isinstance(value, list) or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError("enter a JSON string array")
    return value


def setting_destination(key: str, declared_scope: str) -> tuple[str, str | None]:
    scope = "ui" if declared_scope == "ui-type" else declared_scope
    if scope == "world" and key.startswith("worlds."):
        return scope, key.split(".", 2)[1]
    if scope == "plugin" and key.startswith("plugins.config."):
        return scope, key.split(".", 3)[2]
    return scope, None


def setting_value_text(value: object, value_type: str) -> str:
    if value is None:
        return ""
    if value_type == "string":
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def speaker_rule_label(rule: Mapping[str, object]) -> str:
    speaker = str(rule.get("speaker") or "Unnamed speaker")
    effect = str(rule.get("effect") or "no effect").replace("_", " ").title()
    color = rule.get("color")
    worlds = rule.get("worlds")
    suffix = f" · {color}" if isinstance(color, str) else ""
    if isinstance(worlds, list) and worlds:
        suffix += " · " + ", ".join(str(world) for world in worlds)
    return f"{speaker} — {effect}{suffix}"


def validate_speaker_rule(rule: Mapping[str, object]) -> dict[str, object]:
    value = copy.deepcopy(dict(rule))
    speaker = value.get("speaker")
    effect = value.get("effect")
    if not isinstance(speaker, str) or not speaker.strip():
        raise ValueError("speaker name is required")
    if not isinstance(effect, str) or not effect:
        raise ValueError("effect is required")
    value["speaker"] = speaker.strip()
    for field in ("color", "accent_color", "end_color"):
        color = value.get(field)
        if color is not None and (not isinstance(color, str) or _COLOR.fullmatch(color) is None):
            raise ValueError(f"{field.replace('_', ' ')} must be #RRGGBB")
    for field in ("duration_seconds", "period_seconds", "repeat_seconds", "step_seconds"):
        number = value.get(field)
        if number is not None and (
            not isinstance(number, (int, float)) or isinstance(number, bool) or number <= 0
        ):
            raise ValueError(f"{field.replace('_', ' ')} must be positive")
    trail = value.get("trail_width")
    if trail is not None and (
        not isinstance(trail, (int, float)) or isinstance(trail, bool) or trail <= 0
    ):
        raise ValueError("trail width must be positive")
    for field in ("worlds", "kinds"):
        entries = value.get(field)
        if entries is not None and (
            not isinstance(entries, list) or any(not isinstance(item, str) for item in entries)
        ):
            raise ValueError(f"{field} must be a list of names")
    kinds = value.get("kinds")
    if isinstance(kinds, list) and not set(kinds) <= {"say", "pose"}:
        raise ValueError("message kinds support only Say and Pose")
    return value


def speaker_rules(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError("speaker effects rules are not a list of objects")
    rules = []
    for index, item in enumerate(value):
        try:
            rules.append(validate_speaker_rule(item))
        except ValueError as exc:
            speaker = item.get("speaker") if isinstance(item, Mapping) else None
            label = f" ({speaker})" if isinstance(speaker, str) and speaker else ""
            raise ValueError(f"speaker rule {index + 1}{label}: {exc}") from exc
    return rules
