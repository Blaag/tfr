from __future__ import annotations

import copy
from dataclasses import dataclass
from uuid import UUID

from tfr.configuration_registry import RestartRequirement, SettingScope
from tfr.configuration_values import ConfigurationValues
from tfr.gateway_configuration import GatewayConfigurationStore

CONFIGURATION_SYNC_CAPABILITY = "configuration-sync-v1"
MAX_CONFIGURATION_ISSUES = 100


@dataclass(frozen=True, slots=True)
class EffectiveClientConfiguration:
    revision: int
    settings: dict[str, object]
    requirements: dict[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "settings": copy.deepcopy(self.settings),
            "requirements": dict(self.requirements),
        }


def effective_client_configuration(
    values: ConfigurationValues,
    store: GatewayConfigurationStore,
    *,
    client_type: str,
    ui_id: UUID,
) -> EffectiveClientConfiguration:
    """Return the non-secret Gateway-authoritative view for one named UI."""

    consumer = "native-ui" if client_type == "terminal" else "pwa"
    document = store.document
    keys = set(values.inventory_keys())
    keys.update(document.shared)
    keys.update(document.ui_types.get(client_type, ()))
    keys.update(document.uis.get(str(ui_id), ()))
    settings: dict[str, object] = {}
    requirements: dict[str, str] = {}
    for key in sorted(keys):
        descriptor = store.registry.get(key)
        if (
            descriptor is None
            or descriptor.sensitivity
            or consumer not in descriptor.consumers
            or descriptor.scope in {SettingScope.SECRET, SettingScope.BOOTSTRAP}
        ):
            continue
        effective = values.resolve(key)
        if effective is None:
            continue
        value = effective.value
        # Managed UI layers have deliberately higher precedence than shared and
        # legacy JSONC values. Individual overrides are last by design.
        if key in document.shared:
            value = document.shared[key]
        if key in document.ui_types.get(client_type, {}):
            value = document.ui_types[client_type][key]
        if key in document.uis.get(str(ui_id), {}):
            value = document.uis[str(ui_id)][key]
        descriptor.validate(value)
        settings[key] = copy.deepcopy(value)
        requirements[key] = descriptor.restart.value
    return EffectiveClientConfiguration(store.revision, settings, requirements)


def validate_configuration_ack(
    value: object, *, maximum_revision: int
) -> tuple[int, str, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != {"revision", "status", "issues"}:
        raise ValueError("configuration acknowledgement has invalid fields")
    revision = value["revision"]
    status = value["status"]
    issues = value["issues"]
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or revision > maximum_revision
    ):
        raise ValueError("configuration acknowledgement revision is invalid")
    if status not in {"applied", "reload-required", "restart-required", "unsupported"}:
        raise ValueError("configuration acknowledgement status is invalid")
    if (
        not isinstance(issues, list)
        or len(issues) > MAX_CONFIGURATION_ISSUES
        or any(not isinstance(issue, str) or not 1 <= len(issue) <= 200 for issue in issues)
    ):
        raise ValueError("configuration acknowledgement issues are invalid")
    if status == "unsupported" and not issues:
        raise ValueError("unsupported configuration acknowledgement requires issues")
    return revision, status, tuple(issues)


def required_action(requirements: dict[str, str]) -> str:
    order = {
        RestartRequirement.LIVE.value: 0,
        RestartRequirement.CLIENT_RELOAD.value: 1,
        RestartRequirement.CLIENT_RESTART.value: 2,
        RestartRequirement.GATEWAY_RESTART.value: 2,
    }
    highest = max((order.get(value, 2) for value in requirements.values()), default=0)
    return ("applied", "reload-required", "restart-required")[highest]
