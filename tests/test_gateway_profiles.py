from __future__ import annotations

import json
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
