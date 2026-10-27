from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from tfr.config import AgentsConfig, ConfigurationBundle, MainConfig, WorldsConfig
from tfr.core_settings import core_configuration_registry
from tfr.gateway_configuration import (
    GatewayConfigurationDocument,
    GatewayConfigurationError,
    GatewayConfigurationRevisionConflict,
    GatewayConfigurationStore,
    apply_gateway_configuration,
)


def store(tmp_path: Path) -> GatewayConfigurationStore:
    return GatewayConfigurationStore(tmp_path / "settings", core_configuration_registry())


def test_secret_references_resolve_only_into_runtime_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TFR_TEST_WORLD_PASSWORD", "secret-value")
    bundle = ConfigurationBundle(
        main_path=tmp_path / "gateway.jsonc",
        worlds_path=tmp_path / "worlds.jsonc",
        agents_path=tmp_path / "agents.jsonc",
        main=MainConfig(),
        worlds=WorldsConfig.model_validate(
            {"worlds": {"alpha": {"host": "example.test", "port": 4201}}}
        ),
        agents=AgentsConfig(),
    )
    document = GatewayConfigurationDocument(
        worlds={
            "alpha": {"worlds.alpha.login.character": "Alice"},
        },
        secret_references={
            "worlds.alpha.login.password": "env:TFR_TEST_WORLD_PASSWORD",
        },
    )

    effective = apply_gateway_configuration(bundle, document)

    assert effective.worlds.worlds["alpha"].login is not None
    assert effective.worlds.worlds["alpha"].login.password.get_secret_value() == "secret-value"
    assert document.secret_references["worlds.alpha.login.password"] == (
        "env:TFR_TEST_WORLD_PASSWORD"
    )


def test_world_defaults_apply_to_defaults_instead_of_creating_a_world(tmp_path: Path) -> None:
    bundle = ConfigurationBundle(
        main_path=tmp_path / "gateway.jsonc",
        worlds_path=tmp_path / "worlds.jsonc",
        agents_path=tmp_path / "agents.jsonc",
        main=MainConfig(),
        worlds=WorldsConfig.model_validate(
            {"worlds": {"alpha": {"host": "example.test", "port": 4201}}}
        ),
        agents=AgentsConfig(),
    )
    document = GatewayConfigurationDocument(
        shared={
            "worlds.defaults.encoding": "latin-1",
            "worlds.defaults.scrollback_lines": 1234,
        }
    )

    effective = apply_gateway_configuration(bundle, document)

    assert effective.worlds.defaults.encoding == "latin-1"
    assert effective.worlds.defaults.scrollback_lines == 1234
    assert set(effective.worlds.worlds) == {"alpha"}


def test_store_persists_scoped_versioned_document_with_backup_and_audit(tmp_path: Path) -> None:
    configuration = store(tmp_path)
    first = configuration.set(
        "ui.spellcheck.enabled",
        True,
        scope="shared",
        target=None,
        expected_revision=0,
        actor_ui_id="11111111-1111-4111-8111-111111111111",
        actor_ui_name="Writing desk",
    )
    second = configuration.set(
        "ui.mouse_mode",
        "terminal",
        scope="ui",
        target="11111111-1111-4111-8111-111111111111",
        expected_revision=1,
        actor_ui_id="11111111-1111-4111-8111-111111111111",
        actor_ui_name="Writing desk",
    )

    reloaded = store(tmp_path)
    assert first.revision == 1
    assert second.revision == 2
    assert reloaded.revision == 2
    assert reloaded.get("ui.spellcheck.enabled", scope="shared") == (True, True)
    assert reloaded.get(
        "ui.mouse_mode",
        scope="ui",
        target="11111111-1111-4111-8111-111111111111",
    ) == (True, "terminal")
    previous = json.loads(reloaded.previous_path.read_text())
    assert previous["revision"] == 1
    changes = reloaded.changes()
    assert [change["actor_ui_name"] for change in changes] == [
        "Writing desk",
        "Writing desk",
    ]
    if os.name == "posix":
        assert stat.S_IMODE(reloaded.path.stat().st_mode) == 0o600


def test_revision_conflict_prevents_lost_update(tmp_path: Path) -> None:
    configuration = store(tmp_path)
    configuration.set(
        "ui.spellcheck.enabled",
        True,
        scope="shared",
        target=None,
        expected_revision=0,
        actor_ui_id="one",
        actor_ui_name="One",
    )

    with pytest.raises(GatewayConfigurationRevisionConflict, match="revision is 1, not 0"):
        configuration.set(
            "ui.speaking_streaks.enabled",
            False,
            scope="shared",
            target=None,
            expected_revision=0,
            actor_ui_id="two",
            actor_ui_name="Two",
        )
    assert configuration.revision == 1


def test_invalid_value_and_document_never_replace_active_revision(tmp_path: Path) -> None:
    configuration = store(tmp_path)
    with pytest.raises(ValueError, match="allowed choice"):
        configuration.set(
            "ui.mouse_mode",
            "invalid",
            scope="ui",
            target="one",
            expected_revision=0,
            actor_ui_id="one",
            actor_ui_name="One",
        )
    assert configuration.revision == 0
    assert not configuration.path.exists()

    configuration.path.write_text('{"schema_version":1,"revision":9}', encoding="utf-8")
    configuration.path.chmod(0o600)
    with pytest.raises(GatewayConfigurationError, match="invalid fields"):
        store(tmp_path)


def test_reset_and_undo_restore_previous_effective_override(tmp_path: Path) -> None:
    configuration = store(tmp_path)
    configuration.set(
        "ui.spellcheck.enabled",
        True,
        scope="shared",
        target=None,
        expected_revision=0,
        actor_ui_id="one",
        actor_ui_name="One",
    )
    reset = configuration.reset(
        "ui.spellcheck.enabled",
        scope="shared",
        target=None,
        expected_revision=1,
        actor_ui_id="one",
        actor_ui_name="One",
    )
    assert reset.revision == 2
    assert configuration.get("ui.spellcheck.enabled", scope="shared") == (False, None)

    undone = configuration.undo(
        expected_revision=2,
        actor_ui_id="two",
        actor_ui_name="Two",
    )
    assert undone.revision == 3
    assert configuration.get("ui.spellcheck.enabled", scope="shared") == (True, True)
    assert store(tmp_path).get("ui.spellcheck.enabled", scope="shared") == (True, True)


def test_secret_references_are_redacted_and_omitted_from_export(tmp_path: Path) -> None:
    configuration = store(tmp_path)
    with pytest.raises(GatewayConfigurationError, match="must use a keychain"):
        configuration.set(
            "worlds.alpha.login.password",
            "plaintext",
            scope="secret",
            target=None,
            expected_revision=0,
            actor_ui_id="one",
            actor_ui_name="One",
        )
    configuration.set(
        "worlds.alpha.login.password",
        "keychain:tfr/worlds/alpha",
        scope="secret",
        target=None,
        expected_revision=0,
        actor_ui_id="one",
        actor_ui_name="One",
    )

    assert configuration.export()["secret_references"] == {}
    assert configuration.export(include_secret_references=True)["secret_references"] == {
        "worlds.alpha.login.password": "keychain:tfr/worlds/alpha"
    }
    change = configuration.changes()[0]
    assert change["redacted"] is True
    assert change["value"] is None


def test_schema_zero_migrates_to_current_document(tmp_path: Path) -> None:
    state = tmp_path / "settings"
    state.mkdir(mode=0o700)
    path = state / "gateway-configuration.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 0,
                "revision": 3,
                "settings": {"ui.spellcheck.enabled": True},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    migrated = GatewayConfigurationStore(state, core_configuration_registry())

    assert migrated.revision == 3
    assert migrated.document.schema_version == 1
    assert migrated.get("ui.spellcheck.enabled", scope="shared") == (True, True)
    assert json.loads(path.read_text())["schema_version"] == 1
    assert json.loads(migrated.previous_path.read_text())["schema_version"] == 0


def test_preview_reports_required_action_without_writing(tmp_path: Path) -> None:
    configuration = store(tmp_path)

    preview = configuration.preview(
        "web_gateway.listen_port",
        7443,
        scope="gateway",
        target=None,
    )

    assert preview["restart"] == "gateway-restart"
    assert preview["requires_confirmation"] is True
    assert configuration.revision == 0
    assert not configuration.path.exists()


def test_target_must_match_dynamic_setting_key_without_mutating_document(
    tmp_path: Path,
) -> None:
    configuration = store(tmp_path)

    with pytest.raises(GatewayConfigurationError, match="world target does not match"):
        configuration.set(
            "worlds.alpha.gag.patterns",
            ["quiet"],
            scope="world",
            target="beta",
            expected_revision=0,
            actor_ui_id="one",
            actor_ui_name="One",
        )

    assert configuration.revision == 0
    assert configuration.document.worlds == {}
    assert not configuration.path.exists()
