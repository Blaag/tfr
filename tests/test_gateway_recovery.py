from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import trustme
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tfr.config import AgentsConfig, ConfigurationBundle, MainConfig, WorldsConfig
from tfr.configuration_transfer import GatewayConfigurationTransfer
from tfr.core_settings import core_configuration_registry
from tfr.gateway_configuration import GatewayConfigurationStore
from tfr.gateway_devices import DeviceStore
from tfr.gateway_recovery import (
    build_gateway_recovery,
    restore_gateway_recovery,
    validate_gateway_recovery,
)
from tfr.gateway_uis import UiStore


def recovery_source(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    certificate_authority = trustme.CA()
    certificate = certificate_authority.issue_cert("gateway.example.ts.net")
    config = tmp_path / "source-config"
    web = tmp_path / "source-web"
    config.mkdir(mode=0o700)
    web.mkdir(mode=0o700)
    token = config / "gateway.token"
    certificate_path = config / "gateway.crt"
    key_path = config / "gateway.key"
    token.write_text("a" * 64 + "\n", encoding="ascii")
    token.chmod(0o600)
    certificate.cert_chain_pems[0].write_to_path(certificate_path)
    certificate_path.chmod(0o644)
    certificate.private_key_pem.write_to_path(key_path)
    key_path.chmod(0o600)
    (web / "devices.json").write_text('{"schema_version":2,"devices":[]}\n', encoding="utf-8")
    (web / "provisioning.json").write_text(
        '{"schema_version":1,"origin":"https://gateway.example.ts.net"}\n',
        encoding="utf-8",
    )
    vapid = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (web / "web-push-vapid.pem").write_bytes(vapid)
    for path in web.iterdir():
        path.chmod(0o600)
    return build_gateway_recovery(
        listen_host="100.64.0.1",
        listen_port=7347,
        token_file=token,
        tls_certificate=certificate_path,
        tls_private_key=key_path,
        ui_state=UiStore(tmp_path / "source-ui").recovery_state(),
        web_state_directory=web,
        web_origin="https://gateway.example.ts.net",
        web_transport="tailscale",
        web_target="http://127.0.0.1:7348",
    )


def test_complete_gateway_recovery_round_trip(tmp_path: Path) -> None:
    recovery = recovery_source(tmp_path)

    details = restore_gateway_recovery(
        recovery,
        config_directory=tmp_path / "destination-config",
        managed_state_directory=tmp_path / "destination-state/settings",
        web_state_directory=tmp_path / "destination-state/web",
    )

    assert details["listen_host"] == "100.64.0.1"
    assert details["certificate_host"] == "gateway.example.ts.net"
    assert details["web_target"] == "http://127.0.0.1:7348"
    assert (tmp_path / "destination-config/gateway.token").read_text().strip() == "a" * 64
    assert (
        json.loads((tmp_path / "destination-state/settings/uis.json").read_text(encoding="utf-8"))[
            "schema_version"
        ]
        == 3
    )
    assert (
        json.loads((tmp_path / "destination-state/web/devices.json").read_text(encoding="utf-8"))[
            "devices"
        ]
        == []
    )
    assert stat.S_IMODE((tmp_path / "destination-config/gateway.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "destination-config/gateway.crt").stat().st_mode) == 0o644


def test_private_configuration_export_carries_validated_recovery(tmp_path: Path) -> None:
    base = ConfigurationBundle(
        main_path=tmp_path / "config.jsonc",
        worlds_path=tmp_path / "worlds.jsonc",
        agents_path=tmp_path / "agents.jsonc",
        main=MainConfig.model_validate(
            {"managed_settings": {"state_directory": str(tmp_path / "managed")}}
        ),
        worlds=WorldsConfig(),
        agents=AgentsConfig(),
    )
    store = GatewayConfigurationStore(
        base.main.managed_settings.state_directory, core_configuration_registry()
    )
    transfer = GatewayConfigurationTransfer(
        base.main.managed_settings.state_directory,
        store,
        UiStore(base.main.managed_settings.state_directory),
        base,
    )

    archive = transfer.export(include_secrets=True, recovery=recovery_source(tmp_path / "source"))
    preview = transfer.preview_import(archive).preview

    assert archive["format_version"] == 2
    assert archive["recovery"] is not None
    assert preview["recovery_included"] is True

    progress = base.main.managed_settings.state_directory / "recovery-setup.json"
    progress.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "archive_digest": "a" * 64,
                "phase": "route-verified",
                "updated_at": "2026-11-16T20:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    assert transfer.status()["recovery_setup"] == {
        "phase": "route-verified",
        "updated_at": "2026-11-16T20:00:00Z",
        "archive_digest": "a" * 12,
    }


@pytest.mark.asyncio
async def test_recovery_import_rebases_ui_convergence_revisions(tmp_path: Path) -> None:
    base = ConfigurationBundle(
        main_path=tmp_path / "config.jsonc",
        worlds_path=tmp_path / "worlds.jsonc",
        agents_path=tmp_path / "agents.jsonc",
        main=MainConfig.model_validate(
            {"managed_settings": {"state_directory": str(tmp_path / "managed")}}
        ),
        worlds=WorldsConfig(),
        agents=AgentsConfig(),
    )
    store = GatewayConfigurationStore(
        base.main.managed_settings.state_directory, core_configuration_registry()
    )
    uis = UiStore(base.main.managed_settings.state_directory)
    ui_id = UUID("22222222-2222-4222-8222-222222222222")
    await uis.register(
        ui_id=ui_id,
        client_type="terminal",
        platform="restored-host",
        versions={},
        capabilities=(),
        applied_config_revision=28,
    )
    transfer = GatewayConfigurationTransfer(
        base.main.managed_settings.state_directory,
        store,
        uis,
        base,
    )
    archive = transfer.export(include_secrets=True, recovery=recovery_source(tmp_path / "source"))
    proposal = transfer.preview_import(archive)

    await transfer.apply_import(
        archive,
        expected_revision=0,
        expected_digest=proposal.digest,
        actor_ui_id="local-admin",
        actor_ui_name="Local administrator",
    )

    assert uis.get(ui_id) is not None
    assert uis.get(ui_id).applied_config_revision is None  # type: ignore[union-attr]


def test_recovery_rejects_mismatched_tls_key(tmp_path: Path) -> None:
    recovery = recovery_source(tmp_path)
    gateway = recovery["gateway"]
    assert isinstance(gateway, dict)
    gateway["tls_private_key"] = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )

    with pytest.raises(ValueError, match="do not match"):
        validate_gateway_recovery(recovery)


def test_direct_web_tls_is_restored_separately(tmp_path: Path) -> None:
    recovery = recovery_source(tmp_path)
    gateway = recovery["gateway"]
    web = recovery["web"]
    assert isinstance(gateway, dict) and isinstance(web, dict)
    web.update(
        {
            "transport": "direct",
            "target": None,
            "direct_host": "gateway.example.ts.net",
            "direct_port": 7443,
            "tls_certificate": gateway["tls_certificate"],
            "tls_private_key": gateway["tls_private_key"],
        }
    )

    details = restore_gateway_recovery(
        validate_gateway_recovery(recovery),
        config_directory=tmp_path / "destination-config",
        managed_state_directory=tmp_path / "destination-state/settings",
        web_state_directory=tmp_path / "destination-state/web",
    )

    assert details["web_transport"] == "direct"
    assert (tmp_path / "destination-config/web-gateway.crt").is_file()
    assert stat.S_IMODE((tmp_path / "destination-config/web-gateway.key").stat().st_mode) == 0o600


def test_recovery_refuses_to_replace_existing_credentials(tmp_path: Path) -> None:
    recovery = recovery_source(tmp_path)
    destination = tmp_path / "destination-config"
    destination.mkdir()
    (destination / "gateway.token").write_text("existing", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to replace"):
        restore_gateway_recovery(
            recovery,
            config_directory=destination,
            managed_state_directory=tmp_path / "destination-state/settings",
            web_state_directory=tmp_path / "destination-state/web",
        )


def test_recovery_retry_accepts_identical_private_staged_files(tmp_path: Path) -> None:
    recovery = recovery_source(tmp_path)
    arguments = {
        "config_directory": tmp_path / "destination-config",
        "managed_state_directory": tmp_path / "destination-state/settings",
        "web_state_directory": tmp_path / "destination-state/web",
    }

    first = restore_gateway_recovery(recovery, **arguments)
    second = restore_gateway_recovery(recovery, **arguments)

    assert second == first


@pytest.mark.asyncio
async def test_recovery_preserves_native_and_pwa_credentials(tmp_path: Path) -> None:
    recovery = recovery_source(tmp_path)
    native_ui_id = UUID("11111111-1111-4111-8111-111111111111")
    native_token = "native-paired-token"
    ui_state = recovery["ui_state"]
    assert isinstance(ui_state, dict)
    credentials = ui_state["credentials"]
    assert isinstance(credentials, dict)
    credentials[str(native_ui_id)] = hashlib.sha256(native_token.encode("ascii")).hexdigest()

    device_source = tmp_path / "device-source"
    devices = DeviceStore(device_source, "https://gateway.example.ts.net")
    pairing_url = devices.create_pairing_url("Phone", ("alpha",))
    code = parse_qs(urlsplit(pairing_url).fragment)["pair"][0]
    _device, pwa_token = await devices.redeem(code, "owner@example.com")
    web = recovery["web"]
    assert isinstance(web, dict)
    files = web["files"]
    assert isinstance(files, dict)
    files["devices.json"] = (device_source / "devices.json").read_text(encoding="utf-8")

    restore_gateway_recovery(
        validate_gateway_recovery(recovery),
        config_directory=tmp_path / "destination-config",
        managed_state_directory=tmp_path / "destination-state/settings",
        web_state_directory=tmp_path / "destination-state/web",
    )

    restored_uis = UiStore(tmp_path / "destination-state/settings")
    restored_devices = DeviceStore(
        tmp_path / "destination-state/web", "https://gateway.example.ts.net"
    )
    assert restored_uis.authenticate_credential(native_ui_id, native_token) is True
    assert restored_devices.authenticate(pwa_token) is not None
