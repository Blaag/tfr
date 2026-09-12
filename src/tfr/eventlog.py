from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, TextIO
from uuid import UUID

from tfr.events import Event


class EventSink(Protocol):
    async def write(self, event: Event) -> None: ...

    async def close(self) -> None: ...


def _timestamp(value: datetime) -> str:
    utc_value = value.astimezone(UTC)
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "password",
    "passwd",
    "proxy_authorization",
    "refresh_token",
    "secret",
    "token",
}


def _sensitive_key(key: object) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )


def _json_value(value: Any, *, key: object | None = None) -> Any:
    if key is not None and _sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(item_key): _json_value(item, key=item_key) for item_key, item in value.items()}
    if hasattr(value, "items"):
        return {str(item_key): _json_value(item, key=item_key) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def event_to_dict(event: Event) -> dict[str, Any]:
    provenance = None
    if event.provenance is not None:
        provenance = {
            "sender_name": event.provenance.sender_name,
            "sender_dbref": event.provenance.sender_dbref,
            "owner_name": event.provenance.owner_name,
            "owner_dbref": event.provenance.owner_dbref,
            "enactor_dbref": event.provenance.enactor_dbref,
            "server_source": event.provenance.server_source,
            "prefix_span": event.provenance.prefix_span,
            "adapter": event.provenance.adapter,
            "confidence": event.provenance.confidence,
        }

    actor = None
    if event.actor is not None:
        actor = {"type": event.actor.type, "id": event.actor.id}

    return _json_value(
        {
            "event_id": event.event_id,
            "session_id": event.session_id,
            "world": event.world,
            "connection_generation": event.connection_generation,
            "sequence": event.sequence,
            "timestamp": event.timestamp,
            "monotonic_ns": event.monotonic_ns,
            "direction": event.direction,
            "kind": event.kind,
            "canonical_text": event.canonical_text,
            "plain_text": event.plain_text,
            "display_text": event.display_text,
            "provenance": provenance,
            "parser": {
                "name": event.parser_name,
                "version": event.parser_version,
                "confidence": event.confidence,
            },
            "actor": actor,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
            "redacted": event.redacted,
            "metadata": event.metadata,
        }
    )


def serialize_event(event: Event) -> str:
    return json.dumps(event_to_dict(event), ensure_ascii=False, separators=(",", ":"))


class JsonlEventSink:
    def __init__(self, path: Path | str, *, flush: bool = True) -> None:
        self.path = Path(path).expanduser()
        self.flush = flush
        self._file: TextIO | None = None
        self._lock = asyncio.Lock()

    def _open(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        self._file = os.fdopen(descriptor, "a", encoding="utf-8")

    def _write_line(self, line: str) -> None:
        self._open()
        assert self._file is not None
        self._file.write(line)
        self._file.write("\n")
        if self.flush:
            self._file.flush()

    async def write(self, event: Event) -> None:
        line = serialize_event(event)
        async with self._lock:
            await asyncio.to_thread(self._write_line, line)

    def _close(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._close)

    async def __aenter__(self) -> JsonlEventSink:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()
