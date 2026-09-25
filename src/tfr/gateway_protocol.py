from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from tfr.eventlog import event_to_dict
from tfr.events import Event
from tfr.replay import event_from_dict

PROTOCOL_VERSION = 2
MAX_MESSAGE_BYTES = 1_048_576
MAX_SNAPSHOT_EVENTS = 1_000_000
WRITE_TIMEOUT_SECONDS = 10


class GatewayProtocolError(ValueError):
    pass


def encode_message(message: Mapping[str, Any]) -> bytes:
    value = dict(message)
    value.setdefault("protocol", PROTOCOL_VERSION)
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GatewayProtocolError(f"message is not JSON serializable: {exc}") from exc
    if len(payload) + 1 > MAX_MESSAGE_BYTES:
        raise GatewayProtocolError("message exceeds the maximum size")
    return payload + b"\n"


async def read_message(
    reader: asyncio.StreamReader,
    *,
    maximum_bytes: int = MAX_MESSAGE_BYTES,
) -> dict[str, Any] | None:
    if not 1 <= maximum_bytes <= MAX_MESSAGE_BYTES:
        raise ValueError("message read limit is outside the protocol bounds")
    try:
        payload = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise GatewayProtocolError("connection ended during a message") from exc
    except asyncio.LimitOverrunError as exc:
        raise GatewayProtocolError("message exceeds the maximum size") from exc
    if len(payload) > maximum_bytes:
        raise GatewayProtocolError("message exceeds the maximum size")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayProtocolError(f"invalid JSON message: {exc}") from exc
    if not isinstance(value, dict):
        raise GatewayProtocolError("message must be a JSON object")
    if value.get("protocol") != PROTOCOL_VERSION:
        raise GatewayProtocolError(f"unsupported protocol version: {value.get('protocol')!r}")
    if not isinstance(value.get("type"), str):
        raise GatewayProtocolError("message type must be a string")
    return value


async def write_message(
    writer: asyncio.StreamWriter,
    message: Mapping[str, Any],
    *,
    lock: asyncio.Lock | None = None,
) -> None:
    payload = encode_message(message)
    if lock is None:
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), timeout=WRITE_TIMEOUT_SECONDS)
        return
    async with lock:
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), timeout=WRITE_TIMEOUT_SECONDS)


def event_message(cursor: int, event: Event) -> dict[str, Any]:
    if cursor < 1:
        raise GatewayProtocolError("event cursor must be positive")
    return {
        "type": "event",
        "protocol": PROTOCOL_VERSION,
        "cursor": cursor,
        "event": event_to_dict(event),
    }


def event_from_message(message: Mapping[str, Any]) -> tuple[int, Event]:
    if message.get("type") != "event":
        raise GatewayProtocolError("expected an event message")
    if message.get("protocol") != PROTOCOL_VERSION:
        raise GatewayProtocolError(f"unsupported protocol version: {message.get('protocol')!r}")
    cursor = message.get("cursor")
    value = message.get("event")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 1:
        raise GatewayProtocolError("event cursor must be a positive integer")
    if not isinstance(value, dict):
        raise GatewayProtocolError("event payload must be an object")
    try:
        return cursor, event_from_dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise GatewayProtocolError(f"invalid event payload: {exc}") from exc
