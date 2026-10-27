from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from tfr.core_settings import core_configuration_registry
from tfr.gateway_client import GatewayClient, _legacy_migration_values
from tfr.gateway_configuration import GatewayConfigurationStore
from tfr.gateway_migration import (
    GatewayMigrationStore,
    collect_migration_settings,
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


def test_divergent_shared_values_require_choice_and_apply_atomically(tmp_path: Path) -> None:
    configuration = GatewayConfigurationStore(tmp_path / "state", core_configuration_registry())
    migration = GatewayMigrationStore(tmp_path / "state", configuration)
    first, second = uuid4(), uuid4()
    one = tmp_path / "one.jsonc"
    two = tmp_path / "two.jsonc"
    one.write_text('{"value":true}', encoding="utf-8")
    two.write_text('{"value":false}', encoding="utf-8")
    submit(
        migration,
        ui_id=first,
        ui_name="One",
        values={"ui.spellcheck.enabled": True},
        source=one,
    )
    submit(
        migration,
        ui_id=second,
        ui_name="Two",
        values={"ui.spellcheck.enabled": False},
        source=two,
    )

    assert migration.preview()["conflicts"][0]["key"] == "ui.spellcheck.enabled"
    with pytest.raises(ValueError, match="unresolved"):
        migration.apply(
            expected_revision=0,
            choices=None,
            actor_ui_id="admin",
            actor_ui_name="Admin",
        )

    revision = migration.apply(
        expected_revision=0,
        choices={"ui.spellcheck.enabled": "Two"},
        actor_ui_id="admin",
        actor_ui_name="Admin",
    )

    assert revision == 1
    assert configuration.get("ui.spellcheck.enabled", scope="shared") == (True, False)
    assert len(migration.backups) == 2
    assert one.read_text(encoding="utf-8") == '{"value":true}'


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


def test_late_stale_proposal_cannot_override_and_rollback_is_atomic(tmp_path: Path) -> None:
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
    assert (
        migration.apply(
            expected_revision=0,
            choices=None,
            actor_ui_id="admin",
            actor_ui_name="Admin",
        )
        == 1
    )

    late = uuid4()
    settings = collect_migration_settings(
        {"ui.spellcheck.enabled": False},
        core_configuration_registry(),
        ui_id=late,
        source=str(source),
    )
    migration.submit(
        new_proposal(
            ui_id=late,
            ui_name="Late",
            base_revision=0,
            settings=settings,
        )
    )
    with pytest.raises(ValueError, match="stale"):
        migration.apply(
            expected_revision=1,
            choices={"ui.spellcheck.enabled": "Late"},
            actor_ui_id="admin",
            actor_ui_name="Admin",
        )

    assert (
        migration.rollback(
            expected_revision=1,
            actor_ui_id="admin",
            actor_ui_name="Admin",
        )
        == 2
    )
    assert configuration.get("ui.spellcheck.enabled", scope="shared") == (False, None)


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


def test_client_backup_preserves_legacy_file_and_marker_disables_proposal(
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
    assert _legacy_migration_values(configuration) == {}
