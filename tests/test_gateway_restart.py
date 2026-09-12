from __future__ import annotations

from uuid import UUID

from tfr.gateway_client import (
    _RESTART_CURSOR,
    _RESTART_GATEWAY_ID,
    _RESTART_WORLD,
    _restart_state_from_environment,
)


def test_restart_state_round_trips_once(monkeypatch) -> None:
    gateway_id = UUID("92716400-4bb9-43d2-845f-b8a0e51c9994")
    monkeypatch.setenv(_RESTART_GATEWAY_ID, str(gateway_id))
    monkeypatch.setenv(_RESTART_CURSOR, "42")
    monkeypatch.setenv(_RESTART_WORLD, "beta")

    assert _restart_state_from_environment() == (gateway_id, "beta")
    assert _restart_state_from_environment() == (None, None)


def test_invalid_restart_state_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv(_RESTART_GATEWAY_ID, "invalid")
    monkeypatch.setenv(_RESTART_CURSOR, "-1")
    monkeypatch.setenv(_RESTART_WORLD, "alpha")

    assert _restart_state_from_environment() == (None, None)
