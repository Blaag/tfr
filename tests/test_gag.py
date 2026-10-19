from __future__ import annotations

import time
from uuid import uuid4

from tfr.events import Direction, Event, EventKind
from tfr.gag import MAX_GAG_INPUT_CHARACTERS, GagPolicy


def event(text: str, *, direction: Direction = Direction.INBOUND) -> Event:
    return Event(
        session_id=uuid4(),
        world="alpha",
        connection_generation=1,
        sequence=1,
        direction=direction,
        kind=EventKind.RAW_OUTPUT,
        canonical_text=text,
        plain_text=text,
        display_text=text,
    )


def test_case_insensitive_regex_matches_visible_world_text() -> None:
    policy = GagPolicy({"alpha": (r"^bing\b",)})

    assert policy.allows(event("BING goes the bell")) is False
    assert policy.allows(event("a bing later")) is True
    assert policy.allows(event("BING goes the bell", direction=Direction.OUTBOUND)) is True


def test_matching_input_is_bounded() -> None:
    policy = GagPolicy({"alpha": ("hidden beyond bound",)})
    text = "x" * MAX_GAG_INPUT_CHARACTERS + "hidden beyond bound"

    assert policy.allows(event(text)) is True


def test_catastrophic_pattern_times_out_fails_open_and_is_quarantined() -> None:
    diagnostics: list[tuple[str, str, int]] = []
    policy = GagPolicy(
        {"alpha": (r"^(a|aa)+$",)},
        diagnostic=lambda code, world, index: diagnostics.append((code, world, index)),
    )
    hostile = [event("a" * 16_000 + "!") for _ in range(3)]

    started = time.monotonic()
    assert all(policy.allows(item) is True for item in hostile)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert policy.quarantined_patterns == frozenset({("alpha", 0)})
    assert diagnostics == [
        ("match_timeout", "alpha", 0),
        ("pattern_quarantined", "alpha", 0),
    ]
    assert "a" * 100 not in repr(diagnostics)


def test_policy_decision_is_cached_per_canonical_event() -> None:
    diagnostics: list[tuple[str, str, int]] = []
    policy = GagPolicy(
        {"alpha": (r"^(a|aa)+$",)},
        diagnostic=lambda code, world, index: diagnostics.append((code, world, index)),
    )
    hostile = event("a" * 16_000 + "!")

    assert policy.allows(hostile) is True
    assert policy.allows(hostile) is True
    assert policy.allows(hostile) is True

    assert diagnostics == [("match_timeout", "alpha", 0)]
    assert policy.quarantined_patterns == frozenset()
