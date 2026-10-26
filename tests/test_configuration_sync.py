from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tfr.config import MainConfig, UiConfiguration
from tfr.configuration_sync import (
    effective_client_configuration,
    required_action,
    validate_configuration_ack,
)
from tfr.configuration_values import ConfigurationValues
from tfr.core import EventBus
from tfr.core_settings import core_configuration_registry
from tfr.gateway_client import GatewayClient, _apply_gateway_ui_configuration
from tfr.gateway_configuration import GatewayConfigurationStore
from tfr.gateway_protocol import GatewayProtocolError, read_message, write_message
from tfr.gateway_uis import UiStore
from tfr.gateway_web import WebGatewayServer


def test_effective_configuration_uses_shared_type_and_individual_precedence(
    tmp_path: Path,
) -> None:
    registry = core_configuration_registry()
    values = ConfigurationValues(registry)
    store = GatewayConfigurationStore(tmp_path / "configuration", registry)
    first_ui = uuid4()
    second_ui = uuid4()
    changes = (
        ("ui.spellcheck.enabled", True, "shared", None),
        ("ui.theme.preset", "dracula", "ui-type", "terminal"),
        ("ui.theme.preset", "nord", "ui", str(first_ui)),
    )
    for key, value, scope, target in changes:
        store.set(
            key,
            value,
            scope=scope,
            target=target,
            expected_revision=store.revision,
            actor_ui_id="test",
            actor_ui_name="Test",
        )

    first = effective_client_configuration(
        values, store, client_type="terminal", ui_id=first_ui
    )
    second = effective_client_configuration(
        values, store, client_type="terminal", ui_id=second_ui
    )
    pwa = effective_client_configuration(values, store, client_type="pwa", ui_id=uuid4())

    assert first.revision == 3
    assert first.settings["ui.spellcheck.enabled"] is True
    assert first.settings["ui.theme.preset"] == "nord"
    assert second.settings["ui.theme.preset"] == "dracula"
    assert "ui.theme.preset" not in pwa.settings
    assert all("password" not in key and "api_key" not in key for key in first.settings)


def test_gateway_ui_configuration_is_an_in_memory_overlay() -> None:
    local = UiConfiguration(
        main_path=Path("config.jsonc"),
        main=MainConfig.model_validate(
            {"ui": {"mouse_mode": "terminal"}, "updates": {"channel": "stable"}}
        ),
    )

    effective = _apply_gateway_ui_configuration(
        local,
        {
            "ui.mouse_mode": "tfr",
            "ui.theme.preset": "nord",
            "updates.channel": "candidate",
        },
    )

    assert effective.main.ui.mouse_mode == "tfr"
    assert effective.main.ui.theme.preset == "nord"
    assert effective.main.updates.channel == "candidate"
    assert local.main.ui.mouse_mode == "terminal"
    assert local.main.updates.channel == "stable"


def test_client_rejects_invalid_configuration_envelopes() -> None:
    with pytest.raises(GatewayProtocolError, match="envelope"):
        GatewayClient._configuration(
            {
                "revision": 1,
                "settings": {"ui.mouse_mode": "tfr"},
                "requirements": {},
            }
        )
    with pytest.raises(GatewayProtocolError, match="unsupported configuration"):
        GatewayClient._configuration(
            {
                "revision": 1,
                "settings": {"logging.enabled": True},
                "requirements": {"logging.enabled": "live"},
            }
        )


def test_configuration_ack_validation_and_required_action() -> None:
    assert validate_configuration_ack(
        {"revision": 4, "status": "unsupported", "issues": ["missing capability"]},
        maximum_revision=4,
    ) == (4, "unsupported", ("missing capability",))
    assert required_action({"one": "live", "two": "client-reload"}) == "reload-required"
    assert required_action({"one": "client-restart"}) == "restart-required"
    with pytest.raises(ValueError, match="requires issues"):
        validate_configuration_ack(
            {"revision": 4, "status": "unsupported", "issues": []},
            maximum_revision=4,
        )


async def test_ui_store_tracks_application_reports_without_losing_applied_revision(
    tmp_path: Path,
) -> None:
    store = UiStore(tmp_path / "uis")
    record = await store.register(
        ui_id=uuid4(),
        client_type="terminal",
        platform="test",
        versions={},
        capabilities=[],
    )

    await store.report_configuration(record.ui_id, 2, "unsupported", ("missing",))
    assert store.get(record.ui_id).applied_config_revision is None  # type: ignore[union-attr]
    assert store.configuration_report(record.ui_id) == (2, "unsupported", ("missing",))

    await store.report_configuration(record.ui_id, 2, "applied", ())
    assert store.get(record.ui_id).applied_config_revision == 2  # type: ignore[union-attr]
    assert store.configuration_report(record.ui_id) == (2, "applied", ())

    await store.report_configuration(record.ui_id, 1, "applied", ())
    assert store.get(record.ui_id).applied_config_revision == 2  # type: ignore[union-attr]
    assert store.configuration_report(record.ui_id) == (2, "applied", ())


async def test_web_configuration_notice_does_not_take_over_server_shutdown() -> None:
    server = object.__new__(WebGatewayServer)
    server.runtime = SimpleNamespace(configuration_values=object(), gateway_configuration=object())
    server._active_sockets = {}
    server._paste_transfers = {}
    server._runner = SimpleNamespace(cleanup=AsyncMock())
    server._site = object()

    await server.notify_configuration()

    assert server._runner is not None
    server._runner.cleanup.assert_not_awaited()

    runner = server._runner
    await server.stop()
    runner.cleanup.assert_awaited_once()
    assert server._runner is None
    assert server._site is None


def test_initial_configuration_reports_unknown_plugin_setting_as_unsupported() -> None:
    client = object.__new__(GatewayClient)
    client.effective_configuration = {"plugins.config.missing.option": True}

    status, issues = client._configuration_application_result(())

    assert status == "unsupported"
    assert issues == ("unsupported setting: plugins.config.missing.option",)


async def test_pushed_configuration_is_acknowledged_without_blocking_the_read_pump() -> None:
    acknowledgement: asyncio.Future[dict[str, object]] = (
        asyncio.get_running_loop().create_future()
    )

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await write_message(
            writer,
            {
                "type": "configuration",
                "revision": 2,
                "settings": {"ui.spellcheck.enabled": True},
                "requirements": {"ui.spellcheck.enabled": "live"},
            },
        )
        message = await read_message(reader)
        assert message is not None
        acknowledgement.set_result(message)
        await write_message(
            writer,
            {
                "type": "ack",
                "request_id": message["request_id"],
                "ok": True,
            },
        )
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    reader, writer = await asyncio.open_connection(address[0], address[1])
    client = GatewayClient(
        reader=reader,
        writer=writer,
        client_id=uuid4(),
        gateway_id=uuid4(),
        cursor=0,
        oldest_cursor=1,
        history_truncated=False,
        history_reset=False,
        event_bus=EventBus(),
        sessions=[],
        initial_events=(),
        agent_worlds=set(),
        configuration_revision=1,
        effective_configuration={"ui.spellcheck.enabled": False},
        configuration_requirements={"ui.spellcheck.enabled": "live"},
    )
    client._initial_configuration_ack_pending = False
    client.configuration_handler = lambda _settings, _requirements: ("applied", ())
    async with server:
        client.start()
        message = await asyncio.wait_for(acknowledgement, timeout=1)
        assert message["type"] == "configuration_applied"
        assert message["revision"] == 2
        await asyncio.wait_for(client.stop(), timeout=1)
        await client.event_bus.close()
