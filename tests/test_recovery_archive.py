from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tfr.configuration_cli import read_configuration_archive
from tfr.recovery_archive import (
    restore_complete_recovery_logs,
    write_complete_recovery_archive,
)


def test_complete_recovery_archive_round_trips_canonical_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir()
    first = b'{"event_id":"one"}\n'
    second = b'{"event_id":"two"}\n'
    (logs / "tfr-20261101T000000Z-1.jsonl").write_bytes(first)
    (logs / "tfr-20261102T000000Z-2.jsonl").write_bytes(second)
    document: dict[str, object] = {"format": "test", "recovery": {"private": True}}
    destination = tmp_path / "complete.tfr-recovery"

    summary = write_complete_recovery_archive(destination, document, logs)

    assert summary == {"files": 2, "bytes": len(first) + len(second)}
    assert read_configuration_archive(destination) == document
    shutil.rmtree(logs)
    restored = restore_complete_recovery_logs(destination)
    assert restored == summary
    assert (logs / "tfr-20261101T000000Z-1.jsonl").read_bytes() == first
    assert (logs / "tfr-20261102T000000Z-2.jsonl").read_bytes() == second
    assert restore_complete_recovery_logs(destination) == {"files": 0, "bytes": 0}


def test_complete_recovery_refuses_conflicting_existing_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "tfr-20261101T000000Z-1.jsonl"
    path.write_bytes(b'{"event_id":"one"}\n')
    destination = tmp_path / "complete.tfr-recovery"
    write_complete_recovery_archive(destination, {"format": "test"}, logs)
    path.write_bytes(b'{"event_id":"different"}\n')

    with pytest.raises(ValueError, match="conflicts"):
        restore_complete_recovery_logs(destination)


def test_complete_recovery_refuses_incomplete_active_log(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "tfr-20261101T000000Z-1.jsonl").write_bytes(b'{"event_id":"partial"}')

    with pytest.raises(ValueError, match="incomplete final record"):
        write_complete_recovery_archive(
            tmp_path / "complete.tfr-recovery", {"format": "test"}, logs
        )


def test_complete_recovery_refuses_symlink_log_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "tfr-20261101T000000Z-1.jsonl").write_bytes(b'{"event_id":"one"}\n')
    destination = tmp_path / "complete.tfr-recovery"
    write_complete_recovery_archive(destination, {"format": "test"}, logs)
    shutil.rmtree(logs)
    outside = tmp_path / "outside"
    outside.mkdir()
    logs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        restore_complete_recovery_logs(destination)
