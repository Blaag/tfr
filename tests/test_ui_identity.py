from __future__ import annotations

import json
import stat
from uuid import UUID

import pytest

from tfr.ui_identity import load_or_create_native_ui_id


def test_native_ui_id_survives_restart(tmp_path) -> None:
    state_directory = tmp_path / "ui"

    first = load_or_create_native_ui_id(state_directory)
    second = load_or_create_native_ui_id(state_directory)

    assert second == first
    assert stat.S_IMODE(state_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((state_directory / "native-ui.json").stat().st_mode) == 0o600


def test_native_ui_id_loads_existing_identity(tmp_path) -> None:
    state_directory = tmp_path / "ui"
    state_directory.mkdir(mode=0o700)
    identity_path = state_directory / "native-ui.json"
    identity_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ui_id": "11111111-1111-4111-8111-111111111111",
            }
        ),
        encoding="utf-8",
    )
    identity_path.chmod(0o600)

    assert load_or_create_native_ui_id(state_directory) == UUID(
        "11111111-1111-4111-8111-111111111111"
    )


def test_native_ui_id_rejects_insecure_identity_file(tmp_path) -> None:
    state_directory = tmp_path / "ui"
    state_directory.mkdir(mode=0o700)
    identity_path = state_directory / "native-ui.json"
    identity_path.write_text(
        '{"schema_version":1,"ui_id":"11111111-1111-4111-8111-111111111111"}',
        encoding="utf-8",
    )
    identity_path.chmod(0o644)

    with pytest.raises(ValueError, match="must not grant group or other permissions"):
        load_or_create_native_ui_id(state_directory)
