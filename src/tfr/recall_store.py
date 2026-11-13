from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import regex as safe_regex

from tfr.events import Event

SCHEMA_VERSION = 1
PROJECTION_VERSION = "inbound-only-v1"
BACKFILL_BATCH_LINES = 500
QUERY_CANDIDATE_LIMIT = 20_000
MAX_MATCHES = 500
MAX_CONTEXT = 100
MAX_STATS_ROWS = 100
MAX_STATS_GROUPS = 20
_CONVERSATION_KINDS = ("say", "pose", "speech")
_WORD = safe_regex.compile(r"[\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)*")
_VISIBLE_SAY = safe_regex.compile(r"^(?P<speaker>.+?)\s+says?,\s+[\"“]", safe_regex.IGNORECASE)
_INFERRED_SPEAKER = safe_regex.compile(r"^[\p{L}\p{N}_'-]{1,64}$")
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "him",
        "his",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "she",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "who",
        "will",
        "with",
        "would",
        "you",
        "your",
        "say",
        "says",
    }
)


@dataclass(frozen=True, slots=True)
class RecallWorldPolicy:
    enabled: bool
    retention_days: int | None
    character_name: str | None = None


class RecallStore:
    """Gateway-owned searchable projection of eligible event logs."""

    def __init__(
        self,
        path: Path | str,
        *,
        worlds: dict[str, RecallWorldPolicy],
        log_directory: Path | str,
    ) -> None:
        self.path = Path(path).expanduser()
        self.worlds = worlds
        self.log_directory = Path(log_directory).expanduser()
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._backfill_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._write_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10_000)
        self._writer_error: BaseException | None = None
        self._closed = False
        self._backfill_state = "pending"
        self._backfill_error: str | None = None
        self._backfill_files_total = 0
        self._backfill_files_completed = 0
        self._backfill_bytes_total = 0
        self._backfill_bytes_checkpointed = 0
        self._backfill_records_scanned = 0
        self._backfill_records_inserted = 0
        self._projection_rebuilt = False

    async def start(self) -> None:
        await asyncio.to_thread(self._open)
        self._writer_task = asyncio.create_task(self._write_loop(), name="tfr-recall-writer")
        self._backfill_task = asyncio.create_task(self._backfill(), name="tfr-recall-backfill")
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="tfr-recall-maintenance"
        )

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self.path.parent, 0o700)
        new_database = not self.path.exists()
        connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        if new_database:
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute("VACUUM")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS recall_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recall_events (
                id INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                world TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                timestamp_us INTEGER NOT NULL,
                kind TEXT NOT NULL,
                direction TEXT NOT NULL,
                speaker TEXT,
                speaker_folded TEXT,
                text TEXT NOT NULL,
                display_text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS recall_events_world_time
                ON recall_events(world, timestamp_us, id);
            CREATE INDEX IF NOT EXISTS recall_events_speaker_time
                ON recall_events(speaker_folded, timestamp_us, id);
            CREATE INDEX IF NOT EXISTS recall_events_time
                ON recall_events(timestamp_us, id);
            CREATE VIRTUAL TABLE IF NOT EXISTS recall_events_fts USING fts5(
                text, content='recall_events', content_rowid='id', tokenize='unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS recall_events_ai AFTER INSERT ON recall_events BEGIN
                INSERT INTO recall_events_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS recall_events_ad AFTER DELETE ON recall_events BEGIN
                INSERT INTO recall_events_fts(recall_events_fts, rowid, text)
                VALUES('delete', old.id, old.text);
            END;
            """
        )
        row = connection.execute(
            "SELECT value FROM recall_metadata WHERE key='schema_version'"
        ).fetchone()
        if row is not None and int(row[0]) != SCHEMA_VERSION:
            connection.close()
            raise ValueError("recall database schema is unsupported")
        connection.execute(
            "INSERT OR REPLACE INTO recall_metadata(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        projection = connection.execute(
            "SELECT value FROM recall_metadata WHERE key='projection_version'"
        ).fetchone()
        if projection is None or projection[0] != PROJECTION_VERSION:
            connection.execute("DELETE FROM recall_events")
            connection.execute("DELETE FROM recall_metadata WHERE key LIKE 'backfill:%'")
            connection.execute(
                "INSERT OR REPLACE INTO recall_metadata(key, value) "
                "VALUES('projection_version', ?)",
                (PROJECTION_VERSION,),
            )
            self._projection_rebuilt = True
        else:
            # Defend against rows written by an older candidate after rollback.
            connection.execute("DELETE FROM recall_events WHERE direction!='inbound'")
        connection.commit()
        if os.name == "posix":
            os.chmod(self.path, 0o600)
        self._connection = connection

    async def write(self, event: Event) -> None:
        if self._writer_error is not None:
            raise RuntimeError("recall database writer failed") from self._writer_error
        policy = self.worlds.get(event.world)
        if (
            policy is None
            or not policy.enabled
            or event.redacted
            or event.direction.value != "inbound"
        ):
            return
        value = {
            "event_id": str(event.event_id),
            "world": event.world,
            "timestamp": event.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "kind": event.kind.value,
            "direction": event.direction.value,
            "plain_text": event.plain_text,
            "canonical_text": event.canonical_text,
            "display_text": event.display_text,
            "provenance": (
                {"sender_name": event.provenance.sender_name}
                if event.provenance is not None
                else None
            ),
            "spoof": {"speaker": event.spoof.speaker} if event.spoof is not None else None,
            "redacted": event.redacted,
        }
        await self._write_queue.put(value)

    async def _write_loop(self) -> None:
        try:
            while True:
                first = await self._write_queue.get()
                values = [first]
                while len(values) < 100:
                    try:
                        values.append(await asyncio.wait_for(self._write_queue.get(), timeout=0.05))
                    except TimeoutError:
                        break
                try:
                    await asyncio.to_thread(self._insert_values, values)
                finally:
                    for _value in values:
                        self._write_queue.task_done()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._writer_error = exc
            while True:
                try:
                    self._write_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._write_queue.task_done()

    def _insert_values(self, values: list[dict[str, Any]]) -> int:
        connection = self._require_connection()
        inserted = 0
        with self._lock, connection:
            for value in values:
                row = self._project(value)
                if row is None:
                    continue
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO recall_events(
                        event_id, world, timestamp, timestamp_us, kind, direction,
                        speaker, speaker_folded, text, display_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                inserted += cursor.rowcount
        return inserted

    def _project(self, value: dict[str, Any]) -> tuple[object, ...] | None:
        world = value.get("world")
        policy = self.worlds.get(str(world))
        if policy is None or not policy.enabled or value.get("redacted") is True:
            return None
        if value.get("direction") != "inbound":
            return None
        text = value.get("plain_text") or value.get("canonical_text") or value.get("display_text")
        if not isinstance(text, str) or not text:
            return None
        timestamp = value.get("timestamp")
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        parsed = parsed.astimezone(UTC)
        if policy.retention_days is not None and parsed < datetime.now(UTC) - timedelta(
            days=policy.retention_days
        ):
            return None
        provenance = value.get("provenance")
        spoof = value.get("spoof")
        speaker = (
            provenance.get("sender_name")
            if isinstance(provenance, dict) and isinstance(provenance.get("sender_name"), str)
            else spoof.get("speaker")
            if isinstance(spoof, dict) and isinstance(spoof.get("speaker"), str)
            else None
        )
        event_id = value.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return None
        display = value.get("display_text")
        return (
            event_id,
            str(world),
            parsed.isoformat().replace("+00:00", "Z"),
            int(parsed.timestamp() * 1_000_000),
            str(value.get("kind", "unknown")),
            str(value.get("direction", "unknown")),
            speaker,
            speaker.casefold() if speaker else None,
            text,
            display if isinstance(display, str) and display else text,
        )

    async def _backfill(self) -> None:
        self._backfill_state = "running"
        try:
            paths = tuple(sorted(self.log_directory.glob("*.jsonl")))
            enabled_worlds = tuple(world for world, policy in self.worlds.items() if policy.enabled)
            self._backfill_files_total = len(paths) * len(enabled_worlds)
            self._backfill_bytes_total = sum(
                self._file_size(path) for path in paths for _world in enabled_worlds
            )
            for world, policy in self.worlds.items():
                if not policy.enabled:
                    continue
                for path in paths:
                    if self._closed:
                        return
                    await self._backfill_file(path, world)
                    self._backfill_files_completed += 1
            self._backfill_state = "completed"
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._backfill_state = "failed"
            self._backfill_error = f"{type(exc).__name__}: {exc}"[:240]

    async def _backfill_file(self, path: Path, world: str) -> None:
        offset = await asyncio.to_thread(self._checkpoint, path, world)
        self._backfill_bytes_checkpointed += offset
        while not self._closed:
            values, next_offset, scanned = await asyncio.to_thread(
                self._read_log_batch, path, offset
            )
            if not values and next_offset == offset:
                return
            self._backfill_records_scanned += scanned
            if values:
                inserted = await asyncio.to_thread(
                    self._insert_values,
                    [value for value in values if value.get("world") == world],
                )
                self._backfill_records_inserted += inserted
            await asyncio.to_thread(self._set_checkpoint, path, world, next_offset)
            self._backfill_bytes_checkpointed += max(0, next_offset - offset)
            offset = next_offset
            await asyncio.sleep(0)

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _checkpoint_key(self, path: Path, world: str) -> str:
        return f"backfill:{world}:{path.resolve()}"

    def _checkpoint(self, path: Path, world: str) -> int:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT value FROM recall_metadata WHERE key=?", (self._checkpoint_key(path, world),)
        ).fetchone()
        offset = int(row[0]) if row is not None else 0
        try:
            size = path.stat().st_size
        except OSError:
            return offset
        return offset if offset <= size else 0

    def _set_checkpoint(self, path: Path, world: str, offset: int) -> None:
        connection = self._require_connection()
        with self._lock, connection:
            connection.execute(
                "INSERT OR REPLACE INTO recall_metadata(key, value) VALUES(?, ?)",
                (self._checkpoint_key(path, world), str(offset)),
            )

    @staticmethod
    def _read_log_batch(path: Path, offset: int) -> tuple[list[dict[str, Any]], int, int]:
        values: list[dict[str, Any]] = []
        scanned = 0
        try:
            with path.open("rb") as source:
                source.seek(offset)
                last_complete = offset
                for _ in range(BACKFILL_BATCH_LINES):
                    line = source.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    last_complete = source.tell()
                    scanned += 1
                    try:
                        value = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(value, dict):
                        values.append(value)
                return values, last_complete, scanned
        except OSError:
            return [], offset, 0

    async def query(self, query: dict[str, Any]) -> dict[str, Any]:
        await self._write_queue.join()
        if self._writer_error is not None:
            raise RuntimeError("recall database writer failed") from self._writer_error
        return await asyncio.to_thread(self._query, query)

    async def statistics(self, query: dict[str, Any]) -> dict[str, Any]:
        await self._write_queue.join()
        if self._writer_error is not None:
            raise RuntimeError("recall database writer failed") from self._writer_error
        return await asyncio.to_thread(self._statistics, query)

    def _statistics(self, query: dict[str, Any]) -> dict[str, Any]:
        view = query.get("view", "summary")
        if view not in {
            "summary",
            "speakers",
            "speaker",
            "worlds",
            "words",
            "activity",
            "hours",
            "weekdays",
            "heatmap",
        }:
            raise ValueError("stats view is invalid")
        world = self._optional_text(query.get("world"), "world")
        speaker = self._optional_text(query.get("speaker"), "speaker")
        after_us = self._time_bound(query.get("after"), "after")
        before_us = self._time_bound(query.get("before"), "before")
        top = query.get("top", 10)
        groups = query.get("groups", 10)
        minimum = query.get("minimum_word_length", 2)
        offset = query.get("timezone_offset_minutes", 0)
        if not isinstance(top, int) or isinstance(top, bool) or not 1 <= top <= MAX_STATS_ROWS:
            raise ValueError(f"stats top must be between 1 and {MAX_STATS_ROWS}")
        if (
            not isinstance(groups, int)
            or isinstance(groups, bool)
            or not 1 <= groups <= MAX_STATS_GROUPS
        ):
            raise ValueError(f"stats groups must be between 1 and {MAX_STATS_GROUPS}")
        if not isinstance(minimum, int) or isinstance(minimum, bool) or not 1 <= minimum <= 20:
            raise ValueError("stats minimum word length must be between 1 and 20")
        if not isinstance(offset, int) or isinstance(offset, bool) or not -840 <= offset <= 840:
            raise ValueError("stats timezone offset is invalid")
        arguments = (
            world,
            world,
            after_us,
            after_us,
            before_us,
            before_us,
        )
        rows = (
            self._require_connection()
            .execute(
                "SELECT world, timestamp, timestamp_us, kind, speaker, speaker_folded, text "
                "FROM recall_events WHERE direction='inbound' "
                "AND kind IN ('say','pose','speech','raw_output') "
                "AND (? IS NULL OR world=?) "
                "AND (? IS NULL OR timestamp_us>=?) AND (? IS NULL OR timestamp_us<=?) "
                "ORDER BY timestamp_us, id",
                arguments,
            )
            .fetchall()
        )
        values = self._infer_stats_values([dict(row) for row in rows])
        if speaker:
            values = [
                value
                for value in values
                if str(value.get("speaker_folded", "")) == speaker.casefold()
            ]
        summary = self._stats_summary(values)
        result: dict[str, Any] = {
            "view": view,
            "world": world,
            "speaker": speaker,
            "summary": summary,
            "scanned_lines": len(values),
            "timezone_offset_minutes": offset,
        }
        if view == "summary":
            return result
        if view in {"speakers", "speaker"}:
            result["rows"] = self._stats_groups(values, "speaker", top)
        elif view == "worlds":
            result["rows"] = self._stats_groups(values, "world", top)
        elif view == "words":
            result.update(self._stats_words(values, query, top, groups, minimum))
        elif view in {"hours", "weekdays", "heatmap", "activity"}:
            result.update(self._stats_time(values, view, query.get("bucket", "day"), offset))
        return result

    def _infer_stats_values(self, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        values = [
            value
            for value in values
            if not str(value.get("text", "")).lstrip().startswith("▶ [")
        ]
        known: dict[str, dict[str, str]] = defaultdict(dict)
        for world, policy in self.worlds.items():
            if policy.character_name:
                known[world][policy.character_name.casefold()] = policy.character_name
        for value in values:
            speaker = value.get("speaker")
            if speaker:
                value["attribution"] = "stored"
                known[str(value["world"])][str(speaker).casefold()] = str(speaker)
                continue
            if value["kind"] != "say":
                continue
            text = str(value["text"]).lstrip()
            policy = self.worlds.get(str(value["world"]))
            if policy and policy.character_name and text.casefold().startswith("you say,"):
                self._set_inferred_speaker(value, policy.character_name, "configured-character")
            elif match := _VISIBLE_SAY.match(text):
                speaker = match.group("speaker").strip()
                if speaker.casefold() != "you" and _INFERRED_SPEAKER.fullmatch(speaker):
                    self._set_inferred_speaker(value, speaker, "visible-text")
            if value.get("speaker"):
                speaker = str(value["speaker"])
                known[str(value["world"])][speaker.casefold()] = speaker
        output: list[dict[str, Any]] = []
        for value in values:
            if value["kind"] != "raw_output":
                output.append(value)
                continue
            text = str(value["text"]).lstrip()
            speakers = known.get(str(value["world"]), {})
            matches = [
                display
                for folded, display in speakers.items()
                if self._stats_speaker_prefix(text, folded)
            ]
            if not matches:
                continue
            speaker = max(matches, key=len)
            value["kind"] = "pose"
            character = getattr(self.worlds.get(str(value["world"])), "character_name", None)
            source = (
                "configured-character"
                if character and speaker.casefold() == character.casefold()
                else "visible-text"
            )
            self._set_inferred_speaker(value, speaker, source)
            output.append(value)
        return output

    @staticmethod
    def _set_inferred_speaker(value: dict[str, Any], speaker: str, source: str) -> None:
        value["speaker"] = speaker
        value["speaker_folded"] = speaker.casefold()
        value["attribution"] = source

    @staticmethod
    def _stats_speaker_prefix(text: str, speaker_folded: str) -> bool:
        if not text.casefold().startswith(speaker_folded):
            return False
        suffix = text[len(speaker_folded) :]
        return not suffix or suffix[0].isspace() or suffix[:2].casefold() in {"'s", "’s"}

    @staticmethod
    def _stats_summary(values: list[dict[str, Any]]) -> dict[str, Any]:
        kinds = Counter(str(value["kind"]) for value in values)
        speakers = {str(value["speaker_folded"]) for value in values if value["speaker_folded"]}
        attributed = sum(value["speaker_folded"] is not None for value in values)
        attribution = Counter(str(value.get("attribution", "unattributed")) for value in values)
        classified = kinds["say"] + kinds["pose"]
        return {
            "lines": len(values),
            "attributed_lines": attributed,
            "unattributed_lines": len(values) - attributed,
            "stored_attribution_lines": attribution["stored"],
            "configured_character_lines": attribution["configured-character"],
            "inferred_attribution_lines": attribution["visible-text"],
            "speakers": len(speakers),
            "says": kinds["say"],
            "poses": kinds["pose"],
            "speech": kinds["speech"],
            "say_percent": round(kinds["say"] * 100 / classified, 1) if classified else 0.0,
            "pose_percent": round(kinds["pose"] * 100 / classified, 1) if classified else 0.0,
            "classification_coverage": round(classified * 100 / len(values), 1) if values else 0.0,
            "first": values[0]["timestamp"] if values else None,
            "last": values[-1]["timestamp"] if values else None,
        }

    @classmethod
    def _stats_groups(
        cls, values: list[dict[str, Any]], field: str, limit: int
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, str] = {}
        key_field = "speaker_folded" if field == "speaker" else "world"
        for value in values:
            raw = value.get(key_field)
            if raw is None:
                continue
            key = str(raw)
            labels.setdefault(key, str(value.get(field) or raw))
            grouped[key].append(value)
        output = []
        for key, items in grouped.items():
            summary = cls._stats_summary(items)
            output.append({"name": labels[key], **summary})
        output.sort(key=lambda item: (-int(item["lines"]), str(item["name"]).casefold()))
        total = sum(int(item["lines"]) for item in output)
        for item in output:
            item["share_percent"] = round(int(item["lines"]) * 100 / total, 1) if total else 0.0
        return output[:limit]

    @staticmethod
    def _tokens(text: str, speaker: str | None, *, minimum: int, stop: bool) -> list[str]:
        excluded = set(_STOP_WORDS if stop else ())
        if speaker:
            excluded.update(token.casefold() for token in _WORD.findall(speaker))
            folded_text = text.casefold().lstrip()
            folded_speaker = speaker.casefold()
            if folded_text.startswith(folded_speaker):
                text = text.lstrip()[len(speaker) :].lstrip()
            elif folded_text.startswith("you say,"):
                text = text.lstrip()[len("you say,") :].lstrip()
        return [
            token.casefold()
            for token in _WORD.findall(text)
            if len(token) >= minimum and token.casefold() not in excluded
        ]

    @classmethod
    def _stats_words(
        cls,
        values: list[dict[str, Any]],
        query: dict[str, Any],
        top: int,
        groups: int,
        minimum: int,
    ) -> dict[str, Any]:
        by = query.get("by")
        if by not in {None, "speaker", "world"}:
            raise ValueError("stats words by must be speaker or world")
        stop = query.get("stop_words", True)
        if not isinstance(stop, bool):
            raise ValueError("stats stop_words must be boolean")
        if by is None:
            counter: Counter[str] = Counter()
            for value in values:
                counter.update(
                    cls._tokens(
                        str(value["text"]), value.get("speaker"), minimum=minimum, stop=stop
                    )
                )
            return {
                "words": [
                    {"word": word, "count": count} for word, count in counter.most_common(top)
                ]
            }
        active = cls._stats_groups(values, by, groups)
        selected = {str(item["name"]).casefold() for item in active}
        counters: dict[str, Counter[str]] = defaultdict(Counter)
        labels: dict[str, str] = {}
        for value in values:
            label = value.get(by)
            if not label or str(label).casefold() not in selected:
                continue
            key = str(label).casefold()
            labels.setdefault(key, str(label))
            counters[key].update(
                cls._tokens(str(value["text"]), value.get("speaker"), minimum=minimum, stop=stop)
            )
        return {
            "word_groups": [
                {
                    "name": labels[key],
                    "words": [
                        {"word": word, "count": count} for word, count in counter.most_common(top)
                    ],
                }
                for key, counter in sorted(
                    counters.items(), key=lambda item: -sum(item[1].values())
                )
            ]
        }

    @staticmethod
    def _stats_time(
        values: list[dict[str, Any]], view: str, bucket: object, offset_minutes: int
    ) -> dict[str, Any]:
        shifted = [
            datetime.fromisoformat(str(value["timestamp"]).replace("Z", "+00:00"))
            + timedelta(minutes=offset_minutes)
            for value in values
        ]
        if view == "hours":
            counts = Counter(value.hour for value in shifted)
            return {
                "series": [{"label": f"{hour:02d}", "count": counts[hour]} for hour in range(24)]
            }
        if view == "weekdays":
            counts = Counter(value.weekday() for value in shifted)
            names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
            return {"series": [{"label": names[day], "count": counts[day]} for day in range(7)]}
        if view == "heatmap":
            counts = Counter((value.weekday(), value.hour) for value in shifted)
            return {"matrix": [[counts[(day, hour)] for hour in range(24)] for day in range(7)]}
        if bucket not in {"hour", "day", "month"}:
            raise ValueError("stats activity bucket must be hour, day, or month")
        labels = []
        for value in shifted:
            labels.append(
                value.strftime("%Y-%m-%d %H:00")
                if bucket == "hour"
                else value.strftime("%Y-%m")
                if bucket == "month"
                else value.strftime("%Y-%m-%d")
            )
        counts = Counter(labels)
        return {"series": [{"label": label, "count": counts[label]} for label in sorted(counts)]}

    def _query(self, query: dict[str, Any]) -> dict[str, Any]:
        speaker = self._optional_text(query.get("speaker"), "speaker")
        words = self._optional_text(query.get("words"), "words")
        regexp = self._optional_text(query.get("regexp"), "regexp")
        if words and regexp:
            raise ValueError("words and regexp are mutually exclusive")
        world = self._optional_text(query.get("world"), "world")
        kinds = query.get("kinds", [])
        if not isinstance(kinds, list) or not all(isinstance(item, str) and item for item in kinds):
            raise ValueError("recall kinds are invalid")
        limit = query.get("limit", 50)
        context_before = query.get("context_before", query.get("context", 0))
        context_after = query.get("context_after", query.get("context", 0))
        context_time_before = query.get("context_time_before", 0)
        context_time_after = query.get("context_time_after", 0)
        context_speakers = query.get("context_speakers", "all")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_MATCHES:
            raise ValueError(f"recall limit must be between 1 and {MAX_MATCHES}")
        for value in (context_before, context_after):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= MAX_CONTEXT
            ):
                raise ValueError(f"recall context must be between 0 and {MAX_CONTEXT}")
        for value in (context_time_before, context_time_after):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 86_400:
                raise ValueError("recall context time must be between 0 and 86400 seconds")
        if context_speakers not in {"all", "matched"}:
            raise ValueError("recall context speakers must be all or matched")
        after_us = self._time_bound(query.get("after"), "after")
        before_us = self._time_bound(query.get("before"), "before")
        kinds_json = json.dumps(kinds, separators=(",", ":"))
        arguments: tuple[object, ...] = (
            world,
            world,
            speaker.casefold() if speaker else None,
            speaker.casefold() if speaker else None,
            kinds_json,
            kinds_json,
            after_us,
            after_us,
            before_us,
            before_us,
        )
        use_fts = words is not None and (
            "*" not in words or words.endswith("*") and words.count("*") == 1
        )
        connection = self._require_connection()
        if use_fts:
            if words is not None and words.endswith("*"):
                fts_query = f'"{words[:-1].replace(chr(34), chr(34) * 2)}"*'
            else:
                assert words is not None
                fts_query = f'"{words.replace(chr(34), chr(34) * 2)}"'
            rows = connection.execute(
                "SELECT recall_events.* FROM recall_events "
                "JOIN recall_events_fts ON recall_events_fts.rowid=recall_events.id "
                "WHERE (? IS NULL OR world=?) "
                "AND direction='inbound' "
                "AND (? IS NULL OR speaker_folded=?) "
                "AND (?='[]' OR kind IN (SELECT value FROM json_each(?))) "
                "AND (? IS NULL OR timestamp_us>=?) AND (? IS NULL OR timestamp_us<=?) "
                "AND recall_events_fts MATCH ? "
                "ORDER BY timestamp_us DESC, id DESC LIMIT ?",
                (*arguments, fts_query, QUERY_CANDIDATE_LIMIT),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM recall_events WHERE (? IS NULL OR world=?) "
                "AND direction='inbound' "
                "AND (? IS NULL OR speaker_folded=?) "
                "AND (?='[]' OR kind IN (SELECT value FROM json_each(?))) "
                "AND (? IS NULL OR timestamp_us>=?) AND (? IS NULL OR timestamp_us<=?) "
                "ORDER BY timestamp_us DESC, id DESC LIMIT ?",
                (*arguments, QUERY_CANDIDATE_LIMIT),
            ).fetchall()
        matcher = self._matcher(words, regexp)
        matches = [row for row in rows if matcher is None or matcher(row["text"])][:limit]
        matches.reverse()
        match_ids = {row["id"] for row in matches}
        output: list[dict[str, Any]] = []
        included: set[int] = set()
        for match in matches:
            context_rows: list[sqlite3.Row] = []
            context_speaker = (
                match["speaker_folded"]
                if context_speakers == "matched" and match["speaker_folded"]
                else None
            )
            if context_before or context_time_before:
                lower = match["timestamp_us"] - int(context_time_before) * 1_000_000
                context_rows.extend(
                    reversed(
                        connection.execute(
                            "SELECT * FROM recall_events WHERE world=? AND "
                            "direction='inbound' AND "
                            "(? IS NULL OR timestamp_us>=?) AND "
                            "(timestamp_us < ? OR (timestamp_us = ? AND id < ?)) AND "
                            "(? IS NULL OR speaker_folded=?) "
                            "ORDER BY timestamp_us DESC, id DESC LIMIT ?",
                            (
                                match["world"],
                                lower if context_time_before else None,
                                lower if context_time_before else None,
                                match["timestamp_us"],
                                match["timestamp_us"],
                                match["id"],
                                context_speaker,
                                context_speaker,
                                context_before or MAX_CONTEXT,
                            ),
                        ).fetchall()
                    )
                )
            context_rows.append(match)
            if context_after or context_time_after:
                upper = match["timestamp_us"] + int(context_time_after) * 1_000_000
                context_rows.extend(
                    connection.execute(
                        "SELECT * FROM recall_events WHERE world=? AND "
                        "direction='inbound' AND "
                        "(? IS NULL OR timestamp_us<=?) AND "
                        "(timestamp_us > ? OR (timestamp_us = ? AND id > ?)) AND "
                        "(? IS NULL OR speaker_folded=?) "
                        "ORDER BY timestamp_us, id LIMIT ?",
                        (
                            match["world"],
                            upper if context_time_after else None,
                            upper if context_time_after else None,
                            match["timestamp_us"],
                            match["timestamp_us"],
                            match["id"],
                            context_speaker,
                            context_speaker,
                            context_after or MAX_CONTEXT,
                        ),
                    ).fetchall()
                )
            for row in context_rows:
                if row["id"] in included:
                    continue
                included.add(row["id"])
                output.append(self._row_dict(row, matched=row["id"] in match_ids))
        output.sort(key=lambda item: (str(item["timestamp"]), int(item["id"])))
        return {"matches": len(matches), "events": output, "truncated": len(matches) == limit}

    @staticmethod
    def _matcher(words: str | None, regexp: str | None) -> Any:
        if regexp:
            try:
                expression = safe_regex.compile(regexp)
            except safe_regex.error as exc:
                raise ValueError(f"invalid recall regular expression: {exc}") from exc

            def regex_match(text: str) -> bool:
                try:
                    return expression.search(text, timeout=0.1) is not None
                except TimeoutError:
                    raise ValueError("recall regular expression exceeded its time limit") from None

            return regex_match
        if not words:
            return None
        pieces = words.split("*")
        pattern = ".*?".join(safe_regex.escape(piece) for piece in pieces)
        if not words.startswith("*"):
            pattern = r"\b" + pattern
        if not words.endswith("*"):
            pattern += r"\b"
        expression = safe_regex.compile(pattern, safe_regex.IGNORECASE | safe_regex.DOTALL)
        return lambda text: expression.search(text, timeout=0.1) is not None

    @staticmethod
    def _optional_text(value: object, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 1_024:
            raise ValueError(f"recall {name} is invalid")
        return value

    @staticmethod
    def _time_bound(value: object, name: str) -> int | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"recall {name} time is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"recall {name} time is invalid") from exc
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return int(parsed.astimezone(UTC).timestamp() * 1_000_000)

    @staticmethod
    def _row_dict(row: sqlite3.Row, *, matched: bool) -> dict[str, Any]:
        return {
            "id": row["id"],
            "event_id": row["event_id"],
            "world": row["world"],
            "timestamp": row["timestamp"],
            "kind": row["kind"],
            "direction": row["direction"],
            "speaker": row["speaker"],
            "text": row["display_text"],
            "matched": matched,
        }

    async def status(self) -> dict[str, Any]:
        await self._write_queue.join()
        if self._writer_error is not None:
            raise RuntimeError("recall database writer failed") from self._writer_error
        return await asyncio.to_thread(self._status)

    def _status(self) -> dict[str, Any]:
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT world, COUNT(*) AS count, MIN(timestamp) AS oldest, MAX(timestamp) AS newest "
            "FROM recall_events GROUP BY world ORDER BY world"
        ).fetchall()
        indexed = {str(row["world"]): dict(row) for row in rows}
        return {
            "enabled": True,
            "database_path": str(self.path),
            "database_bytes": sum(
                candidate.stat().st_size
                for candidate in (
                    self.path,
                    Path(f"{self.path}-wal"),
                    Path(f"{self.path}-shm"),
                )
                if candidate.exists()
            ),
            "worlds": [
                {
                    "world": world,
                    "enabled": policy.enabled,
                    "retention_days": policy.retention_days,
                    "count": int(indexed.get(world, {}).get("count", 0)),
                    "oldest": indexed.get(world, {}).get("oldest"),
                    "newest": indexed.get(world, {}).get("newest"),
                }
                for world, policy in self.worlds.items()
            ],
            "backfill_active": self._backfill_task is not None and not self._backfill_task.done(),
            "projection": PROJECTION_VERSION,
            "projection_rebuilt": self._projection_rebuilt,
            "backfill": {
                "state": self._backfill_state,
                "error": self._backfill_error,
                "files_total": self._backfill_files_total,
                "files_completed": self._backfill_files_completed,
                "bytes_total": self._backfill_bytes_total,
                "bytes_checkpointed": self._backfill_bytes_checkpointed,
                "records_scanned": self._backfill_records_scanned,
                "records_inserted": self._backfill_records_inserted,
            },
        }

    async def _maintenance_loop(self) -> None:
        try:
            while not self._closed:
                while await asyncio.to_thread(self._prune_batch):
                    await asyncio.sleep(0.05)
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    def _prune_batch(self) -> int:
        connection = self._require_connection()
        now = datetime.now(UTC)
        deleted = 0
        with self._lock, connection:
            for world, policy in self.worlds.items():
                if policy.retention_days is None:
                    continue
                cutoff = int((now - timedelta(days=policy.retention_days)).timestamp() * 1_000_000)
                cursor = connection.execute(
                    "DELETE FROM recall_events WHERE id IN ("
                    "SELECT id FROM recall_events WHERE world=? AND timestamp_us<? LIMIT 1000)",
                    (world, cutoff),
                )
                deleted += cursor.rowcount
            if deleted:
                connection.execute("PRAGMA incremental_vacuum(100)")
        return deleted

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("recall database is not open")
        return self._connection

    async def close(self) -> None:
        self._closed = True
        if self._writer_task is not None:
            await self._write_queue.join()
            self._writer_task.cancel()
        for task in (self._backfill_task, self._maintenance_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (self._writer_task, self._backfill_task, self._maintenance_task)
                if task is not None
            ),
            return_exceptions=True,
        )
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
