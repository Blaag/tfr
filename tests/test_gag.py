from __future__ import annotations

import os
import stat
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tfr.config import GagConfig, WorldConfig
from tfr.core import CommandBus, EventBus
from tfr.events import Direction, Event, EventKind
from tfr.gag_persist import GagPersistError, persist_gag_pattern
from tfr.plugins import PluginManager, PluginWorldInfo


def _inbound(world: str, text: str) -> Event:
    return Event(
        session_id=uuid4(),
        world=world,
        connection_generation=1,
        sequence=1,
        direction=Direction.INBOUND,
        kind=EventKind.RAW_OUTPUT,
        canonical_text=text,
        plain_text=text,
        display_text=text,
    )


def test_gag_config_accepts_valid_patterns() -> None:
    world = WorldConfig(
        host="example.org",
        port=4201,
        gag={"patterns": ["database (saving|backup)", r"^\[BACKUP\]"]},
    )
    assert world.gag is not None
    assert world.gag.patterns == ("database (saving|backup)", r"^\[BACKUP\]")


def test_gag_config_rejects_invalid_regex() -> None:
    with pytest.raises(ValidationError, match="invalid gag pattern"):
        GagConfig(patterns=["(unbalanced"])


def test_gag_config_rejects_empty_pattern() -> None:
    with pytest.raises(ValidationError, match="cannot be empty"):
        GagConfig(patterns=[""])


async def _manager_with_gag(patterns: tuple[str, ...]) -> PluginManager:
    return await PluginManager.load(
        enabled=(),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        worlds={
            "delusions": PluginWorldInfo(
                server="tinymux", encoding="utf-8", gag_patterns=patterns
            )
        },
        discovered=(),
        scope="gateway",
    )


async def test_gagged_inbound_event_is_flagged_and_skips_activity() -> None:
    manager = await _manager_with_gag(("database saving",))
    processed = await manager.process_event(
        _inbound("delusions", "The database saving process has started.")
    )
    await manager.drain()

    # Event is tagged gagged and NO world_activity was queued for it.
    assert processed.metadata.get("gagged") is True
    assert not manager._queued_world_activity


async def test_non_matching_inbound_event_is_not_gagged_and_triggers_activity() -> None:
    manager = await _manager_with_gag(("database saving",))
    processed = await manager.process_event(
        _inbound("delusions", "Someone says, \"hello\"")
    )

    assert "gagged" not in processed.metadata
    # A normal inbound line queues a world_activity notification.
    assert manager._queued_world_activity


async def test_world_without_gag_patterns_never_suppresses() -> None:
    manager = await PluginManager.load(
        enabled=(),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        worlds={"delusions": PluginWorldInfo(server="tinymux", encoding="utf-8")},
        discovered=(),
        scope="gateway",
    )
    processed = await manager.process_event(
        _inbound("delusions", "database saving now")
    )
    assert "gagged" not in processed.metadata


# --- PluginManager live gag-set mutation -------------------------------------


async def test_add_list_remove_live_gag_patterns() -> None:
    manager = await _manager_with_gag(())
    assert manager.list_gag_patterns("delusions") == ()

    assert manager.add_gag_pattern("delusions", "database saving") is True
    # Adding the same pattern again is a no-op.
    assert manager.add_gag_pattern("delusions", "database saving") is False
    assert manager.list_gag_patterns("delusions") == ("database saving",)

    # A newly added pattern takes effect immediately.
    processed = await manager.process_event(
        _inbound("delusions", "database saving in progress")
    )
    assert processed.metadata.get("gagged") is True

    assert manager.remove_gag_pattern("delusions", "database saving") is True
    assert manager.remove_gag_pattern("delusions", "database saving") is False
    assert manager.list_gag_patterns("delusions") == ()


async def test_add_gag_pattern_rejects_invalid_regex_and_unknown_world() -> None:
    manager = await _manager_with_gag(())
    with pytest.raises(ValueError, match="invalid gag pattern"):
        manager.add_gag_pattern("delusions", "(unbalanced")
    with pytest.raises(ValueError, match="unknown world"):
        manager.add_gag_pattern("nope", "x")


async def test_add_gag_pattern_enforces_per_world_limit() -> None:
    manager = await _manager_with_gag(())
    for index in range(PluginManager.MAX_GAG_PATTERNS_PER_WORLD):
        assert manager.add_gag_pattern("delusions", f"pattern{index}") is True
    with pytest.raises(ValueError, match="maximum"):
        manager.add_gag_pattern("delusions", "one-too-many")


# --- worlds.jsonc persist writer ---------------------------------------------


_WORLDS = """{
  // top comment
  "schema_version": 1,
  "worlds": {
    "delusions": {
      "aliases": ["d"],
      "host": "del.example.org",
      "port": 4999,
    },
    "other": {
      "host": "other.example.org",
      "port": 4201,
    },
  },
}
"""


def test_persist_creates_gag_block_preserving_comments(tmp_path) -> None:
    path = tmp_path / "worlds.jsonc"
    path.write_text(_WORLDS, encoding="utf-8")

    assert persist_gag_pattern(path, "delusions", "database saving") is True
    updated = path.read_text(encoding="utf-8")

    # Comment preserved, pattern present, other world untouched, still parses.
    assert "// top comment" in updated
    assert "database saving" in updated
    import jsonc

    parsed = jsonc.loads(updated)
    assert parsed["worlds"]["delusions"]["gag"]["patterns"] == ["database saving"]
    assert "gag" not in parsed["worlds"]["other"]
    # A timestamped backup was written.
    assert any(child.name.startswith("worlds.jsonc.bak-gag-") for child in tmp_path.iterdir())


def test_persist_appends_to_existing_gag_block(tmp_path) -> None:
    path = tmp_path / "worlds.jsonc"
    path.write_text(_WORLDS, encoding="utf-8")
    persist_gag_pattern(path, "delusions", "first")

    assert persist_gag_pattern(path, "delusions", "second") is True
    import jsonc

    parsed = jsonc.loads(path.read_text(encoding="utf-8"))
    assert parsed["worlds"]["delusions"]["gag"]["patterns"] == ["first", "second"]


def test_persist_is_noop_when_pattern_already_present(tmp_path) -> None:
    path = tmp_path / "worlds.jsonc"
    path.write_text(_WORLDS, encoding="utf-8")
    persist_gag_pattern(path, "delusions", "dup")
    assert persist_gag_pattern(path, "delusions", "dup") is False


def test_persist_rejects_unknown_world(tmp_path) -> None:
    path = tmp_path / "worlds.jsonc"
    path.write_text(_WORLDS, encoding="utf-8")
    with pytest.raises(GagPersistError, match="not found"):
        persist_gag_pattern(path, "ghost", "x")


def test_persist_preserves_file_permissions(tmp_path) -> None:
    path = tmp_path / "worlds.jsonc"
    path.write_text(_WORLDS, encoding="utf-8")
    os.chmod(path, 0o600)
    persist_gag_pattern(path, "delusions", "keep perms")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)
