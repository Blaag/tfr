from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from tfr.configuration_cli import run_config_cli


class FakeClient:
    def __init__(self) -> None:
        self.start = lambda: None
        self.stop = AsyncMock()
        self.config_status = AsyncMock(return_value={"revision": 7})
        self.config_list = AsyncMock(
            return_value=[
                {
                    "key": "ui.typing_glow.enabled",
                    "type": "boolean",
                    "scope": "ui-type",
                },
                {
                    "key": "ui.typing_glow.duration_seconds",
                    "type": "number",
                    "scope": "ui-type",
                },
            ]
        )
        self.config_batch = AsyncMock(
            return_value={"revision": 8, "restart": "client-restart"}
        )


def test_config_cli_previews_typed_atomic_batch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeClient()

    async def connect(_args):
        return client

    monkeypatch.setattr("tfr.configuration_cli._connect", connect)

    result = run_config_cli(
        [
            "--json",
            "set",
            "ui.typing_glow.enabled=off",
            "ui.typing_glow.duration_seconds=0.25",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["changes"][0]["value"] is False
    assert payload["changes"][1]["value"] == 0.25
    client.config_batch.assert_awaited_once_with(
        [
            {
                "operation": "set",
                "key": "ui.typing_glow.enabled",
                "value": False,
                "scope": "ui-type",
                "target": "terminal",
            },
            {
                "operation": "set",
                "key": "ui.typing_glow.duration_seconds",
                "value": 0.25,
                "scope": "ui-type",
                "target": "terminal",
            },
        ],
        expected_revision=7,
        confirm=False,
    )


def test_config_cli_apply_uses_one_confirmed_batch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeClient()

    async def connect(_args):
        return client

    monkeypatch.setattr("tfr.configuration_cli._connect", connect)

    assert (
        run_config_cli(
            ["set", "ui.typing_glow.enabled", "on", "--ui-type", "terminal", "--apply"]
        )
        == 0
    )

    assert "Applied" in capsys.readouterr().out
    assert client.config_batch.await_args.kwargs == {
        "expected_revision": 7,
        "confirm": True,
    }


def test_config_cli_rejects_ambiguous_per_ui_target(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeClient()
    client.config_list.return_value = [
        {"key": "ui.animations_enabled", "type": "boolean", "scope": "ui"}
    ]

    async def connect(_args):
        return client

    monkeypatch.setattr("tfr.configuration_cli._connect", connect)

    assert run_config_cli(["set", "ui.animations_enabled", "off"]) == 2
    assert "requires --ui NAME" in capsys.readouterr().err
