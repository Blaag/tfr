from __future__ import annotations

from dataclasses import replace

import pytest

import tfr.plugins as plugins
from tfr.configuration_registry import (
    NO_DEFAULT,
    RestartRequirement,
    SettingDescriptor,
    SettingScope,
    SettingType,
    SettingValidation,
)
from tfr.plugins import (
    DuplicatePluginRegistration,
    PluginRegistrar,
    PluginRegistrationError,
    PluginRegistry,
)


def setting(**changes: object) -> SettingDescriptor:
    values = {
        "key": "plugins.config.spotlight.beam_count",
        "value_type": SettingType.INTEGER,
        "default": 2,
        "scope": SettingScope.PLUGIN,
        "consumers": ("native-ui", "pwa"),
        "restart": RestartRequirement.LIVE,
        "validation": SettingValidation(minimum=1, maximum=8),
    }
    values.update(changes)
    return SettingDescriptor(**values)  # type: ignore[arg-type]


def test_plugin_registers_bounded_metadata_and_typed_schema() -> None:
    registry = PluginRegistry()
    registrar = PluginRegistrar("spotlight", registry)
    descriptor = setting()

    registration = registrar.register_configuration(
        summary="Animated spotlight screen-clear effect.",
        category="effects",
        version="1.2.3",
        settings=(descriptor,),
    )

    assert registry.configurations == {"spotlight": registration}
    assert registry.configuration_descriptors() == (descriptor,)
    assert registration.as_dict() == {
        "plugin": "spotlight",
        "summary": "Animated spotlight screen-clear effect.",
        "category": "effects",
        "version": "1.2.3",
        "settings": [descriptor.as_dict()],
    }


def test_plugin_configuration_rejects_wrong_namespace_scope_and_duplicates() -> None:
    registry = PluginRegistry()
    registrar = PluginRegistrar("spotlight", registry)
    descriptor = setting()

    with pytest.raises(PluginRegistrationError, match="must begin"):
        registrar.register_configuration(
            summary="Summary",
            version="1",
            settings=(replace(descriptor, key="plugins.config.other.beam_count"),),
        )
    with pytest.raises(PluginRegistrationError, match="scope must be plugin"):
        registrar.register_configuration(
            summary="Summary",
            version="1",
            settings=(replace(descriptor, scope=SettingScope.SHARED),),
        )
    with pytest.raises(DuplicatePluginRegistration, match="setting descriptor"):
        registrar.register_configuration(
            summary="Summary",
            version="1",
            settings=(descriptor, descriptor),
        )


def test_plugin_configuration_never_publishes_secret_defaults() -> None:
    registrar = PluginRegistrar("spotlight", PluginRegistry())
    secret = setting(
        key="plugins.config.spotlight.api_key",
        value_type=SettingType.STRING,
        default="plaintext-secret",
        sensitivity=True,
        validation=SettingValidation(),
    )

    with pytest.raises(PluginRegistrationError, match="plaintext default"):
        registrar.register_configuration(
            summary="Summary",
            version="1",
            settings=(secret,),
        )

    safe = replace(secret, default=NO_DEFAULT)
    registration = registrar.register_configuration(
        summary="Summary",
        version="1",
        settings=(safe,),
    )
    assert registration.settings[0].default is NO_DEFAULT


def test_plugin_configuration_enforces_metadata_and_schema_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PluginRegistrationError, match="summary is invalid"):
        PluginRegistrar("spotlight", PluginRegistry()).register_configuration(
            summary="x" * 241,
            version="1",
            settings=(setting(),),
        )

    monkeypatch.setattr(plugins, "MAX_PLUGIN_CONFIGURATION_SCHEMA_BYTES", 10)
    with pytest.raises(PluginRegistrationError, match="exceeds 65536 bytes"):
        PluginRegistrar("spotlight", PluginRegistry()).register_configuration(
            summary="Summary",
            version="1",
            settings=(setting(),),
        )
