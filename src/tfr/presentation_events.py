from __future__ import annotations

import re
from dataclasses import replace

from tfr.ansi import terminal_plain_text
from tfr.events import Confidence, Event, EventKind, Provenance

MAX_INFERRED_SPEAKER_LENGTH = 64
_ALIEN_MULTIPAGE = re.compile(
    r"^(?P<speaker>.+?) pages \([^\r\n:]{1,200}\):",
    re.IGNORECASE,
)
_MUSH_MULTIPAGE = re.compile(
    r"^To \([^\r\n)]{1,200}\), (?P<speaker>.+?) pages:",
    re.IGNORECASE,
)


def _page_speaker(plain_text: str) -> str | None:
    for pattern in (_MUSH_MULTIPAGE, _ALIEN_MULTIPAGE):
        match = pattern.match(plain_text)
        if match is not None:
            speaker = match.group("speaker").strip()
            if speaker and len(speaker) <= MAX_INFERRED_SPEAKER_LENGTH:
                return speaker
    matches = tuple(
        re.finditer(
            r"(?:^|,\s)(?P<speaker>[^,:()\r\n]{1,64}?)\s+pages(?:\s+you)?[.:]",
            plain_text,
            re.IGNORECASE,
        )
    )
    if matches:
        speaker = matches[-1].group("speaker").strip()
        if speaker.casefold().startswith("from afar, "):
            speaker = speaker[11:].strip()
        if speaker:
            return speaker
    # PAGE classification is already stronger evidence than ordinary raw
    # output. Prefer a bounded false attribution over silently skipping a
    # configured speaker effect for an unfamiliar MU* page format.
    token = plain_text.split(maxsplit=1)[0] if plain_text else ""
    return token if token and len(token) <= MAX_INFERRED_SPEAKER_LENGTH else None


def infer_presentation_speaker(event: Event, event_text: str | None) -> Event:
    """Return a display-only event with conservative bare-output speaker attribution."""
    if event_text is None or event.provenance is not None:
        return event
    plain_text = terminal_plain_text(event_text).lstrip()
    if event.kind is EventKind.PAGE:
        speaker = _page_speaker(plain_text)
        if speaker is None:
            return event
        return replace(
            event,
            provenance=Provenance(
                sender_name=speaker,
                adapter=event.parser_name,
                confidence=Confidence.INFERRED,
            ),
            confidence=Confidence.INFERRED,
        )
    if event.kind not in {EventKind.RAW_OUTPUT, EventKind.SAY}:
        return event
    token = plain_text.split(maxsplit=1)[0] if plain_text else ""
    if token.casefold().endswith(("'s", "’s")):
        token = token[:-2]
    if not token or len(token) > MAX_INFERRED_SPEAKER_LENGTH:
        return event
    return replace(
        event,
        kind=EventKind.POSE if event.kind is EventKind.RAW_OUTPUT else event.kind,
        provenance=Provenance(
            sender_name=token,
            adapter=event.parser_name,
            confidence=Confidence.INFERRED,
        ),
        confidence=Confidence.INFERRED,
    )
