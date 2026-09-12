from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from tfr.eventlog import serialize_event
from tfr.events import (
    Actor,
    ActorType,
    CommandRequest,
    Confidence,
    Direction,
    Event,
    EventKind,
    Provenance,
    outbound_audit_event,
    redact_command,
)

SESSION_ID = UUID("63f755aa-e407-4f78-ae05-f9d62c23f765")
NOW = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("connect Alice world-secret", "connect Alice [REDACTED]"),
        ('connect "Alice Smith" world secret', 'connect "Alice Smith" [REDACTED]'),
        ("create Alice world-secret", "create Alice [REDACTED]"),
        ("connect Alice world-secret\nlook", "connect Alice [REDACTED]"),
        ("@password old-secret=new-secret", "@password [REDACTED]"),
        ("@newpassword Alice=new-secret", "@newpassword [REDACTED]"),
    ],
)
def test_redacts_known_sensitive_commands(command: str, expected: str) -> None:
    result = redact_command(command)

    assert result.text == expected
    assert result.redacted is True
    assert "secret" not in result.text


def test_preserves_ordinary_commands() -> None:
    result = redact_command("look")

    assert result.text == "look"
    assert result.redacted is False


def test_redacts_explicitly_sensitive_commands() -> None:
    result = redact_command("custom-login-token", sensitive=True)

    assert result.text == "[REDACTED]"
    assert result.redacted is True


def test_outbound_audit_event_never_serializes_real_password() -> None:
    request = CommandRequest(
        session_id=SESSION_ID,
        world="example",
        actor=Actor(ActorType.STARTUP, "automatic-login"),
        text="connect Alice world-secret",
    )

    event = outbound_audit_event(
        request,
        connection_generation=1,
        sequence=2,
        timestamp=NOW,
        monotonic_ns=100,
    )
    serialized = serialize_event(event)
    payload = json.loads(serialized)

    assert "world-secret" not in serialized
    assert payload["canonical_text"] == "connect Alice [REDACTED]"
    assert payload["redacted"] is True
    assert payload["direction"] == "outbound"
    assert payload["actor"] == {"type": "startup", "id": "automatic-login"}
    assert payload["timestamp"] == "2026-09-08T12:30:00.000000Z"
    assert payload["correlation_id"] == str(request.request_id)


def test_command_request_repr_hides_text() -> None:
    request = CommandRequest(
        session_id=SESSION_ID,
        world="example",
        actor=Actor(ActorType.STARTUP, "automatic-login"),
        text="connect Alice world-secret",
        sensitive=True,
    )

    assert "world-secret" not in repr(request)


def test_event_serialization_recursively_redacts_sensitive_metadata() -> None:
    event = Event(
        session_id=SESSION_ID,
        world="example",
        connection_generation=0,
        sequence=0,
        direction=Direction.INTERNAL,
        kind=EventKind.SYSTEM,
        timestamp=NOW,
        metadata={
            "api_key": "api-secret",
            "headers": {"Authorization": "Bearer token-secret"},
            "safe": "visible",
        },
    )

    serialized = serialize_event(event)
    payload = json.loads(serialized)

    assert "api-secret" not in serialized
    assert "token-secret" not in serialized
    assert payload["metadata"]["api_key"] == "[REDACTED]"
    assert payload["metadata"]["headers"]["Authorization"] == "[REDACTED]"
    assert payload["metadata"]["safe"] == "visible"


def test_event_serializes_structured_provenance() -> None:
    event = Event(
        session_id=SESSION_ID,
        world="example",
        connection_generation=1,
        sequence=3,
        direction=Direction.INBOUND,
        kind=EventKind.CHANNEL,
        timestamp=NOW,
        canonical_text="[Alice(#12),comsys] hello\r\n",
        plain_text="[Alice(#12),comsys] hello\r\n",
        display_text="hello\r\n",
        provenance=Provenance(
            sender_name="Alice",
            sender_dbref=12,
            server_source="comsys",
            prefix_span=(0, 23),
            adapter="tinymux",
            confidence=Confidence.HIGH,
        ),
        parser_name="tinymux",
        parser_version="1",
        confidence=Confidence.HIGH,
    )

    payload = json.loads(serialize_event(event))

    assert payload["provenance"] == {
        "sender_name": "Alice",
        "sender_dbref": 12,
        "owner_name": None,
        "owner_dbref": None,
        "enactor_dbref": None,
        "server_source": "comsys",
        "prefix_span": [0, 23],
        "adapter": "tinymux",
        "confidence": "high",
    }
    assert payload["parser"] == {"name": "tinymux", "version": "1", "confidence": "high"}


def test_event_metadata_is_immutable() -> None:
    event = Event(
        session_id=SESSION_ID,
        world="example",
        connection_generation=0,
        sequence=0,
        direction=Direction.INTERNAL,
        kind=EventKind.SYSTEM,
        timestamp=NOW,
        metadata={"state": "ready", "nested": {"items": ["one"]}},
    )

    with pytest.raises(TypeError):
        event.metadata["state"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        event.metadata["nested"]["items"] = ()  # type: ignore[index]
    assert event.metadata["nested"]["items"] == ("one",)  # type: ignore[index]


def test_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Event(
            session_id=SESSION_ID,
            world="example",
            connection_generation=0,
            sequence=0,
            direction=Direction.INTERNAL,
            kind=EventKind.SYSTEM,
            timestamp=datetime(2026, 9, 8),
        )
