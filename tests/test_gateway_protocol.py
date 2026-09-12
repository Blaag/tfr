from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from tfr.events import Direction, Event, EventKind
from tfr.gateway_protocol import (
    MAX_MESSAGE_BYTES,
    GatewayProtocolError,
    encode_message,
    event_from_message,
    event_message,
    read_message,
)


def make_event() -> Event:
    return Event(
        event_id=UUID("b0ad698c-f58c-48c8-9c67-7c7c8951d188"),
        session_id=UUID("63f755aa-e407-4f78-ae05-f9d62c23f765"),
        world="alpha",
        connection_generation=2,
        sequence=3,
        timestamp=datetime(2026, 9, 9, 12, tzinfo=UTC),
        direction=Direction.INBOUND,
        kind=EventKind.SAY,
        canonical_text='Alice says, "Hello"',
        plain_text='Alice says, "Hello"',
        display_text='Alice says, "Hello"',
    )


async def test_reads_and_versions_json_line_messages() -> None:
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    reader.feed_data(encode_message({"type": "hello", "client_id": "example"}))
    reader.feed_eof()

    message = await read_message(reader)

    assert message == {"type": "hello", "client_id": "example", "protocol": 1}
    assert await read_message(reader) is None


def test_event_message_round_trips() -> None:
    original = make_event()

    cursor, restored = event_from_message(json.loads(encode_message(event_message(7, original))))

    assert cursor == 7
    assert restored == original


async def test_rejects_an_unsupported_protocol_version() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"type":"hello","protocol":99}\n')

    with pytest.raises(GatewayProtocolError, match="unsupported protocol"):
        await read_message(reader)


def test_rejects_an_oversized_message() -> None:
    with pytest.raises(GatewayProtocolError, match="maximum size"):
        encode_message({"type": "command", "text": "x" * MAX_MESSAGE_BYTES})
