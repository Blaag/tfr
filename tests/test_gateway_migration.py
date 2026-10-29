from __future__ import annotations

import stat
from pathlib import Path
from uuid import uuid4

import pytest

from tfr.config import AgentsConfig, ConfigurationBundle, MainConfig, WorldsConfig
from tfr.core_settings import core_configuration_registry
from tfr.gateway import _stage_legacy_world_login_references
from tfr.gateway_client import GatewayClient, _legacy_migration_values
from tfr.gateway_configuration import GatewayConfigurationStore, apply_gateway_configuration
from tfr.gateway_migration import (
    GatewayMigrationStore,
    collect_migration_settings,
    collect_omitted_migration_keys,
    new_proposal,
)


def submit(
    migration: GatewayMigrationStore,
    *,
    ui_id,
    ui_name: str,
    values: dict[str, object],
    source: Path,
) -> None:
    settings = collect_migration_settings(
        values,
        core_configuration_registry(),
        ui_id=ui_id,
        source=str(source),
    )
    migration.submit(
        new_proposal(
            ui_id=ui_id,
            ui_name=ui_name,
            base_revision=migration.configuration.revision,
            settings=settings,
            source_files=(str(source),),
            omitted_keys=collect_omitted_migration_keys(
                values, core_configuration_registry()
            ),
        )
    )


def test_matching_shared_values_deduplicate_and_ui_values_remain_separate(
    tmp_path: Path,
) -> None:
    configuration = GatewayConfigurationStore(tmp_path / "state", core_configuration_registry())
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    first, second = uuid4(), uuid4()
    one = tmp_path / "one.jsonc"
    two = tmp_path / "two.jsonc"
    one.write_text("{}", encoding="utf-8")
    two.write_text("{}", encoding="utf-8")
    submit(
        migration,
        ui_id=first,
        ui_name="One",
        values={"ui.spellcheck.enabled": True, "ui.mouse_mode": "tfr"},
        source=one,
    )
    submit(
        migration,
        ui_id=second,
        ui_name="Two",
        values={"ui.spellcheck.enabled": True, "ui.mouse_mode": "terminal"},
        source=two,
    )

    preview = migration.preview()

    assert preview["conflicts"] == []
    accepted = preview["accepted"]
    assert sum(item["key"] == "ui.spellcheck.enabled" for item in accepted) == 1
    assert sum(item["key"] == "ui.mouse_mode" for item in accepted) == 2


def test_speaker_rules_are_migrated_and_unknown_plugin_leaves_are_reported(
    tmp_path: Path,
) -> None:
    configuration = GatewayConfigurationStore(tmp_path / "state", core_configuration_registry())
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    values = {
        "plugins.config": {
            "speaker_effects": {"rules": [{"speaker": "Barnaby", "effect": "comet"}]},
            "unknown_plugin": {"nested": {"value": 3}},
        }
    }
    submit(
        migration,
        ui_id=uuid4(),
        ui_name="Terminal",
        values=values,
        source=tmp_path / "config.jsonc",
    )
    submit(
        migration,
        ui_id=uuid4(),
        ui_name="Empty Terminal",
        values={"plugins.config": {"speaker_effects": {"rules": []}}},
        source=tmp_path / "empty.jsonc",
    )

    preview = migration.preview()

    assert preview["accepted"][0]["key"] == "plugins.config.speaker_effects.rules"
    assert preview["accepted"][0]["value"][0]["speaker"] == "Barnaby"
    assert preview["conflicts"] == []
    assert preview["restart"] == "gateway-restart"
    assert {
        "key": "plugins.config.unknown_plugin.nested.value",
        "reason": "unrecognized setting; retained in the source file but not imported",
        "source": "Terminal",
    } in preview["skipped"]


def test_bootstrap_metadata_is_silent_and_existing_secret_reference_is_preserved(
    tmp_path: Path,
) -> None:
    registry = core_configuration_registry()
    configuration = GatewayConfigurationStore(tmp_path / "state", registry)
    configuration.set(
        "worlds.alpha.login.password",
        "file:/private/alpha.password",
        scope="secret",
        target=None,
        expected_revision=0,
        actor_ui_id="migration",
        actor_ui_name="Migration",
    )
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    submit(
        migration,
        ui_id=uuid4(),
        ui_name="Gateway legacy configuration",
        values={
            "schema_version": 1,
            "worlds_file": "worlds.jsonc",
            "worlds.alpha.login.password": None,
        },
        source=tmp_path / "config.jsonc",
    )

    preview = migration.preview()

    assert preview["conflicts"] == []
    assert preview["accepted"] == []
    assert preview["skipped"] == [
        {
            "key": "worlds.alpha.login.password",
            "reason": "already managed; the existing Gateway value is preserved",
        }
    ]


def test_incremental_migration_preserves_existing_managed_values(tmp_path: Path) -> None:
    registry = core_configuration_registry()
    configuration = GatewayConfigurationStore(tmp_path / "state", registry)
    configuration.set(
        "ui.spellcheck.enabled",
        False,
        scope="shared",
        target=None,
        expected_revision=0,
        actor_ui_id="owner",
        actor_ui_name="Owner",
    )
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    submit(
        migration,
        ui_id=uuid4(),
        ui_name="Terminal",
        values={
            "ui.spellcheck.enabled": True,
            "plugins.config": {
                "speaker_effects": {
                    "rules": [{"speaker": "Barnaby", "effect": "comet"}]
                }
            },
        },
        source=tmp_path / "config.jsonc",
    )

    preview = migration.preview()

    assert [item["key"] for item in preview["accepted"]] == [
        "plugins.config.speaker_effects.rules"
    ]
    assert preview["restart"] == "gateway-restart"
    assert {
        "key": "ui.spellcheck.enabled",
        "reason": "already managed; the existing Gateway value is preserved",
    } in preview["skipped"]


def test_gateway_import_keeps_per_ui_settings_out_of_ui_type_scope(tmp_path: Path) -> None:
    configuration = GatewayConfigurationStore(tmp_path / "state", core_configuration_registry())
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    ui_id = uuid4()
    settings = collect_migration_settings(
        {
            "ui.animations_enabled": False,
            "ui.pager.enabled": False,
        },
        core_configuration_registry(),
        ui_id=ui_id,
        source="Gateway JSONC one-time import",
        ui_scope="ui-type",
        ui_target="terminal",
    )
    migration.submit(
        new_proposal(
            ui_id=ui_id,
            ui_name="Gateway legacy configuration",
            base_revision=0,
            settings=settings,
        )
    )

    assert migration.apply(
        expected_revision=0,
        choices=None,
        actor_ui_id="migration",
        actor_ui_name="Legacy one-time import",
    ) == 1
    assert configuration.get("ui.animations_enabled", scope="ui", target=str(ui_id)) == (
        True,
        False,
    )
    assert configuration.get("ui.pager.enabled", scope="ui-type", target="terminal") == (
        True,
        False,
    )


def test_gateway_import_moves_world_passwords_to_private_references(tmp_path: Path) -> None:
    legacy = ConfigurationBundle(
        main_path=tmp_path / "config.jsonc",
        worlds_path=tmp_path / "worlds.jsonc",
        agents_path=tmp_path / "agents.jsonc",
        main=MainConfig(),
        worlds=WorldsConfig.model_validate(
            {
                "worlds": {
                    "alpha": {
                        "host": "example.test",
                        "port": 4201,
                        "login": {"character": "Alice", "password": "secret-value"},
                    }
                }
            }
        ),
        agents=AgentsConfig(),
    )
    configuration = GatewayConfigurationStore(tmp_path / "state", core_configuration_registry())
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    migration.submit(
        new_proposal(
            ui_id=uuid4(),
            ui_name="Gateway legacy configuration",
            base_revision=0,
            settings=collect_migration_settings(
                {
                    "worlds.alpha.host": "example.test",
                    "worlds.alpha.port": 4201,
                    "worlds.alpha.login.character": "Alice",
                },
                core_configuration_registry(),
                ui_id=uuid4(),
                source="Gateway JSONC one-time import",
            ),
        )
    )
    references = _stage_legacy_world_login_references(legacy, tmp_path / "state")

    migration.apply(
        expected_revision=0,
        choices=None,
        actor_ui_id="migration",
        actor_ui_name="Legacy one-time import",
        secret_references=references,
    )
    effective = apply_gateway_configuration(
        ConfigurationBundle(
            main_path=legacy.main_path,
            worlds_path=legacy.worlds_path,
            agents_path=legacy.agents_path,
            main=MainConfig(),
            worlds=WorldsConfig(),
            agents=AgentsConfig(),
        ),
        configuration.document,
    )

    reference = references["worlds.alpha.login.password"]
    secret_path = Path(reference.removeprefix("file:"))
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert effective.worlds.worlds["alpha"].login is not None
    assert effective.worlds.worlds["alpha"].login.password.get_secret_value() == "secret-value"


def test_divergent_non_default_values_require_choice_and_apply_atomically(
    tmp_path: Path,
) -> None:
    configuration = GatewayConfigurationStore(tmp_path / "state", core_configuration_registry())
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    first, second = uuid4(), uuid4()
    one = tmp_path / "one.jsonc"
    two = tmp_path / "two.jsonc"
    one.write_text('{"directory":"/one"}', encoding="utf-8")
    two.write_text('{"directory":"/two"}', encoding="utf-8")
    submit(
        migration,
        ui_id=first,
        ui_name="One",
        values={"logging.directory": "/one"},
        source=one,
    )
    submit(
        migration,
        ui_id=second,
        ui_name="Two",
        values={"logging.directory": "/two"},
        source=two,
    )

    assert migration.preview()["conflicts"][0]["key"] == "logging.directory"
    with pytest.raises(ValueError, match="unresolved"):
        migration.apply(
            expected_revision=0,
            choices=None,
            actor_ui_id="admin",
            actor_ui_name="Admin",
        )

    revision = migration.apply(
        expected_revision=0,
        choices={"logging.directory": "Two"},
        actor_ui_id="admin",
        actor_ui_name="Admin",
    )

    assert revision == 1
    assert configuration.get("logging.directory", scope="gateway") == (True, "/two")
    assert len(migration.backups) == 2
    assert one.read_text(encoding="utf-8") == '{"directory":"/one"}'


def test_conflicting_same_name_world_can_be_renamed(tmp_path: Path) -> None:
    configuration = GatewayConfigurationStore(tmp_path / "state", core_configuration_registry())
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    first, second = uuid4(), uuid4()
    for ui_id, name, host in ((first, "One", "one.example"), (second, "Two", "two.example")):
        migration.submit(
            new_proposal(
                ui_id=ui_id,
                ui_name=name,
                base_revision=0,
                settings=collect_migration_settings(
                    {"worlds.alpha.host": host},
                    core_configuration_registry(),
                    ui_id=ui_id,
                    source=f"{name}.jsonc",
                ),
            )
        )

    preview = migration.preview({"worlds.alpha.host": "rename:Two:alpha-from-two"})

    assert preview["conflicts"] == []
    assert preview["accepted"][0]["target"] == "alpha-from-two"
    assert preview["accepted"][0]["key"] == "worlds.alpha-from-two.host"
    assert preview["accepted"][0]["value"] == "two.example"


def test_older_proposal_rebases_without_overriding_managed_values(tmp_path: Path) -> None:
    configuration = GatewayConfigurationStore(tmp_path / "state", core_configuration_registry())
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    source = tmp_path / "config.jsonc"
    source.write_text("{}", encoding="utf-8")
    submit(
        migration,
        ui_id=uuid4(),
        ui_name="One",
        values={
            "ui.spellcheck.enabled": False,
            "logging.directory": "/legacy/logs",
        },
        source=source,
    )
    configuration.set(
        "ui.spellcheck.enabled",
        True,
        scope="shared",
        target=None,
        expected_revision=0,
        actor_ui_id="owner",
        actor_ui_name="Owner",
    )

    assert migration.apply(
        expected_revision=1,
        choices=None,
        actor_ui_id="admin",
        actor_ui_name="Admin",
    ) == 2

    assert configuration.get("ui.spellcheck.enabled", scope="shared") == (True, True)
    assert configuration.get("logging.directory", scope="gateway") == (
        True,
        "/legacy/logs",
    )


def test_interrupted_apply_keeps_active_configuration_and_clears_pending_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = GatewayConfigurationStore(tmp_path / "state", core_configuration_registry())
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    source = tmp_path / "config.jsonc"
    source.write_text("{}", encoding="utf-8")
    submit(
        migration,
        ui_id=uuid4(),
        ui_name="One",
        values={"ui.spellcheck.enabled": True},
        source=source,
    )

    def interrupt(*_args, **_kwargs):
        raise OSError("interrupted")

    monkeypatch.setattr(configuration, "replace_for_migration", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        migration.apply(
            expected_revision=0,
            choices=None,
            actor_ui_id="admin",
            actor_ui_name="Admin",
        )

    assert configuration.revision == 0
    assert migration.status()["applied_revision"] is None


def test_secret_proposals_never_include_plaintext(tmp_path: Path) -> None:
    registry = core_configuration_registry()
    secret_key = "providers.demo.api_key"
    settings = collect_migration_settings(
        {secret_key: "super-secret"}, registry, ui_id=uuid4(), source="agents.jsonc"
    )

    assert settings[0].sensitive is True
    assert settings[0].value is None
    assert "super-secret" not in str(settings[0].as_dict())


def test_client_backup_preserves_legacy_file_and_marker_allows_incremental_recovery(
    tmp_path: Path,
) -> None:
    from tfr.config import MainConfig, UiConfiguration

    source = tmp_path / "config.jsonc"
    source.write_text('{"ui":{"mouse_mode":"terminal"}}', encoding="utf-8")
    configuration = UiConfiguration(
        main_path=source,
        main=MainConfig.model_validate({"ui": {"mouse_mode": "terminal"}}),
    )
    assert _legacy_migration_values(configuration)["ui.mouse_mode"] == "terminal"

    GatewayClient._backup_migration_sources((str(source),))
    source.with_name(f"{source.name}.tfr-migrated").write_text("revision 1\n", encoding="utf-8")

    assert source.read_text(encoding="utf-8") == '{"ui":{"mouse_mode":"terminal"}}'
    assert source.with_name(f"{source.name}.pre-tfr-migration").is_file()
    assert _legacy_migration_values(configuration)["ui.mouse_mode"] == "terminal"
