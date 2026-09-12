from __future__ import annotations

import json
from pathlib import Path

import pytest

from tfr.eventlog import serialize_event
from tfr.events import EventKind
from tfr.replay import ReplayError, event_from_dict, read_transcript

FIXTURE = Path(__file__).parent / "fixtures" / "transcript.jsonl"


def test_reads_multiple_worlds_from_jsonl_fixture() -> None:
    events = read_transcript(FIXTURE)

    assert [event.world for event in events] == ["alpha", "beta"]
    assert events[0].kind is EventKind.SAY
    assert events[0].provenance is not None
    assert events[0].provenance.sender_name == "Alice"
    assert events[0].display_text == 'Alice says, "Hello"\r\n'


def test_serialized_event_round_trips_for_deterministic_replay() -> None:
    original = read_transcript(FIXTURE)[0]

    restored = event_from_dict(json.loads(serialize_event(original)))

    assert restored == original


def test_invalid_transcript_reports_file_and_line(tmp_path: Path) -> None:
    transcript = tmp_path / "broken.jsonl"
    transcript.write_text("{}\nnot-json\n", encoding="utf-8")

    with pytest.raises(ReplayError, match=r"broken\.jsonl:1"):
        read_transcript(transcript)
