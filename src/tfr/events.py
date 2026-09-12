from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


class EventKind(StrEnum):
    RAW_OUTPUT = "raw_output"
    COMMAND = "command"
    SPEECH = "speech"
    SAY = "say"
    POSE = "pose"
    PAGE = "page"
    CHANNEL = "channel"
    EMIT = "emit"
    SYSTEM = "system"
    CONNECTION = "connection"
    TELNET = "telnet"
    AGENT_REQUEST = "agent_request"
    AGENT_RESPONSE = "agent_response"
    AGENT_ACTION = "agent_action"
    PLUGIN = "plugin"


class ActorType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    PLUGIN = "plugin"
    STARTUP = "startup"
    IDLE = "idle"
    SYSTEM = "system"


class Confidence(StrEnum):
    AUTHORITATIVE = "authoritative"
    HIGH = "high"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class Actor:
    type: ActorType
    id: str

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("actor id cannot be empty")


@dataclass(frozen=True, slots=True)
class Provenance:
    sender_name: str | None = None
    sender_dbref: int | None = None
    owner_name: str | None = None
    owner_dbref: int | None = None
    enactor_dbref: int | None = None
    server_source: str | None = None
    prefix_span: tuple[int, int] | None = None
    adapter: str | None = None
    confidence: Confidence = Confidence.UNKNOWN

    def __post_init__(self) -> None:
        if self.prefix_span is not None:
            start, end = self.prefix_span
            if start < 0 or end < start:
                raise ValueError("prefix span must be an ordered non-negative range")


@dataclass(frozen=True, slots=True)
class Event:
    session_id: UUID
    world: str
    connection_generation: int
    sequence: int
    direction: Direction
    kind: EventKind
    event_id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    monotonic_ns: int | None = None
    canonical_text: str | None = None
    plain_text: str | None = None
    display_text: str | None = None
    provenance: Provenance | None = None
    parser_name: str | None = None
    parser_version: str | None = None
    confidence: Confidence = Confidence.UNKNOWN
    actor: Actor | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    redacted: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.world:
            raise ValueError("world cannot be empty")
        if self.connection_generation < 0:
            raise ValueError("connection generation cannot be negative")
        if self.sequence < 0:
            raise ValueError("sequence cannot be negative")
        if self.monotonic_ns is not None and self.monotonic_ns < 0:
            raise ValueError("monotonic time cannot be negative")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True, slots=True)
class CommandRequest:
    session_id: UUID
    world: str
    actor: Actor
    text: str = field(repr=False)
    request_id: UUID = field(default_factory=uuid4)
    sensitive: bool = False
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.world:
            raise ValueError("world cannot be empty")
        if not self.text:
            raise ValueError("command text cannot be empty")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True, slots=True)
class RedactedText:
    text: str
    redacted: bool


_LOGIN_COMMAND = re.compile(
    r"^(?P<prefix>\s*(?:connect|co|create)\s+(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\S+)\s+).+$",
    re.IGNORECASE | re.DOTALL,
)
_PASSWORD_COMMAND = re.compile(
    r"^\s*(?P<command>@(?:password|newpassword))(?:\s+|=)", re.IGNORECASE
)


def redact_command(text: str, *, sensitive: bool = False) -> RedactedText:
    login_match = _LOGIN_COMMAND.match(text)
    if login_match:
        return RedactedText(f"{login_match.group('prefix')}[REDACTED]", True)
    password_match = _PASSWORD_COMMAND.match(text)
    if password_match:
        return RedactedText(f"{password_match.group('command')} [REDACTED]", True)
    if sensitive:
        return RedactedText("[REDACTED]", True)
    return RedactedText(text, False)


def outbound_audit_event(
    request: CommandRequest,
    *,
    connection_generation: int,
    sequence: int,
    timestamp: datetime | None = None,
    monotonic_ns: int | None = None,
) -> Event:
    audit_text = redact_command(request.text, sensitive=request.sensitive)
    return Event(
        session_id=request.session_id,
        world=request.world,
        connection_generation=connection_generation,
        sequence=sequence,
        direction=Direction.OUTBOUND,
        kind=EventKind.COMMAND,
        timestamp=timestamp or datetime.now(UTC),
        monotonic_ns=monotonic_ns,
        canonical_text=audit_text.text,
        plain_text=audit_text.text,
        display_text=audit_text.text,
        actor=request.actor,
        correlation_id=request.correlation_id or request.request_id,
        causation_id=request.causation_id,
        redacted=audit_text.redacted,
        metadata=request.metadata,
    )
