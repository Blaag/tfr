from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from tfr.events import Confidence, Direction, Event, EventKind, Provenance
from tfr.recall_store import RecallStore, RecallWorldPolicy


def recall_event(
    text: str,
    *,
    speaker: str,
    seconds: int = 0,
    direction: Direction = Direction.INBOUND,
) -> Event:
    return Event(
        event_id=uuid4(),
        session_id=UUID("11111111-1111-4111-8111-111111111111"),
        world="alpha",
        connection_generation=1,
        sequence=seconds,
        timestamp=datetime(2026, 11, 11, 12, tzinfo=UTC) + timedelta(seconds=seconds),
        direction=direction,
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
        await store.write(
            recall_event(
                "say A red dragon appears",
                speaker="Alice",
                seconds=2,
                direction=Direction.OUTBOUND,
            )
        )
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


async def test_recall_projection_rebuilds_existing_database_as_inbound_only(tmp_path) -> None:
    database = tmp_path / "recall.sqlite3"
    store = RecallStore(
        database,
        worlds={"alpha": RecallWorldPolicy(enabled=True, retention_days=None)},
        log_directory=tmp_path / "logs",
    )
    await store.start()
    await store.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute("DELETE FROM recall_metadata WHERE key='projection_version'")
        connection.execute(
            "INSERT INTO recall_metadata(key, value) VALUES(?, '100')",
            (f"backfill:alpha:{database.resolve()}",),
        )
        connection.execute(
            "INSERT INTO recall_events(event_id, world, timestamp, timestamp_us, kind, "
            "direction, speaker, speaker_folded, text, display_text) "
            "VALUES(?, 'alpha', '2026-11-11T12:00:00Z', 1, 'command', 'outbound', "
            "NULL, NULL, '@@', '@@')",
            (str(uuid4()),),
        )
        connection.commit()
    finally:
        connection.close()

    rebuilt = RecallStore(
        database,
        worlds={"alpha": RecallWorldPolicy(enabled=True, retention_days=None)},
        log_directory=tmp_path / "logs",
    )
    await rebuilt.start()
    try:
        status = await rebuilt.status()
        assert status["projection"] == "inbound-only-v1"
        assert status["projection_rebuilt"] is True
        assert (await rebuilt.query({"words": "@@"}))["matches"] == 0
        assert rebuilt._checkpoint(database, "alpha") == 0
    finally:
        await rebuilt.close()


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
        worlds = (await store.status())["worlds"]
        assert worlds == [
            {
                "world": "alpha",
                "enabled": False,
                "retention_days": None,
                "count": 0,
                "oldest": None,
                "newest": None,
            },
            {
                "world": "beta",
                "enabled": True,
                "retention_days": 30,
                "count": 0,
                "oldest": None,
                "newest": None,
            },
        ]
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


async def test_recall_statistics_aggregate_speakers_words_and_time(tmp_path) -> None:
    store = RecallStore(
        tmp_path / "recall.sqlite3",
        worlds={"alpha": RecallWorldPolicy(enabled=True, retention_days=None)},
        log_directory=tmp_path / "logs",
    )
    await store.start()
    try:
        await store.write(
            recall_event('Alice says, "red dragon coffee"', speaker="Alice", seconds=1)
        )
        await store.write(recall_event("Alice waves at the dragon", speaker="Alice", seconds=2))
        await store.write(recall_event('Bob says, "coffee time"', speaker="Bob", seconds=3602))
        await store._write_queue.join()

        speakers = await store.statistics({"view": "speakers", "world": "alpha"})
        assert speakers["summary"]["lines"] == 3
        assert [row["name"] for row in speakers["rows"]] == ["Alice", "Bob"]
        assert speakers["rows"][0]["lines"] == 2

        words = await store.statistics(
            {"view": "words", "world": "alpha", "top": 3, "stop_words": True}
        )
        assert words["words"][0] == {"word": "dragon", "count": 2}
        assert all(item["word"] not in {"alice", "says", "the"} for item in words["words"])

        hours = await store.statistics(
            {"view": "hours", "world": "alpha", "timezone_offset_minutes": 0}
        )
        assert hours["series"][12]["count"] == 2
        assert hours["series"][13]["count"] == 1
        heatmap = await store.statistics(
            {"view": "heatmap", "world": "alpha", "timezone_offset_minutes": 0}
        )
        assert sum(sum(row) for row in heatmap["matrix"]) == 3
    finally:
        await store.close()


async def test_stats_infer_local_and_visible_speakers_without_provenance(tmp_path) -> None:
    store = RecallStore(
        tmp_path / "recall.sqlite3",
        worlds={
            "global": RecallWorldPolicy(enabled=True, retention_days=None, character_name="black"),
            "alien": RecallWorldPolicy(enabled=True, retention_days=None),
        },
        log_directory=tmp_path / "logs",
    )
    await store.start()
    try:
        base = {
            "timestamp": "2026-11-11T12:00:00Z",
            "direction": "inbound",
            "display_text": "unused",
            "redacted": False,
        }
        values = [
            {
                **base,
                "event_id": str(uuid4()),
                "world": "global",
                "kind": "say",
                "plain_text": 'You say, "hello"',
            },
            {
                **base,
                "event_id": str(uuid4()),
                "world": "global",
                "kind": "raw_output",
                "plain_text": "black waves.",
            },
            {
                **base,
                "event_id": str(uuid4()),
                "world": "alien",
                "kind": "say",
                "plain_text": 'Blood says, "hello"',
            },
            {
                **base,
                "event_id": str(uuid4()),
                "world": "alien",
                "kind": "raw_output",
                "plain_text": "Blood waves.",
            },
            {
                **base,
                "event_id": str(uuid4()),
                "world": "alien",
                "kind": "raw_output",
                "plain_text": "Unknown system output",
            },
            {
                **base,
                "event_id": str(uuid4()),
                "world": "alien",
                "kind": "say",
                "plain_text": '▶ [2026-09-15T12:40:00Z] Blood says, "old recall"',
            },
        ]
        store._insert_values(values)

        local = await store.statistics({"view": "speaker", "world": "global", "speaker": "black"})
        assert local["summary"]["lines"] == 2
        assert local["summary"]["configured_character_lines"] == 2
        assert local["summary"]["poses"] == 1

        alien = await store.statistics({"view": "speakers", "world": "alien"})
        assert alien["summary"]["lines"] == 2
        assert alien["summary"]["unattributed_lines"] == 0
        assert alien["summary"]["inferred_attribution_lines"] == 2
        assert alien["summary"]["poses"] == 1
        assert alien["rows"][0]["name"] == "Blood"
        assert all(not row["name"].startswith("▶") for row in alien["rows"])

        words = await store.statistics({"view": "words", "top": 15})
        tokens = {item["word"] for item in words["words"]}
        assert "blood" not in tokens
        assert "black" not in tokens
        assert "says" not in tokens
        assert "hello" in tokens
    finally:
        await store.close()
