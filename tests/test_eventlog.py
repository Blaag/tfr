from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from tfr.eventlog import JsonlEventSink
from tfr.events import Direction, Event, EventKind


def make_event(sequence: int) -> Event:
    return Event(
        session_id=UUID("63f755aa-e407-4f78-ae05-f9d62c23f765"),
        world="example",
        connection_generation=1,
        sequence=sequence,
        direction=Direction.INBOUND,
        kind=EventKind.RAW_OUTPUT,
        timestamp=datetime(2026, 9, 8, 12, 30, sequence, tzinfo=UTC),
        canonical_text=f"line {sequence}",
        plain_text=f"line {sequence}",
    )


async def test_appends_ordered_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "events.jsonl"

    async with JsonlEventSink(path) as sink:
        await sink.write(make_event(1))
        await sink.write(make_event(2))

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["sequence"] for record in records] == [1, 2]
    assert [record["canonical_text"] for record in records] == ["line 1", "line 2"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are required")
async def test_creates_private_log_file(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    async with JsonlEventSink(path) as sink:
        await sink.write(make_event(1))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are required")
async def test_restricts_existing_log_file_permissions(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.touch(mode=0o644)

    async with JsonlEventSink(path) as sink:
        await sink.write(make_event(1))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
