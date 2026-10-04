from __future__ import annotations

import re
from dataclasses import dataclass

from tfr.ansi import terminal_plain_text
from tfr.events import Event, EventKind

COMBO_TIMEOUT_SECONDS = 300.0

_SAY_BODY = re.compile(r'^.*?\bsays?,\s+["“](?P<body>.*?)["”]?$', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Combo:
    count: int
    speaker: str
    body_start: int
    body_end: int

    @property
    def multiplier(self) -> int:
        return max(1, self.count - 6)

    @property
    def notice(self) -> str:
        if self.count == 3:
            return "> Speaking Spree! <"
        if self.count == 4:
            return "> Rampage! <"
        if self.count == 5:
            return "> Dominating! <"
        if self.count == 6:
            return "> Unstoppable! <"
        if self.count == 7:
            return "> GODLIKE! <"
        return f"> GODLIKE x{self.multiplier} <"

    @property
    def color(self) -> str:
        return {
            3: "#ffffff",
            4: "#1eff00",
            5: "#0070dd",
            6: "#a335ee",
        }.get(self.count, "#ff8000")


@dataclass(slots=True)
class _Streak:
    speaker: str
    identity: tuple[str, str | int]
    count: int
    last_at: float


class ComboTracker:
    def __init__(self, timeout_seconds: float = COMBO_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self._streaks: dict[str, _Streak] = {}

    def observe(
        self,
        event: Event,
        text: str,
        now: float,
        *,
        character_name: str | None = None,
    ) -> Combo | None:
        speaker = _speaker(event, text, character_name)
        if speaker is None or event.kind not in {
            EventKind.SAY,
            EventKind.POSE,
            EventKind.SPEECH,
            EventKind.RAW_OUTPUT,
        }:
            return None
        span = message_body_span(event, text, speaker)
        if span is None or span[0] >= span[1]:
            return None
        identity = (
            ("dbref", event.provenance.sender_dbref)
            if event.provenance is not None and event.provenance.sender_dbref is not None
            else ("name", speaker.casefold())
        )
        previous = self._streaks.get(event.world)
        count = (
            previous.count + 1
            if previous is not None
            and previous.identity == identity
            and now - previous.last_at <= self.timeout_seconds
            else 1
        )
        self._streaks[event.world] = _Streak(speaker, identity, count, now)
        return Combo(count, speaker, *span) if count >= 3 else None


def _speaker(event: Event, text: str, character_name: str | None) -> str | None:
    if event.provenance is not None and event.provenance.sender_name:
        return event.provenance.sender_name
    plain = terminal_plain_text(text).lstrip()
    say = _SAY_BODY.match(plain)
    if say is not None:
        prefix = plain[: say.start("body")]
        visible = re.sub(r"\s+says?,\s+[\"“]$", "", prefix, flags=re.IGNORECASE).strip()
        if visible.casefold() == "you" and character_name is not None:
            return character_name
        return visible or None
    if character_name is not None and _starts_with_speaker(plain, character_name):
        return character_name
    if event.parser_name not in {"bare", "generic"}:
        return None
    token = plain.split(maxsplit=1)[0] if plain else ""
    if token.casefold().endswith(("'s", "’s")):
        token = token[:-2]
    return token or None


def _starts_with_speaker(text: str, speaker: str) -> bool:
    if text[: len(speaker)].casefold() != speaker.casefold():
        return False
    suffix = text[len(speaker) :]
    return bool(suffix and (suffix[0].isspace() or suffix[:2].casefold() in {"'s", "’s"}))


def message_body_span(event: Event, text: str, speaker: str) -> tuple[int, int] | None:
    plain = terminal_plain_text(text).rstrip("\r\n")
    if event.kind in {EventKind.SAY, EventKind.SPEECH}:
        match = _SAY_BODY.match(plain)
        if match is not None:
            return match.start("body"), match.end("body")
    prefix = speaker
    if plain[: len(prefix)].casefold() != prefix.casefold():
        return None
    start = len(prefix)
    if plain[start : start + 2].casefold() in {"'s", "’s"}:
        start += 2
    while start < len(plain) and plain[start].isspace():
        start += 1
    return (start, len(plain)) if start < len(plain) else None
