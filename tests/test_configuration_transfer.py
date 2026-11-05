from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import UUID

import pytest

from tfr.config import AgentsConfig, ConfigurationBundle, MainConfig, WorldsConfig
from tfr.configuration_sync import effective_client_configuration
from tfr.configuration_transfer import GatewayConfigurationTransfer
from tfr.configuration_values import configuration_values_from_bundle
from tfr.core_settings import core_configuration_registry
from tfr.gateway_configuration import GatewayConfigurationError, GatewayConfigurationStore
from tfr.gateway_uis import UiStore


def bundle(tmp_path: Path) -> ConfigurationBundle:
    return ConfigurationBundle(
        main_path=tmp_path / "gateway.jsonc",
        worlds_path=tmp_path / "worlds.jsonc",
        agents_path=tmp_path / "agents.jsonc",
        main=MainConfig.model_validate(
            {"managed_settings": {"state_directory": str(tmp_path / "state")}}
        ),
        worlds=WorldsConfig(),
        agents=AgentsConfig(),
    )


async def register_ui(store: UiStore, ui_id: UUID, name: str, platform: str) -> None:
    await store.register(
        ui_id=ui_id,
        client_type="terminal",
        platform=platform,
        versions={},
        capabilities=("configuration-sync-v1",),
    )
    await store.rename(ui_id, name)


@pytest.mark.asyncio
async def test_wiped_gateway_restores_connected_and_pending_ui_profiles(
    tmp_path: Path,
) -> None:
    old_one = UUID("11111111-1111-4111-8111-111111111111")
    old_two = UUID("22222222-2222-4222-8222-222222222222")
    new_one = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    new_two = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    source_bundle = bundle(tmp_path / "source")
    source_store = GatewayConfigurationStore(
        source_bundle.main.managed_settings.state_directory,
        core_configuration_registry(),
    )
    source_uis = UiStore(source_bundle.main.managed_settings.state_directory)
    await register_ui(source_uis, old_one, "Office Laptop", "office")
    await register_ui(source_uis, old_two, "Living Room", "living-room")
    source_store.set(
        "ui.mouse_mode",
        "terminal",
        scope="ui",
        target=str(old_one),
        expected_revision=0,
        actor_ui_id="admin",
        actor_ui_name="Admin",
    )
    source_store.set(
        "ui.mouse_mode",
        "auto",
        scope="ui",
        target=str(old_two),
        expected_revision=1,
        actor_ui_id="admin",
        actor_ui_name="Admin",
    )
    archive = GatewayConfigurationTransfer(
        source_bundle.main.managed_settings.state_directory,
        source_store,
        source_uis,
        source_bundle,
    ).export()

    destination_bundle = bundle(tmp_path / "destination")
    destination_store = GatewayConfigurationStore(
        destination_bundle.main.managed_settings.state_directory,
        core_configuration_registry(),
    )
    destination_uis = UiStore(destination_bundle.main.managed_settings.state_directory)
    await register_ui(destination_uis, new_one, "Office Laptop", "office")
    transfer = GatewayConfigurationTransfer(
        destination_bundle.main.managed_settings.state_directory,
        destination_store,
        destination_uis,
        destination_bundle,
    )

    proposal = transfer.preview_import(archive)

    assert [item["display_name"] for item in proposal.preview["pending_profiles"]] == [
        "Living Room"
    ]
    office = next(
        item for item in proposal.preview["ui_mappings"] if item["display_name"] == "Office Laptop"
    )
    assert office["match"] == "display-name"
    assert office["destination_ui_id"] == str(new_one)

    await transfer.apply_import(
        archive,
        expected_revision=0,
        expected_digest=proposal.digest,
        actor_ui_id=str(new_one),
        actor_ui_name="Office Laptop",
    )

    assert destination_uis.configuration_profile_id(new_one) == old_one
    pending = next(item for item in transfer.profiles() if item["profile_id"] == str(old_two))
    assert pending["pending"] is True

    await register_ui(destination_uis, new_two, "Living Room", "living-room")
    living_room = destination_uis.get(new_two)
    assert living_room is not None
    assert [item["profile_id"] for item in transfer.pending_candidates(living_room)] == [
        str(old_two)
    ]
    await transfer.claim(new_two, old_two)

    assert destination_uis.configuration_profile_id(new_two) == old_two
    values = configuration_values_from_bundle(destination_bundle, destination_store.registry)
    effective = effective_client_configuration(
        values,
        destination_store,
        client_type="terminal",
        ui_id=new_two,
        configuration_profile_id=destination_uis.configuration_profile_id(new_two),
    )
    assert effective.settings["ui.mouse_mode"] == "auto"
    assert not transfer.status()["pending_profiles"]


@pytest.mark.asyncio
async def test_import_secret_export_backup_rollback_and_later_mutation_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bundle = bundle(tmp_path / "source")
    source_store = GatewayConfigurationStore(
        source_bundle.main.managed_settings.state_directory,
        core_configuration_registry(),
    )
    source_uis = UiStore(source_bundle.main.managed_settings.state_directory)
    monkeypatch.setenv("TFR_TRANSFER_PASSWORD", "correct horse battery staple")
    for revision, key, value in (
        (0, "worlds.alpha.host", "alpha.example"),
        (1, "worlds.alpha.port", 4201),
        (2, "worlds.alpha.login.character", "Alice"),
    ):
        source_store.set(
            key,
            value,
            scope="world",
            target="alpha",
            expected_revision=revision,
            actor_ui_id="admin",
            actor_ui_name="Admin",
        )
    source_store.set(
        "worlds.alpha.login.password",
        "env:TFR_TRANSFER_PASSWORD",
        scope="secret",
        target=None,
        expected_revision=3,
        actor_ui_id="admin",
        actor_ui_name="Admin",
    )
    source_transfer = GatewayConfigurationTransfer(
        source_bundle.main.managed_settings.state_directory,
        source_store,
        source_uis,
        source_bundle,
    )
    safe_archive = source_transfer.export()
    assert safe_archive["secrets"]["values"] == {}
    assert safe_archive["secrets"]["omitted_keys"] == ["worlds.alpha.login.password"]
    secret_archive = source_transfer.export(include_secrets=True)
    assert secret_archive["secrets"]["values"] == {
        "worlds.alpha.login.password": "correct horse battery staple"
    }

    destination_bundle = bundle(tmp_path / "destination")
    destination_store = GatewayConfigurationStore(
        destination_bundle.main.managed_settings.state_directory,
        core_configuration_registry(),
    )
    destination_uis = UiStore(destination_bundle.main.managed_settings.state_directory)
    transfer = GatewayConfigurationTransfer(
        destination_bundle.main.managed_settings.state_directory,
        destination_store,
        destination_uis,
        destination_bundle,
    )
    proposal = transfer.preview_import(secret_archive)
    await transfer.apply_import(
        secret_archive,
        expected_revision=0,
        expected_digest=proposal.digest,
        actor_ui_id="admin",
        actor_ui_name="Admin",
    )

    present, reference = destination_store.get("worlds.alpha.login.password", scope="secret")
    assert present and isinstance(reference, str) and reference.startswith("file:")
    secret_path = Path(reference.removeprefix("file:"))
    assert secret_path.read_text().strip() == "correct horse battery staple"
    if os.name == "posix":
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert transfer.status()["rollback_safe"] is True

    await transfer.rollback(expected_revision=1, actor_ui_id="admin", actor_ui_name="Admin")
    assert destination_store.get("worlds.alpha.login.password", scope="secret") == (False, None)

    second = transfer.preview_import(secret_archive)
    await transfer.apply_import(
        secret_archive,
        expected_revision=2,
        expected_digest=second.digest,
        actor_ui_id="admin",
        actor_ui_name="Admin",
    )
    destination_store.set(
        "ui.spellcheck.enabled",
        True,
        scope="shared",
        target=None,
        expected_revision=3,
        actor_ui_id="admin",
        actor_ui_name="Admin",
    )
    with pytest.raises(GatewayConfigurationError, match="no longer safe"):
        await transfer.rollback(expected_revision=4, actor_ui_id="admin", actor_ui_name="Admin")


@pytest.mark.asyncio
async def test_review_and_remove_dead_ui_settings(tmp_path: Path) -> None:
    config_bundle = bundle(tmp_path)
    store = GatewayConfigurationStore(
        config_bundle.main.managed_settings.state_directory,
        core_configuration_registry(),
    )
    uis = UiStore(config_bundle.main.managed_settings.state_directory)
    dead = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    store.set(
        "ui.mouse_mode",
        "auto",
        scope="ui",
        target=str(dead),
        expected_revision=0,
        actor_ui_id="admin",
        actor_ui_name="Admin",
    )
    transfer = GatewayConfigurationTransfer(
        config_bundle.main.managed_settings.state_directory,
        store,
        uis,
        config_bundle,
    )

    profile = transfer.profiles()[0]
    assert profile["claimed"] is False
    assert profile["pending"] is False

    result = await transfer.remove_profile(
        dead,
        expected_revision=1,
        actor_ui_id="admin",
        actor_ui_name="Admin",
    )

    assert result["removed"]["profile_id"] == str(dead)
    assert transfer.profiles() == []
    assert store.revision == 2
