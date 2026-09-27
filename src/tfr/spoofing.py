from __future__ import annotations

import re
from collections.abc import Iterable

from tfr.events import (
    Confidence,
    EventKind,
    Provenance,
    SpoofAssessment,
    SpoofReason,
    SpoofStatus,
)

_SAY_SPEAKER = re.compile(r"^(?P<speaker>.+?)\s+says?,\s+[\"\u201c]", re.IGNORECASE)


def assess_spoofing(
    text: str,
    *,
    kind: EventKind,
    provenance: Provenance | None,
    known_senders: Iterable[str] = (),
    missing_nospoof_prefix: bool = False,
) -> SpoofAssessment | None:
    sender = _verified_sender(provenance)
    if match := _SAY_SPEAKER.match(text):
        speaker = match.group("speaker").strip()
        start, end = match.span("speaker")
        if speaker.casefold() == "you":
            return None
        if sender is not None:
            speaker_matches = speaker.casefold() == sender.casefold()
            status = (
                SpoofStatus.NOT_SPOOFED
                if speaker_matches
                else SpoofStatus.SPOOFED
            )
            return SpoofAssessment(
                status=status,
                speaker=speaker,
                speaker_span=(start, end),
                reason=(SpoofReason.SPEAKER_MISMATCH if status is SpoofStatus.SPOOFED else None),
            )
        if missing_nospoof_prefix:
            return SpoofAssessment(
                status=SpoofStatus.SPOOFED,
                speaker=speaker,
                speaker_span=(start, end),
                reason=SpoofReason.MISSING_NOSPOOF_PREFIX,
            )
        return None

    if kind is EventKind.POSE:
        apparent_sender = _known_pose_speaker(text, known_senders)
        if sender is not None and _speaker_prefix(text, sender):
            return SpoofAssessment(
                status=SpoofStatus.NOT_SPOOFED,
                speaker=text[: len(sender)],
                speaker_span=(0, len(sender)),
            )
        if sender is not None and apparent_sender is not None:
            speaker, span = apparent_sender
            return SpoofAssessment(
                status=SpoofStatus.SPOOFED,
                speaker=speaker,
                speaker_span=span,
                reason=SpoofReason.SPEAKER_MISMATCH,
            )
        if missing_nospoof_prefix and apparent_sender is not None:
            speaker, span = apparent_sender
            return SpoofAssessment(
                status=SpoofStatus.SPOOFED,
                speaker=speaker,
                speaker_span=span,
                reason=SpoofReason.MISSING_NOSPOOF_PREFIX,
            )
    return None


def _verified_sender(provenance: Provenance | None) -> str | None:
    if (
        provenance is None
        or provenance.confidence not in {Confidence.AUTHORITATIVE, Confidence.HIGH}
        or not provenance.sender_name
    ):
        return None
    return provenance.sender_name.strip() or None


def _speaker_prefix(text: str, sender: str) -> bool:
    if text[: len(sender)].casefold() != sender.casefold():
        return False
    suffix = text[len(sender) :]
    return not suffix or suffix[0].isspace() or _possessive_boundary(suffix)


def _possessive_boundary(suffix: str) -> bool:
    if len(suffix) < 2 or suffix[:2].casefold() not in {"'s", "\u2019s"}:
        return False
    return len(suffix) == 2 or suffix[2].isspace()


def _known_pose_speaker(
    text: str,
    known_senders: Iterable[str],
) -> tuple[str, tuple[int, int]] | None:
    matches = (sender for sender in known_senders if _speaker_prefix(text, sender))
    speaker = max(matches, key=len, default=None)
    if speaker is None:
        return None
    return text[: len(speaker)], (0, len(speaker))
