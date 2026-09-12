from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from tfr.events import (
    Actor,
    ActorType,
    Confidence,
    Direction,
    Event,
    EventKind,
    Provenance,
)


class ReplayError(ValueError):
    pass


def event_from_dict(value: dict[str, Any]) -> Event:
    provenance_value = value.get("provenance")
    provenance = None
    if provenance_value is not None:
        provenance = Provenance(
            sender_name=provenance_value.get("sender_name"),
            sender_dbref=provenance_value.get("sender_dbref"),
            owner_name=provenance_value.get("owner_name"),
            owner_dbref=provenance_value.get("owner_dbref"),
            enactor_dbref=provenance_value.get("enactor_dbref"),
            server_source=provenance_value.get("server_source"),
            prefix_span=(
                tuple(provenance_value["prefix_span"])
                if provenance_value.get("prefix_span") is not None
                else None
            ),
            adapter=provenance_value.get("adapter"),
            confidence=Confidence(provenance_value.get("confidence", "unknown")),
        )
    actor_value = value.get("actor")
    actor = (
        Actor(ActorType(actor_value["type"]), actor_value["id"])
        if actor_value is not None
        else None
    )
    parser = value.get("parser") or {}
    timestamp = str(value["timestamp"]).replace("Z", "+00:00")
    return Event(
        event_id=UUID(value["event_id"]),
        session_id=UUID(value["session_id"]),
        world=value["world"],
        connection_generation=value["connection_generation"],
        sequence=value["sequence"],
        timestamp=datetime.fromisoformat(timestamp),
        monotonic_ns=value.get("monotonic_ns"),
        direction=Direction(value["direction"]),
        kind=EventKind(value["kind"]),
        canonical_text=value.get("canonical_text"),
        plain_text=value.get("plain_text"),
        display_text=value.get("display_text"),
        provenance=provenance,
        parser_name=parser.get("name"),
        parser_version=parser.get("version"),
        confidence=Confidence(parser.get("confidence", "unknown")),
        actor=actor,
        correlation_id=(UUID(value["correlation_id"]) if value.get("correlation_id") else None),
        causation_id=(UUID(value["causation_id"]) if value.get("causation_id") else None),
        redacted=value.get("redacted", False),
        metadata=value.get("metadata") or {},
    )


def read_transcript(path: Path | str) -> tuple[Event, ...]:
    transcript_path = Path(path).expanduser()
    events: list[Event] = []
    try:
        with transcript_path.open(encoding="utf-8") as transcript:
            for line_number, line in enumerate(transcript, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise TypeError("event must be a JSON object")
                    events.append(event_from_dict(value))
                except (AttributeError, KeyError, TypeError, ValueError) as exc:
                    raise ReplayError(
                        f"invalid transcript event at {transcript_path}:{line_number}: {exc}"
                    ) from exc
    except (OSError, UnicodeError) as exc:
        raise ReplayError(f"cannot read transcript {transcript_path}: {exc}") from exc
    if not events:
        raise ReplayError(f"transcript contains no events: {transcript_path}")
    return tuple(events)


async def run_replay(path: Path | str) -> int:
    from tfr.config import WorldConfig, WorldDefaults
    from tfr.core import CommandBus, EventBus
    from tfr.sessions import SessionManager, WorldSession
    from tfr.tui import TfrTui

    events = read_transcript(path)
    aliases = tuple(dict.fromkeys(event.world for event in events))
    event_bus = EventBus()
    command_bus = CommandBus()
    sessions = [
        WorldSession(
            world=alias,
            config=WorldConfig(
                host="replay.invalid",
                port=1,
                reconnect=False,
                autoconnect=False,
            ),
            defaults=WorldDefaults(),
            event_bus=event_bus,
            command_bus=command_bus,
        )
        for alias in aliases
    ]
    tui = TfrTui(
        sessions=sessions,
        manager=SessionManager(sessions),
        event_bus=event_bus,
        command_bus=command_bus,
        scrollback_lines={alias: 20_000 for alias in aliases},
        agent_worlds={
            event.world
            for event in events
            if event.actor is not None and event.actor.type is ActorType.AGENT
        },
        pager_enabled=True,
        pager_overlap=1,
        initial_events=events,
        initial_scroll_to_end=False,
        replay_mode=True,
    )
    try:
        return await tui.run()
    finally:
        await event_bus.close()
