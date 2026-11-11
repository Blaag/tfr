from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from tfr.events import Confidence, Direction, Event, EventKind, Provenance
from tfr.recall_store import RecallStore, RecallWorldPolicy


def recall_event(text: str, *, speaker: str, seconds: int = 0) -> Event:
    return Event(
        event_id=uuid4(),
        session_id=UUID("11111111-1111-4111-8111-111111111111"),
        world="alpha",
        connection_generation=1,
        sequence=seconds,
        timestamp=datetime(2026, 11, 11, 12, tzinfo=UTC) + timedelta(seconds=seconds),
        direction=Direction.INBOUND,
        kind=EventKind.SAY,
        canonical_text=text,
        plain_text=text,
        display_text=text,
        provenance=Provenance(sender_name=speaker, confidence=Confidence.HIGH),
    )


async def test_recall_store_matches_whole_words_wildcards_regex_and_context(tmp_path) -> None:
    store = RecallStore(
        tmp_path / "recall.sqlite3",
        worlds={"alpha": RecallWorldPolicy(enabled=True, retention_days=None)},
        log_directory=tmp_path / "logs",
    )
    await store.start()
    try:
        await store.write(recall_event("Alice arrives", speaker="Alice", seconds=1))
        await store.write(recall_event("A red dragon appears", speaker="Bob", seconds=2))
        await store.write(recall_event("dragonfly", speaker="Bob", seconds=3))
        await store.write(recall_event("Alice leaves", speaker="Alice", seconds=4))

        whole = await store.query({"words": "dragon", "context": 1})
        assert whole["matches"] == 1
        assert [event["text"] for event in whole["events"]] == [
            "Alice arrives",
            "A red dragon appears",
            "dragonfly",
        ]
        wildcard = await store.query({"words": "dragon*"})
        assert wildcard["matches"] == 2
        regexp = await store.query({"regexp": r"(?i)^alice", "speaker": "Alice"})
        assert regexp["matches"] == 2
    finally:
        await store.close()


async def test_recall_store_respects_world_opt_in_and_retention(tmp_path) -> None:
    store = RecallStore(
        tmp_path / "recall.sqlite3",
        worlds={
            "alpha": RecallWorldPolicy(enabled=False, retention_days=None),
            "beta": RecallWorldPolicy(enabled=True, retention_days=30),
        },
        log_directory=tmp_path / "logs",
    )
    await store.start()
    try:
        await store.write(recall_event("not indexed", speaker="Alice"))
        inserted = store._insert_values(
            [
                {
                    "event_id": str(uuid4()),
                    "world": "beta",
                    "timestamp": (datetime.now(UTC) - timedelta(days=31)).isoformat(),
                    "kind": "say",
                    "direction": "inbound",
                    "plain_text": "expired",
                    "display_text": "expired",
                    "provenance": {"sender_name": "Alice"},
                    "redacted": False,
                }
            ]
        )
        assert inserted == 0
        assert (await store.status())["worlds"] == []
    finally:
        await store.close()


async def test_recall_context_can_be_time_bounded_and_match_speaker_only(tmp_path) -> None:
    store = RecallStore(
        tmp_path / "recall.sqlite3",
        worlds={"alpha": RecallWorldPolicy(enabled=True, retention_days=None)},
        log_directory=tmp_path / "logs",
    )
    await store.start()
    try:
        await store.write(recall_event("Alice before", speaker="Alice", seconds=1))
        await store.write(recall_event("Bob interjects", speaker="Bob", seconds=2))
        await store.write(recall_event("Alice sees dragon", speaker="Alice", seconds=3))
        await store.write(recall_event("Alice after", speaker="Alice", seconds=4))
        await store.write(recall_event("Alice much later", speaker="Alice", seconds=20))

        result = await store.query(
            {
                "words": "dragon",
                "context_time_before": 5,
                "context_time_after": 5,
                "context_speakers": "matched",
            }
        )

        assert [event["text"] for event in result["events"]] == [
            "Alice before",
            "Alice sees dragon",
            "Alice after",
        ]
    finally:
        await store.close()


async def test_backfill_is_automatic_deduplicated_and_resumable(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    event = recall_event("historical dragon", speaker="Alice")
    value = {
        "event_id": str(event.event_id),
        "world": "alpha",
        "timestamp": event.timestamp.isoformat().replace("+00:00", "Z"),
        "kind": "say",
        "direction": "inbound",
        "plain_text": event.plain_text,
        "display_text": event.display_text,
        "provenance": {"sender_name": "Alice"},
        "redacted": False,
    }
    beta_value = {
        **value,
        "event_id": str(uuid4()),
        "world": "beta",
        "plain_text": "beta history",
        "display_text": "beta history",
    }
    path = logs / "events.jsonl"
    path.write_text(json.dumps(value) + "\n" + json.dumps(beta_value) + "\n", encoding="utf-8")
    database = tmp_path / "recall.sqlite3"

    first = RecallStore(
        database,
        worlds={
            "alpha": RecallWorldPolicy(enabled=True, retention_days=None),
            "beta": RecallWorldPolicy(enabled=False, retention_days=None),
        },
        log_directory=logs,
    )
    await first.start()
    await first._backfill_task
    assert (await first.query({"words": "dragon"}))["matches"] == 1
    await first.close()

    second = RecallStore(
        database,
        worlds={
            "alpha": RecallWorldPolicy(enabled=True, retention_days=None),
            "beta": RecallWorldPolicy(enabled=True, retention_days=None),
        },
        log_directory=logs,
    )
    await second.start()
    await second._backfill_task
    try:
        assert (await second.query({"words": "dragon"}))["matches"] == 1
        assert (await second.query({"words": "history", "world": "beta"}))["matches"] == 1
    finally:
        await second.close()
