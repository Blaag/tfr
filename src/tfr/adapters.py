from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from tfr.ansi import AnsiProjection, project_ansi
from tfr.events import Confidence, EventKind, Provenance

_PARSER_VERSION = "1"

_TINYMUX_FULL = re.compile(
    r"^\["
    r"(?P<sender>.*?)\(#(?P<sender_dbref>\d+)\)"
    r"(?:\{(?P<owner>.*?)\})?"
    r"(?:<-\(#(?P<enactor_dbref>\d+)\))?"
    r"(?:,(?P<source>comsys|kill|give|page|saypose))?"
    r"\] "
)
_TINYMUX_TERSE = re.compile(r"^\[#(?P<sender_dbref>\d+)\] ")
_RHOST_FULL = re.compile(
    r"^\["
    r"(?P<sender>.*?)\(#(?P<sender_dbref>\d+)\)"
    r"(?:\{(?P<owner>.*?)\})?"
    r"(?:<-\(#(?P<enactor_dbref>\d+)\))?"
    r"\] "
)

_SAY = re.compile(r"(?:^|\s)says?,\s+[\"\u201c]", re.IGNORECASE)
_OWN_SAY = re.compile(r"^You say,\s+[\"\u201c]", re.IGNORECASE)
_PAGE = re.compile(
    r"^(?:From afar(?:, to .*?)?, .+ pages(?: you)?[.:]|.+ pages:|You paged |Long distance to )",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Classification:
    kind: EventKind
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class ParsedInbound:
    canonical_text: str
    plain_text: str
    display_text: str
    message_text: str
    kind: EventKind
    provenance: Provenance | None
    parser_name: str
    parser_version: str
    confidence: Confidence


class ServerAdapter(Protocol):
    name: str

    def parse(self, text: str, *, show_prefix: bool = False) -> ParsedInbound: ...


def classify_message(
    message: str,
    *,
    source: str | None = None,
    sender_name: str | None = None,
) -> Classification:
    visible = message.rstrip("\r\n")
    if source == "page":
        return Classification(EventKind.PAGE, Confidence.HIGH)
    if source == "comsys":
        return Classification(EventKind.CHANNEL, Confidence.HIGH)
    if source in {"kill", "give"}:
        return Classification(EventKind.SYSTEM, Confidence.HIGH)
    if source == "saypose":
        if _SAY.search(visible) or _OWN_SAY.search(visible):
            return Classification(EventKind.SAY, Confidence.INFERRED)
        if sender_name and visible.startswith(sender_name):
            return Classification(EventKind.POSE, Confidence.INFERRED)
        return Classification(EventKind.SPEECH, Confidence.HIGH)

    if _PAGE.search(visible):
        return Classification(EventKind.PAGE, Confidence.INFERRED)
    if _SAY.search(visible) or _OWN_SAY.search(visible):
        return Classification(EventKind.SAY, Confidence.INFERRED)
    if sender_name and visible.startswith(sender_name):
        return Classification(EventKind.POSE, Confidence.INFERRED)
    return Classification(EventKind.RAW_OUTPUT, Confidence.UNKNOWN)


def _parsed(
    *,
    adapter: str,
    text: str,
    projection: AnsiProjection,
    prefix_end: int | None = None,
    sender_name: str | None = None,
    sender_dbref: int | None = None,
    owner_name: str | None = None,
    enactor_dbref: int | None = None,
    source: str | None = None,
    show_prefix: bool = False,
) -> ParsedInbound:
    message = projection.plain[prefix_end:] if prefix_end is not None else projection.plain
    classification = classify_message(message, source=source, sender_name=sender_name)
    provenance = None
    if prefix_end is not None:
        provenance = Provenance(
            sender_name=sender_name,
            sender_dbref=sender_dbref,
            owner_name=owner_name,
            enactor_dbref=enactor_dbref,
            server_source=source,
            prefix_span=(0, projection.raw_boundary(prefix_end)),
            adapter=adapter,
            confidence=Confidence.HIGH,
        )
    display = (
        text if show_prefix or prefix_end is None else projection.remove_visible_prefix(prefix_end)
    )
    confidence = classification.confidence
    if provenance is not None and classification.kind is EventKind.RAW_OUTPUT:
        confidence = Confidence.HIGH
    return ParsedInbound(
        canonical_text=text,
        plain_text=projection.plain,
        display_text=display,
        message_text=message,
        kind=classification.kind,
        provenance=provenance,
        parser_name=adapter,
        parser_version=_PARSER_VERSION,
        confidence=confidence,
    )


class GenericAdapter:
    name = "generic"

    def parse(self, text: str, *, show_prefix: bool = False) -> ParsedInbound:
        del show_prefix
        projection = project_ansi(text)
        return _parsed(adapter=self.name, text=text, projection=projection)


class BareAdapter(GenericAdapter):
    name = "bare"


class TinyMuxAdapter:
    name = "tinymux"

    def parse(self, text: str, *, show_prefix: bool = False) -> ParsedInbound:
        projection = project_ansi(text)
        match = _TINYMUX_FULL.match(projection.plain)
        if match is not None:
            return _parsed(
                adapter=self.name,
                text=text,
                projection=projection,
                prefix_end=match.end(),
                sender_name=match.group("sender"),
                sender_dbref=int(match.group("sender_dbref")),
                owner_name=match.group("owner"),
                enactor_dbref=(
                    int(match.group("enactor_dbref"))
                    if match.group("enactor_dbref") is not None
                    else None
                ),
                source=match.group("source"),
                show_prefix=show_prefix,
            )

        match = _TINYMUX_TERSE.match(projection.plain)
        if match is not None:
            return _parsed(
                adapter=self.name,
                text=text,
                projection=projection,
                prefix_end=match.end(),
                sender_dbref=int(match.group("sender_dbref")),
                show_prefix=show_prefix,
            )
        return _parsed(adapter=self.name, text=text, projection=projection)


class RhostAdapter:
    name = "rhost"

    def parse(self, text: str, *, show_prefix: bool = False) -> ParsedInbound:
        projection = project_ansi(text)
        match = _RHOST_FULL.match(projection.plain)
        if match is None:
            return _parsed(adapter=self.name, text=text, projection=projection)
        return _parsed(
            adapter=self.name,
            text=text,
            projection=projection,
            prefix_end=match.end(),
            sender_name=match.group("sender"),
            sender_dbref=int(match.group("sender_dbref")),
            owner_name=match.group("owner"),
            enactor_dbref=(
                int(match.group("enactor_dbref"))
                if match.group("enactor_dbref") is not None
                else None
            ),
            show_prefix=show_prefix,
        )


class TinyMushAdapter(RhostAdapter):
    name = "tinymush"


def adapter_for(server: str) -> ServerAdapter:
    if server == "bare":
        return BareAdapter()
    if server == "tinymux":
        return TinyMuxAdapter()
    if server == "tinymush":
        return TinyMushAdapter()
    if server == "rhost":
        return RhostAdapter()
    return GenericAdapter()
