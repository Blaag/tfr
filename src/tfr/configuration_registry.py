from __future__ import annotations

import copy
import difflib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any

MAX_SETTING_KEY_CHARACTERS = 160
MAX_SETTING_CONSUMERS = 16
MAX_SETTING_CONSUMER_CHARACTERS = 64
MAX_SETTING_FORM_BYTES = 32_768
_SETTING_KEY = re.compile(r"^(?:[a-z][a-z0-9_-]*|\*)(?:\.(?:[a-z][a-z0-9_-]*|\*))*$")
_CONSUMER = re.compile(r"^[a-z][a-z0-9_.:-]*$")


class SettingType(StrEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    STRING_LIST = "string-list"
    OBJECT = "object"
    JSON = "json"


class SettingScope(StrEnum):
    SHARED = "shared"
    GATEWAY = "gateway"
    UI_TYPE = "ui-type"
    UI = "ui"
    WORLD = "world"
    PLUGIN = "plugin"
    SECRET = "secret"
    BOOTSTRAP = "bootstrap"


class RestartRequirement(StrEnum):
    LIVE = "live"
    CLIENT_RELOAD = "client-reload"
    CLIENT_RESTART = "client-restart"
    GATEWAY_RESTART = "gateway-restart"


class _NoDefault:
    __slots__ = ()


NO_DEFAULT = _NoDefault()


@dataclass(frozen=True, slots=True)
class SettingValidation:
    """Serializable validation constraints shared by core and plugin settings."""

    nullable: bool = False
    choices: tuple[object, ...] = ()
    minimum: int | float | None = None
    maximum: int | float | None = None
    minimum_length: int | None = None
    maximum_length: int | None = None
    pattern: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.nullable, bool):
            raise ValueError("setting validation nullable marker must be a boolean")
        if not isinstance(self.choices, tuple):
            raise ValueError("setting validation choices must be a tuple")
        for name, value in (
            ("minimum_length", self.minimum_length),
            ("maximum_length", self.maximum_length),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"setting validation {name} must be a non-negative integer")
        if (
            self.minimum_length is not None
            and self.maximum_length is not None
            and self.minimum_length > self.maximum_length
        ):
            raise ValueError("setting validation length range is invalid")
        for name, value in (("minimum", self.minimum), ("maximum", self.maximum)):
            if value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
            ):
                raise ValueError(f"setting validation {name} must be numeric")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("setting validation numeric range is invalid")
        if self.pattern is not None:
            if not isinstance(self.pattern, str) or not self.pattern:
                raise ValueError("setting validation pattern must be a non-empty string")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError("setting validation pattern is invalid") from exc

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"nullable": self.nullable}
        for name in (
            "choices",
            "minimum",
            "maximum",
            "minimum_length",
            "maximum_length",
            "pattern",
        ):
            item = getattr(self, name)
            if item not in (None, ()):
                value[name] = list(item) if name == "choices" else copy.deepcopy(item)
        return value


@dataclass(frozen=True, slots=True)
class SettingDescriptor:
    key: str
    value_type: SettingType
    default: object
    scope: SettingScope
    consumers: tuple[str, ...]
    sensitivity: bool = False
    restart: RestartRequirement = RestartRequirement.LIVE
    validation: SettingValidation = field(default_factory=SettingValidation)
    form: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.key, str)
            or len(self.key) > MAX_SETTING_KEY_CHARACTERS
            or _SETTING_KEY.fullmatch(self.key) is None
        ):
            raise ValueError("setting key is invalid")
        if not isinstance(self.value_type, SettingType):
            raise ValueError("setting type is invalid")
        if not isinstance(self.scope, SettingScope):
            raise ValueError("setting scope is invalid")
        if not isinstance(self.restart, RestartRequirement):
            raise ValueError("setting restart requirement is invalid")
        if not isinstance(self.sensitivity, bool):
            raise ValueError("setting sensitivity must be a boolean")
        if (
            not isinstance(self.consumers, tuple)
            or not self.consumers
            or len(self.consumers) > MAX_SETTING_CONSUMERS
            or len(set(self.consumers)) != len(self.consumers)
            or any(
                not isinstance(consumer, str)
                or len(consumer) > MAX_SETTING_CONSUMER_CHARACTERS
                or _CONSUMER.fullmatch(consumer) is None
                for consumer in self.consumers
            )
        ):
            raise ValueError("setting consumers are invalid")
        if not isinstance(self.validation, SettingValidation):
            raise ValueError("setting validation is invalid")
        if self.form is not None:
            if not isinstance(self.form, Mapping) or not _is_json_value(self.form):
                raise ValueError("setting form metadata must be a JSON object")
            if len(json.dumps(self.form, separators=(",", ":")).encode()) > MAX_SETTING_FORM_BYTES:
                raise ValueError("setting form metadata exceeds 32 KiB")
            object.__setattr__(self, "form", _freeze(self.form))
        if self.default is not NO_DEFAULT:
            self.validate(self.default, label="default")
        for choice in self.validation.choices:
            self._validate_type(choice, label="validation choice")
        if self.default is not NO_DEFAULT:
            object.__setattr__(self, "default", _freeze(self.default))
        object.__setattr__(self, "consumers", tuple(sorted(self.consumers)))

    def validate(self, value: object, *, label: str = "value") -> None:
        if value is None:
            if self.validation.nullable:
                return
            raise ValueError(f"setting {self.key} {label} cannot be null")
        self._validate_type(value, label=label)
        validation = self.validation
        if validation.choices and value not in validation.choices:
            raise ValueError(f"setting {self.key} {label} is not an allowed choice")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if validation.minimum is not None and value < validation.minimum:
                raise ValueError(f"setting {self.key} {label} is below its minimum")
            if validation.maximum is not None and value > validation.maximum:
                raise ValueError(f"setting {self.key} {label} exceeds its maximum")
        if isinstance(value, (str, list, tuple)):
            if validation.minimum_length is not None and len(value) < validation.minimum_length:
                raise ValueError(f"setting {self.key} {label} is shorter than its minimum")
            if validation.maximum_length is not None and len(value) > validation.maximum_length:
                raise ValueError(f"setting {self.key} {label} exceeds its maximum length")
        if (
            validation.pattern is not None
            and isinstance(value, str)
            and re.fullmatch(validation.pattern, value) is None
        ):
            raise ValueError(f"setting {self.key} {label} does not match its pattern")

    def _validate_type(self, value: object, *, label: str) -> None:
        valid = {
            SettingType.BOOLEAN: lambda item: isinstance(item, bool),
            SettingType.INTEGER: lambda item: isinstance(item, int) and not isinstance(item, bool),
            SettingType.NUMBER: lambda item: (
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and (not isinstance(item, float) or math.isfinite(item))
            ),
            SettingType.STRING: lambda item: isinstance(item, str),
            SettingType.STRING_LIST: lambda item: (
                isinstance(item, Sequence)
                and not isinstance(item, (str, bytes))
                and all(isinstance(entry, str) for entry in item)
            ),
            SettingType.OBJECT: lambda item: (
                isinstance(item, Mapping)
                and all(isinstance(key, str) for key in item)
                and _is_json_value(item)
            ),
            SettingType.JSON: _is_json_value,
        }[self.value_type](value)
        if not valid:
            raise ValueError(f"setting {self.key} {label} must have type {self.value_type.value}")

    def as_dict(self) -> dict[str, object]:
        value = {
            "key": self.key,
            "type": self.value_type.value,
            "scope": self.scope.value,
            "validation": self.validation.as_dict(),
            "consumers": list(self.consumers),
            "sensitive": self.sensitivity,
            "restart": self.restart.value,
        }
        if self.default is NO_DEFAULT:
            value["required"] = True
        else:
            value["default"] = _thaw(self.default)
        if self.form is not None:
            value["form"] = _thaw(self.form)
        return value


class ConfigurationRegistry:
    def __init__(self, descriptors: Sequence[SettingDescriptor] = ()) -> None:
        self._descriptors: dict[str, SettingDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: SettingDescriptor) -> None:
        if not isinstance(descriptor, SettingDescriptor):
            raise ValueError("configuration registry accepts only setting descriptors")
        if descriptor.key in self._descriptors:
            raise ValueError(f"duplicate setting descriptor: {descriptor.key}")
        self._descriptors[descriptor.key] = descriptor

    def register_plugin(self, descriptor: SettingDescriptor) -> None:
        """Register a plugin setting or overlay UI metadata on a compatible fallback."""

        existing = self._descriptors.get(descriptor.key)
        if existing is None:
            self.register(descriptor)
            return
        compatible = (
            existing.value_type == descriptor.value_type
            and existing.scope == descriptor.scope
            and existing.consumers == descriptor.consumers
            and existing.sensitivity == descriptor.sensitivity
            and existing.restart == descriptor.restart
            and existing.default == descriptor.default
        )
        if not compatible:
            raise ValueError(
                f"plugin setting conflicts with built-in descriptor: {descriptor.key}"
            )
        if descriptor.form is not None:
            self._descriptors[descriptor.key] = replace(
                existing,
                validation=descriptor.validation,
                form=_thaw(descriptor.form),
            )

    def descriptors(self) -> tuple[SettingDescriptor, ...]:
        return tuple(self._descriptors[key] for key in sorted(self._descriptors))

    def get(self, key: str) -> SettingDescriptor | None:
        exact = self._descriptors.get(key)
        if exact is not None:
            return exact
        segments = key.split(".")
        matches = [
            descriptor
            for descriptor in self._descriptors.values()
            if len(pattern := descriptor.key.split(".")) == len(segments)
            and all(
                expected == "*" or expected == actual
                for expected, actual in zip(pattern, segments, strict=True)
            )
        ]
        if len(matches) > 1:
            raise ValueError(f"ambiguous setting descriptor: {key}")
        return matches[0] if matches else None

    def suggestions(self, key: str, *, limit: int = 3) -> tuple[str, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("setting suggestion limit must be positive")
        return tuple(
            difflib.get_close_matches(
                key,
                tuple(sorted(self._descriptors)),
                n=limit,
                cutoff=0.55,
            )
        )

    def unknown_key_message(self, key: str) -> str:
        suggestions = self.suggestions(key)
        message = f"unknown setting: {key}"
        if suggestions:
            message += f". Did you mean: {', '.join(suggestions)}?"
        category = key.split(".", 1)[0]
        known_categories = {
            descriptor.key.split(".", 1)[0] for descriptor in self._descriptors.values()
        }
        if category in known_categories:
            message += f" Use /config list {category} to inspect that category."
        else:
            message += " Use /config list to inspect available settings."
        return message


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_is_json_value(item) for item in value)
    return False


def _thaw(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)
