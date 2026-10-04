from __future__ import annotations

import random
from datetime import UTC, datetime
from uuid import uuid4

import pytest

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


def test_empty_speech_never_starts_or_advances_a_combo() -> None:
    tracker = ComboTracker()
    for now in range(10):
        assert tracker.observe(event('Alice says, ""'), 'Alice says, ""', now) is None

    assert tracker.observe(event('Alice says, "one"'), 'Alice says, "one"', 11) is None
    assert tracker.observe(event('Alice says, "two"'), 'Alice says, "two"', 12) is None
    assert tracker.observe(event('Alice says, "three"'), 'Alice says, "three"', 13) is not None


@pytest.mark.parametrize("target", range(3, 8))
@pytest.mark.parametrize("seed", range(8))
def test_deterministic_mixed_say_pose_chains_reach_every_combo_level(
    target: int,
    seed: int,
) -> None:
    generator = random.Random(seed)
    tracker = ComboTracker()
    combo = None
    for count in range(1, target + 1):
        if generator.choice((True, False)):
            text = f'Alice says, "message {count}"'
            item = event(text)
        else:
            possessive = generator.choice(("", "'s", "’s"))
            text = f"Alice{possessive} poses message {count}"
            item = event(text, kind=EventKind.POSE)
        combo = tracker.observe(item, text, count)
        assert (combo is not None) is (count >= 3), f"seed={seed}, count={count}"
        if combo is not None:
            assert combo.count == count

    assert combo is not None
    assert combo.count == target
