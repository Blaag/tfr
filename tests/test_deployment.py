from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tfr.deployment import (
    deployment_schema,
    ensure_verified_deployment,
    load_deployment,
    reconcile_remote_ui_deployment,
    write_deployment,
)


def test_reconciles_legacy_remote_ui_deployment_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = tmp_path / "tfr/deployment.json"
    path.parent.mkdir()
    path.write_text('{"schema_version":1,"profile":"remote-ui"}\n', encoding="utf-8")
    path.chmod(0o644)

    value = reconcile_remote_ui_deployment()

    assert value == {"schema_version": 2, "profile": "remote-ui"}
    assert json.loads(path.read_text(encoding="utf-8")) == value
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".deployment.json.*.tmp"))


def test_compatibility_schema_read_reconciles_legacy_remote_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = tmp_path / "tfr/deployment.json"
    path.parent.mkdir()
    path.write_text('{"schema_version":1,"profile":"remote-ui"}\n', encoding="utf-8")

    assert deployment_schema(reconcile_remote_ui=True) == 2
    assert load_deployment() == {"schema_version": 2, "profile": "remote-ui"}


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"schema_version":1,"profile":"unknown"}',
        '{"schema_version":1,"profile":"remote-ui","unexpected":true}',
        '{"schema_version":1,"profile":"remote-ui","gateway_socket":"/ambiguous"}',
    ],
)
def test_does_not_rewrite_ambiguous_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = tmp_path / "tfr/deployment.json"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")

    assert reconcile_remote_ui_deployment() is None
    assert path.read_text(encoding="utf-8") == content


def test_does_not_follow_deployment_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "target.json"
    target.write_text('{"schema_version":1,"profile":"remote-ui"}', encoding="utf-8")
    path = tmp_path / "state/tfr/deployment.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(target)

    assert reconcile_remote_ui_deployment() is None
    assert '"schema_version":1' in target.read_text(encoding="utf-8")


def test_installer_writer_always_uses_current_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    write_deployment("remote-ui")

    assert load_deployment() == {"schema_version": 2, "profile": "remote-ui"}


def test_verified_local_connection_migrates_matching_legacy_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = tmp_path / "tfr/deployment.json"
    path.parent.mkdir()
    path.write_text(
        '{"schema_version":1,"profile":"local-split","gateway_socket":"/old.sock"}\n',
        encoding="utf-8",
    )

    assert ensure_verified_deployment("local-split", gateway_socket="/live.sock") is True
    assert load_deployment() == {
        "schema_version": 2,
        "profile": "local-split",
        "gateway_socket": "/live.sock",
    }


def test_verified_local_connection_updates_stale_current_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    write_deployment("local-split", gateway_socket="/old.sock")

    assert ensure_verified_deployment("local-split", gateway_socket="/live.sock") is True
    assert load_deployment() == {
        "schema_version": 2,
        "profile": "local-split",
        "gateway_socket": "/live.sock",
    }
