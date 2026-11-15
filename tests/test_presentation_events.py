from __future__ import annotations

from uuid import UUID

import pytest

from tfr.events import Confidence, Direction, Event, EventKind, Provenance
from tfr.presentation_events import infer_presentation_speaker


def page(text: str) -> Event:
    return Event(
        session_id=UUID(int=1),
        world="alpha",
        connection_generation=1,
        sequence=1,
        direction=Direction.INBOUND,
        kind=EventKind.PAGE,
        canonical_text=text,
        plain_text=text,
        display_text=text,
    )


@pytest.mark.parametrize(
    ("text", "speaker"),
    (
        ("Wedge pages: testing", "Wedge"),
        ("Wedge pages (black Blood): testes testes", "Wedge"),
        ("To (Black, wedge), Blood pages: testing", "Blood"),
    ),
)
def test_page_formats_infer_display_speaker(text: str, speaker: str) -> None:
    inferred = infer_presentation_speaker(page(text), text)

    assert inferred.provenance is not None
    assert inferred.provenance.sender_name == speaker


def test_unknown_page_format_prefers_bounded_first_token_attribution() -> None:
    event = page("A strange server-specific page arrived")

    inferred = infer_presentation_speaker(event, event.display_text)
    assert inferred.provenance is not None
    assert inferred.provenance.sender_name == "A"


@pytest.mark.parametrize(
    "provenance",
    (
        Provenance(sender_name="Blood", adapter="tinymux", confidence=Confidence.HIGH),
        Provenance(sender_dbref=42, adapter="tinymux", confidence=Confidence.HIGH),
    ),
)
def test_provenance_bearing_raw_pose_is_inferred_for_display(provenance: Provenance) -> None:
    event = Event(
        session_id=UUID(int=1),
        world="alpha",
        connection_generation=1,
        sequence=1,
        direction=Direction.INBOUND,
        kind=EventKind.RAW_OUTPUT,
        plain_text="Blood waves.",
        display_text="Blood waves.",
        provenance=provenance,
        parser_name="tinymux",
    )

    inferred = infer_presentation_speaker(event, event.display_text)

    assert inferred.kind is EventKind.POSE
    assert inferred.provenance is not None
    assert inferred.provenance.sender_name == "Blood"
