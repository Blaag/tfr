from __future__ import annotations

from types import MappingProxyType

import pytest

from tfr.configuration_registry import (
    ConfigurationRegistry,
    RestartRequirement,
    SettingDescriptor,
    SettingScope,
    SettingType,
    SettingValidation,
)


def test_descriptor_exposes_typed_bounded_metadata() -> None:
    descriptor = SettingDescriptor(
        key="ui.history.lines",
        value_type=SettingType.INTEGER,
        default=500,
        scope=SettingScope.UI,
        validation=SettingValidation(minimum=10, maximum=10_000),
        consumers=("pwa", "native-ui"),
        restart=RestartRequirement.CLIENT_RELOAD,
    )

    descriptor.validate(2_000)
    assert descriptor.as_dict() == {
        "key": "ui.history.lines",
        "type": "integer",
        "default": 500,
        "scope": "ui",
        "validation": {"nullable": False, "minimum": 10, "maximum": 10_000},
        "consumers": ["native-ui", "pwa"],
        "sensitive": False,
        "restart": "client-reload",
    }
    with pytest.raises(ValueError, match="exceeds its maximum"):
        descriptor.validate(10_001)
    with pytest.raises(ValueError, match="must have type integer"):
        descriptor.validate(True)


def test_descriptor_validates_defaults_choices_and_patterns() -> None:
    descriptor = SettingDescriptor(
        key="gateway.updates.channel",
        value_type=SettingType.STRING,
        default="stable",
        scope=SettingScope.GATEWAY,
        validation=SettingValidation(
            choices=("stable", "candidate"),
            maximum_length=16,
            pattern=r"[a-z]+",
        ),
        consumers=("gateway",),
        restart=RestartRequirement.GATEWAY_RESTART,
    )

    descriptor.validate("candidate")
    assert descriptor.as_dict()["validation"] == {
        "nullable": False,
        "choices": ["stable", "candidate"],
        "maximum_length": 16,
        "pattern": r"[a-z]+",
    }
    with pytest.raises(ValueError, match="allowed choice"):
        descriptor.validate("nightly")
    with pytest.raises(ValueError, match="default is not an allowed choice"):
        SettingDescriptor(
            key="gateway.updates.invalid",
            value_type=SettingType.STRING,
            default="nightly",
            scope=SettingScope.GATEWAY,
            validation=descriptor.validation,
            consumers=("gateway",),
        )


def test_descriptor_freezes_structured_defaults_and_redacts_nothing_itself() -> None:
    source = {"reference": "keychain:tfr/gateway", "fallbacks": ["environment"]}
    descriptor = SettingDescriptor(
        key="secrets.gateway-token.reference",
        value_type=SettingType.OBJECT,
        default=source,
        scope=SettingScope.SECRET,
        consumers=("gateway",),
        sensitivity=True,
    )
    source["reference"] = "changed"

    assert isinstance(descriptor.default, MappingProxyType)
    assert descriptor.as_dict()["default"] == {
        "reference": "keychain:tfr/gateway",
        "fallbacks": ["environment"],
    }
    assert descriptor.as_dict()["sensitive"] is True
    with pytest.raises(ValueError, match="must have type object"):
        SettingDescriptor(
            key="gateway.invalid.object",
            value_type=SettingType.OBJECT,
            default={"unsupported": object()},
            scope=SettingScope.GATEWAY,
            consumers=("gateway",),
        )


@pytest.mark.parametrize(
    "values",
    [
        {"key": "Invalid", "consumers": ("gateway",)},
        {"key": "valid.key", "consumers": ()},
        {"key": "valid.key", "consumers": ("Gateway UI",)},
        {"key": "valid.key", "consumers": ("gateway", "gateway")},
    ],
)
def test_descriptor_rejects_invalid_identity_and_consumers(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SettingDescriptor(
            value_type=SettingType.BOOLEAN,
            default=False,
            scope=SettingScope.SHARED,
            **values,  # type: ignore[arg-type]
        )


def test_validation_metadata_rejects_incoherent_constraints() -> None:
    with pytest.raises(ValueError, match="numeric range"):
        SettingValidation(minimum=10, maximum=1)
    with pytest.raises(ValueError, match="length range"):
        SettingValidation(minimum_length=10, maximum_length=1)
    with pytest.raises(ValueError, match="pattern is invalid"):
        SettingValidation(pattern="[")


def test_registry_unknown_key_guidance_has_matches_and_context() -> None:
    descriptor = SettingDescriptor(
        key="ui.mouse_mode",
        value_type=SettingType.STRING,
        default="tfr",
        scope=SettingScope.UI,
        consumers=("native-ui",),
    )
    registry = ConfigurationRegistry((descriptor,))

    assert registry.suggestions("ui.mouse_mod") == ("ui.mouse_mode",)
    assert registry.unknown_key_message("ui.mouse_mod") == (
        "unknown setting: ui.mouse_mod. Did you mean: ui.mouse_mode? "
        "Use /config list ui to inspect that category."
    )
    assert registry.unknown_key_message("mystery.value").endswith(
        "Use /config list to inspect available settings."
    )
