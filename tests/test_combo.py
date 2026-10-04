from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from tfr.combo import ComboTracker
from tfr.events import Confidence, Direction, Event, EventKind, Provenance


def event(text: str, *, speaker: str = "Alice", kind: EventKind = EventKind.SAY) -> Event:
    return Event(
        session_id=uuid4(),
        world="alpha",
        connection_generation=1,
        sequence=0,
        direction=Direction.INBOUND,
        kind=kind,
        timestamp=datetime.now(UTC),
        canonical_text=text,
        plain_text=text,
        display_text=text,
        provenance=Provenance(sender_name=speaker, confidence=Confidence.HIGH),
    )


def test_combo_starts_at_three_and_uses_five_minute_rolling_timeout() -> None:
    tracker = ComboTracker()

    assert tracker.observe(event('Alice says, "one"'), 'Alice says, "one"', 0) is None
    assert (
        tracker.observe(
            event("Alice poses two", kind=EventKind.POSE), "Alice poses two", 299
        )
        is None
    )
    combo = tracker.observe(event('Alice says, "three"'), 'Alice says, "three"', 598)

    assert combo is not None
    assert combo.count == 3
    assert combo.notice == "> Speaking Spree! <"
    assert 'Alice says, "three"'[combo.body_start : combo.body_end] == "three"

    assert tracker.observe(event('Alice says, "reset"'), 'Alice says, "reset"', 899) is None


def test_different_speaker_breaks_combo_and_godlike_multiplier_continues() -> None:
    tracker = ComboTracker()
    for count in range(1, 9):
        combo = tracker.observe(event(f'Alice says, "{count}"'), f'Alice says, "{count}"', count)

    assert combo is not None
    assert combo.notice == "> GODLIKE x2 <"
    assert tracker.observe(
        event('Bob says, "interrupts"', speaker="Bob"),
        'Bob says, "interrupts"',
        10,
    ) is None


def test_possessive_pose_excludes_speaker_from_effect_span() -> None:
    tracker = ComboTracker()
    pose = event("Alice's cigarette goes out", kind=EventKind.POSE)
    for now in range(3):
        combo = tracker.observe(pose, pose.display_text or "", now)

    assert combo is not None
    assert (pose.display_text or "")[combo.body_start : combo.body_end] == "cigarette goes out"
