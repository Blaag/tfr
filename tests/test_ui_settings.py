from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tfr.ui_settings import LocalUiSettingsStore, UiSettingsError


def test_mouse_mode_persists_per_ui_machine(tmp_path: Path) -> None:
    state = tmp_path / "settings"
    store = LocalUiSettingsStore(state)

    assert store.mouse_mode is None
    store.set_mouse_mode("terminal")

    assert LocalUiSettingsStore(state).mouse_mode == "terminal"
    if os.name == "posix":
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_mouse_store_rejects_symlinks_and_open_permissions(tmp_path: Path) -> None:
    state = tmp_path / "settings"
    state.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    (state / "ui-overrides.json").symlink_to(target)

    with pytest.raises(UiSettingsError):
        LocalUiSettingsStore(state)
    assert target.read_text(encoding="utf-8") == "unchanged"

    if os.name == "posix":
        open_state = tmp_path / "open"
        open_state.mkdir(mode=0o755)
        with pytest.raises(UiSettingsError, match="private"):
            LocalUiSettingsStore(open_state)
