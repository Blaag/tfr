from __future__ import annotations

import os
from pathlib import Path

import pytest

from tfr.doctor import _find_uv


def test_find_uv_checks_standard_user_install_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv = tmp_path / ".local" / "bin" / "uv"
    uv.parent.mkdir(parents=True)
    uv.write_text("#!/bin/sh\n", encoding="utf-8")
    uv.chmod(0o700)
    monkeypatch.setattr("tfr.executables.shutil.which", lambda _name: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert _find_uv() == uv
    assert os.access(uv, os.X_OK)
