from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from tfr.config import ConfigurationBundle
from tfr.configuration_registry import NO_DEFAULT, ConfigurationRegistry, SettingDescriptor
from tfr.managed_settings import ManagedSettingsDocument


class SettingProvenance(StrEnum):
    DEFAULT = "default"
    JSONC = "jsonc"
    MANAGED = "managed"
    RUNTIME = "runtime"


_PRECEDENCE = {
    SettingProvenance.DEFAULT: 0,
    SettingProvenance.JSONC: 1,
    SettingProvenance.MANAGED: 2,
    SettingProvenance.RUNTIME: 3,
}
_REDACTED = object()


@dataclass(frozen=True, slots=True)
class EffectiveSettingValue:
    key: str
    descriptor: SettingDescriptor
    value: object
    provenance: SettingProvenance
    source_detail: str

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "key": self.key,
            "source": self.provenance.value,
            "source_detail": self.source_detail,
        }
        if self.descriptor.sensitivity:
            value["value"] = None
            value["redacted"] = True
        else:
            value["value"] = copy.deepcopy(self.value)
        return value


class ConfigurationValues:
    """Read-only layered values; it never writes configuration persistence."""

    def __init__(self, registry: ConfigurationRegistry) -> None:
        self.registry = registry
        self._layers: dict[str, dict[SettingProvenance, tuple[object, str]]] = {}

    def record(
        self,
        key: str,
        value: object,
        provenance: SettingProvenance,
        *,
        source_detail: str,
    ) -> None:
        descriptor = self.registry.get(key)
        if descriptor is None:
            raise ValueError(f"unknown setting: {key}")
        if provenance is SettingProvenance.DEFAULT:
            raise ValueError("default values come from setting descriptors")
        if not isinstance(source_detail, str) or not source_detail or len(source_detail) > 500:
            raise ValueError("setting source detail is invalid")
        stored: object
        if descriptor.sensitivity:
            stored = _REDACTED
        else:
            descriptor.validate(value)
            stored = copy.deepcopy(value)
        self._layers.setdefault(key, {})[provenance] = (stored, source_detail)

    def resolve(self, key: str) -> EffectiveSettingValue | None:
        descriptor = self.registry.get(key)
        if descriptor is None:
            raise ValueError(self.registry.unknown_key_message(key))
        layers = self._layers.get(key, {})
        if layers:
            provenance = max(layers, key=_PRECEDENCE.__getitem__)
            value, detail = layers[provenance]
            return EffectiveSettingValue(
                key=key,
                descriptor=descriptor,
                value=None if value is _REDACTED else copy.deepcopy(value),
                provenance=provenance,
                source_detail=detail,
            )
        if descriptor.default is NO_DEFAULT:
            return None
        return EffectiveSettingValue(
            key=key,
            descriptor=descriptor,
            value=copy.deepcopy(descriptor.as_dict()["default"]),
            provenance=SettingProvenance.DEFAULT,
            source_detail="built-in default",
        )

    def record_runtime(self, key: str, value: object, *, detail: str) -> None:
        self.record(key, value, SettingProvenance.RUNTIME, source_detail=detail)

    def clear(self, key: str, provenance: SettingProvenance) -> None:
        if provenance is SettingProvenance.DEFAULT:
            raise ValueError("default values come from setting descriptors")
        layers = self._layers.get(key)
        if layers is None:
            return
        layers.pop(provenance, None)
        if not layers:
            self._layers.pop(key, None)

    def inventory_keys(self) -> tuple[str, ...]:
        static = {
            descriptor.key
            for descriptor in self.registry.descriptors()
            if "*" not in descriptor.key
        }
        return tuple(sorted(static | set(self._layers)))

    def summary(self) -> dict[str, object]:
        keys = self.inventory_keys()
        source_counts: Counter[str] = Counter()
        scope_counts: Counter[str] = Counter()
        sensitive = 0
        unset_required = 0
        for key in keys:
            descriptor = self.registry.get(key)
            if descriptor is None:
                raise RuntimeError(f"inventory key has no descriptor: {key}")
            scope_counts[descriptor.scope.value] += 1
            sensitive += int(descriptor.sensitivity)
            effective = self.resolve(key)
            if effective is None:
                unset_required += 1
            else:
                source_counts[effective.provenance.value] += 1
        return {
            "setting_count": len(keys),
            "template_count": sum(
                "*" in descriptor.key for descriptor in self.registry.descriptors()
            ),
            "sensitive_count": sensitive,
            "unset_required_count": unset_required,
            "sources": dict(sorted(source_counts.items())),
            "scopes": dict(sorted(scope_counts.items())),
        }

    def list_descriptors(self, filter_value: str | None = None) -> list[dict[str, object]]:
        normalized = filter_value.casefold() if filter_value is not None else None
        output: list[dict[str, object]] = []
        for descriptor in self.registry.descriptors():
            category = descriptor.key.split(".", 1)[0]
            if normalized is not None and normalized not in {
                category.casefold(),
                descriptor.scope.value.casefold(),
            }:
                continue
            value = descriptor.as_dict()
            value["category"] = category
            if descriptor.sensitivity:
                value.pop("default", None)
                value["default_redacted"] = True
            output.append(value)
        return output

    def show(self, key: str) -> dict[str, object]:
        descriptor = self.registry.get(key)
        if descriptor is None:
            raise ValueError(self.registry.unknown_key_message(key))
        metadata = descriptor.as_dict()
        metadata["key"] = key
        metadata["descriptor_key"] = descriptor.key
        metadata["category"] = descriptor.key.split(".", 1)[0]
        if descriptor.sensitivity:
            metadata.pop("default", None)
            metadata["default_redacted"] = True
        effective = self.resolve(key)
        if effective is None:
            metadata.update(
                {
                    "value": None,
                    "source": "unset",
                    "source_detail": "required setting has no configured value",
                }
            )
        else:
            metadata.update(effective.as_dict())
        return metadata


def configuration_values_from_bundle(
    bundle: ConfigurationBundle,
    registry: ConfigurationRegistry,
    *,
    managed: ManagedSettingsDocument | None = None,
) -> ConfigurationValues:
    values = ConfigurationValues(registry)
    _record_model(values, bundle.main, "", bundle.main_path)
    _record_model(values, bundle.worlds.defaults, "worlds.defaults", bundle.worlds_path)
    if "schema_version" in bundle.worlds.model_fields_set:
        values.record(
            "worlds.schema_version",
            bundle.worlds.schema_version,
            SettingProvenance.JSONC,
            source_detail=str(bundle.worlds_path),
        )
    for name, world in bundle.worlds.worlds.items():
        _record_model(values, world, f"worlds.{name}", bundle.worlds_path)
    if "schema_version" in bundle.agents.model_fields_set:
        values.record(
            "agents.schema_version",
            bundle.agents.schema_version,
            SettingProvenance.JSONC,
            source_detail=str(bundle.agents_path),
        )
    for name, provider in bundle.agents.providers.items():
        _record_model(values, provider, f"providers.{name}", bundle.agents_path)
    for name, agent in bundle.agents.agents.items():
        _record_model(values, agent, f"agents.{name}", bundle.agents_path)
    if managed is not None:
        _record_managed(values, managed)
    return values


def _record_model(
    values: ConfigurationValues,
    model: BaseModel,
    prefix: str,
    source: Path,
) -> None:
    serialized = model.model_dump(mode="json")
    for field_name in model.model_fields_set:
        if field_name == "schema_url":
            continue
        item = getattr(model, field_name)
        key = f"{prefix}.{field_name}" if prefix else field_name
        if isinstance(item, BaseModel):
            _record_model(values, item, key, source)
            continue
        descriptor = values.registry.get(key)
        if descriptor is not None:
            values.record(
                key,
                serialized[field_name],
                SettingProvenance.JSONC,
                source_detail=str(source),
            )


def _record_managed(values: ConfigurationValues, document: ManagedSettingsDocument) -> None:
    detail = f"managed override revision {document.revision}"
    if document.ui.spellcheck is not None:
        values.record(
            "ui.spellcheck.enabled",
            document.ui.spellcheck.enabled,
            SettingProvenance.MANAGED,
            source_detail=detail,
        )
    if document.ui.speaking_streaks is not None:
        values.record(
            "ui.speaking_streaks.enabled",
            document.ui.speaking_streaks.enabled,
            SettingProvenance.MANAGED,
            source_detail=detail,
        )
    for world, override in document.worlds.items():
        values.record(
            f"worlds.{world}.gag.patterns",
            list(override.gag.patterns),
            SettingProvenance.MANAGED,
            source_detail=detail,
        )
