from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from tfr.gateway_profiles import (
    GatewayConnectionProfile,
    GatewayProfileStore,
    interactive_gateway_setup,
)

TOKEN = "a" * 64


def profile(name: str = "home") -> GatewayConnectionProfile:
    return GatewayConnectionProfile(
        name=name,
        host="gateway.example",
        port=7347,
        tls_server_name="gateway.example",
        tls_ca=None,
        credential_reference="credential:pending",
        ui_id=uuid4(),
        ui_name="Test Terminal",
    )


def test_profiles_persist_stable_identity_and_private_credential(tmp_path: Path) -> None:
    store = GatewayProfileStore(tmp_path / "connections")
    original = profile()
    store.add(original, TOKEN)

    loaded = GatewayProfileStore(tmp_path / "connections")

    assert loaded.active is not None
    assert loaded.active.ui_id == original.ui_id
    assert loaded.credentials.load(loaded.active.credential_reference) == TOKEN
    assert TOKEN not in loaded.path.read_text(encoding="utf-8")
    assert loaded.path.stat().st_mode & 0o777 == 0o600


def test_profile_management_and_export_omit_credentials(tmp_path: Path) -> None:
    store = GatewayProfileStore(tmp_path / "connections")
    store.add(profile("home"), TOKEN)
    store.add(profile("office"), "b" * 64, activate=False)

    assert store.switch("office").name == "office"
    assert store.rename("office", "work").name == "work"
    repaired = store.repair(
        "work",
        host="new.example",
        port=7443,
        tls_server_name="tls.example",
        tls_ca=None,
        token="c" * 64,
    )
    assert repaired.ui_id == store.active.ui_id  # type: ignore[union-attr]
    assert store.credentials.load(repaired.credential_reference) == "c" * 64
    exported = json.dumps(store.export())
    assert "credential" not in exported
    assert TOKEN not in exported

    forgotten = store.forget("work")
    assert store.active is not None and store.active.name == "home"
    assert not (
        store.credentials.directory / forgotten.credential_reference.split(":", 1)[1]
    ).exists()


def test_interactive_setup_collects_connection_and_proposes_defaults(tmp_path: Path) -> None:
    answers = iter(["", "gateway.example", "", "", "", "My Terminal"])
    store = GatewayProfileStore(tmp_path / "connections")

    created = interactive_gateway_setup(
        store,
        input_fn=lambda _prompt: next(answers),
        secret_fn=lambda _prompt: TOKEN,
    )

    assert created.name == "default"
    assert created.host == "gateway.example"
    assert created.port == 7347
    assert created.tls_server_name == "gateway.example"
    assert created.ui_id == store.active.ui_id  # type: ignore[union-attr]
    assert created.ui_name == "My Terminal"


def test_private_recovery_bundle_restores_same_ui_identity(tmp_path: Path) -> None:
    source = GatewayProfileStore(tmp_path / "source")
    original = profile("home")
    source.add(original, TOKEN)
    bundle = source.export_recovery(tmp_path / "backup" / "ui-recovery.json")

    assert os.stat(bundle).st_mode & 0o777 == 0o600
    value = json.loads(bundle.read_text(encoding="utf-8"))
    assert value["format"] == "tfr-ui-recovery"
    assert value["schema_version"] == 2
    assert value["profiles"][0]["auth_token"] == TOKEN
    assert "credential_reference" not in value["profiles"][0]["profile"]

    restored_store = GatewayProfileStore(tmp_path / "restored")
    restored = restored_store.restore_recovery(bundle, recover_identity=True)

    assert restored.ui_id == original.ui_id
    assert restored.ui_name == original.ui_name
    assert restored_store.credentials.load(restored.credential_reference) == TOKEN


def test_recovery_bundle_can_create_a_distinct_new_ui(tmp_path: Path) -> None:
    source = GatewayProfileStore(tmp_path / "source")
    original = profile()
    source.add(original, TOKEN)
    bundle = source.export_recovery(tmp_path / "ui-recovery.json")
    restored_store = GatewayProfileStore(tmp_path / "new-ui")

    restored = restored_store.restore_recovery(bundle, recover_identity=False, ui_name="New Laptop")

    assert restored.ui_id != original.ui_id
    assert restored.ui_name == "New Laptop"


def test_recovery_bundle_preserves_all_profiles_and_active_selection(tmp_path: Path) -> None:
    source = GatewayProfileStore(tmp_path / "source")
    first = profile("home")
    second = profile("office")
    source.add(first, TOKEN)
    source.add(second, "b" * 64, activate=True)
    bundle = source.export_recovery(tmp_path / "ui-recovery.json")
    restored_store = GatewayProfileStore(tmp_path / "restored")

    restored = restored_store.restore_recovery(bundle, recover_identity=True)

    assert restored.name == "office"
    assert restored_store.active is not None and restored_store.active.name == "office"
    assert {item.name for item in restored_store.profiles()} == {"home", "office"}
    assert {item.ui_id for item in restored_store.profiles()} == {first.ui_id, second.ui_id}


def test_recovery_export_refuses_to_overwrite_existing_file(tmp_path: Path) -> None:
    store = GatewayProfileStore(tmp_path / "source")
    store.add(profile(), TOKEN)
    destination = tmp_path / "ui-recovery.json"
    destination.write_text("keep", encoding="utf-8")

    try:
        store.export_recovery(destination)
    except FileExistsError:
        pass
    else:
        raise AssertionError("recovery export unexpectedly overwrote an existing file")

    assert destination.read_text(encoding="utf-8") == "keep"
