from __future__ import annotations

from dataclasses import replace

from tfr.ansi import terminal_plain_text
from tfr.events import Confidence, Event, EventKind, Provenance

MAX_INFERRED_SPEAKER_LENGTH = 64


def infer_presentation_speaker(event: Event, event_text: str | None) -> Event:
    """Return a display-only event with conservative bare-output speaker attribution."""
    if (
        event_text is None
        or event.provenance is not None
        or event.kind not in {EventKind.RAW_OUTPUT, EventKind.SAY}
    ):
        return event
    plain_text = terminal_plain_text(event_text).lstrip()
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
