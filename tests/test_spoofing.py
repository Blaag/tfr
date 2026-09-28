from __future__ import annotations

from tfr.events import Confidence, EventKind, Provenance, SpoofReason, SpoofStatus
from tfr.spoofing import assess_spoofing


def provenance(sender: str) -> Provenance:
    return Provenance(sender_name=sender, confidence=Confidence.HIGH)


def test_verified_say_speaker_matches_nospoof_sender() -> None:
    assessment = assess_spoofing(
        'Black2 says, "Hello"\r\n',
        kind=EventKind.SAY,
        provenance=provenance("Black2"),
        known_senders=("Black2",),
    )

    assert assessment is not None
    assert assessment.status is SpoofStatus.NOT_SPOOFED
    assert assessment.speaker == "Black2"
    assert assessment.speaker_span == (0, 6)


def test_you_say_remains_undetermined_without_verified_local_dbref() -> None:
    assessment = assess_spoofing(
        'You say, "Hello"\r\n',
        kind=EventKind.SAY,
        provenance=provenance("Black2"),
        known_senders=("Black2",),
    )

    assert assessment is None
    assert (
        assess_spoofing(
            'You say, "Hello"\r\n',
            kind=EventKind.SAY,
            provenance=None,
            missing_nospoof_prefix=True,
        )
        is None
    )


def test_mismatched_and_unprefixed_says_are_spoofed() -> None:
    mismatch = assess_spoofing(
        'Bob says, "I like grapes!"\r\n',
        kind=EventKind.SAY,
        provenance=provenance("Black2"),
        known_senders=("Black2", "Bob"),
    )
    injected = assess_spoofing(
        'Bob says, "I like grapes!"\r\n',
        kind=EventKind.SAY,
        provenance=None,
        known_senders=("Black2", "Bob"),
        missing_nospoof_prefix=True,
    )

    assert mismatch is not None and mismatch.status is SpoofStatus.SPOOFED
    assert mismatch.reason is SpoofReason.SPEAKER_MISMATCH
    assert injected is not None and injected.status is SpoofStatus.SPOOFED
    assert injected.reason is SpoofReason.MISSING_NOSPOOF_PREFIX
    assert injected.suspected_sender is None
    assert injected.attribution_confidence is None
    assert injected.speaker_span == (0, 3)


def test_unprefixed_speech_requires_a_correlated_prefixed_sender() -> None:
    pose = assess_spoofing(
        "Bob waves.\r\n",
        kind=EventKind.POSE,
        provenance=None,
        known_senders=("Bob",),
    )
    correlated_pose = assess_spoofing(
        "Bob waves.\r\n",
        kind=EventKind.POSE,
        provenance=None,
        known_senders=("Bob",),
        missing_nospoof_prefix=True,
    )
    ordinary = assess_spoofing(
        "The room shakes.\r\n",
        kind=EventKind.RAW_OUTPUT,
        provenance=None,
        known_senders=("Bob",),
    )

    assert pose is None
    assert correlated_pose is not None
    assert correlated_pose.status is SpoofStatus.SPOOFED
    assert correlated_pose.speaker_span == (0, 3)
    assert ordinary is None


def test_verified_poses_require_a_complete_sender_name() -> None:
    matching = assess_spoofing(
        "Al waves.\r\n",
        kind=EventKind.POSE,
        provenance=provenance("Al"),
        known_senders=("Al", "Alice"),
    )
    mismatched = assess_spoofing(
        "Alice waves.\r\n",
        kind=EventKind.POSE,
        provenance=provenance("Al"),
        known_senders=("Al", "Alice"),
    )

    assert matching is not None and matching.status is SpoofStatus.NOT_SPOOFED
    assert mismatched is not None and mismatched.status is SpoofStatus.SPOOFED
    assert mismatched.speaker == "Alice"
    assert mismatched.reason is SpoofReason.SPEAKER_MISMATCH


def test_pose_apostrophe_boundary_requires_a_complete_possessive() -> None:
    for text in ("Al'ice waves.", "Al’ice waves."):
        assessment = assess_spoofing(
            text,
            kind=EventKind.POSE,
            provenance=provenance("Al"),
            known_senders=("Al",),
        )
        assert assessment is None

    possessive = assess_spoofing(
        "Al's hat falls.",
        kind=EventKind.POSE,
        provenance=provenance("Al"),
        known_senders=("Al",),
    )
    assert possessive is not None
    assert possessive.status is SpoofStatus.NOT_SPOOFED


def test_standalone_unprefixed_say_is_not_flagged() -> None:
    assert (
        assess_spoofing(
            'Bob says, "Hello"',
            kind=EventKind.SAY,
            provenance=None,
            known_senders=(),
        )
        is None
    )
