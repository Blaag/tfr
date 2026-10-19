from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from tfr.config import (
    AgentsConfig,
    ConfigurationBundle,
    GagConfig,
    LoggingConfig,
    MainConfig,
    ManagedSettingsConfig,
    UpdateConfig,
    WorldConfig,
    WorldsConfig,
)
from tfr.events import Direction, Event, EventKind
from tfr.gateway import GatewayRuntime
from tfr.managed_settings import (
    MAX_MANAGED_SETTINGS_BYTES,
    ManagedSettingsError,
    ManagedSettingsRevisionConflict,
    ManagedSettingsStore,
    apply_managed_settings,
)


def worlds() -> WorldsConfig:
    return WorldsConfig(
        worlds={
            "alpha": WorldConfig(
                host="alpha.example",
                port=4201,
                gag=GagConfig(patterns=("from base",)),
            ),
            "beta": WorldConfig(host="beta.example", port=4202),
        }
    )


def configuration_bundle(tmp_path: Path) -> ConfigurationBundle:
    return ConfigurationBundle(
        main_path=tmp_path / "config.jsonc",
        worlds_path=tmp_path / "worlds.jsonc",
        agents_path=tmp_path / "agents.jsonc",
        main=MainConfig(
            logging=LoggingConfig(enabled=False),
            updates=UpdateConfig(enabled=False),
            managed_settings=ManagedSettingsConfig(state_directory=tmp_path / "settings"),
        ),
        worlds=worlds(),
        agents=AgentsConfig(),
    )


def test_apply_managed_settings_returns_effective_gateway_bundle(tmp_path: Path) -> None:
    bundle = configuration_bundle(tmp_path)
    writer = ManagedSettingsStore(bundle.main.managed_settings.state_directory, bundle.worlds)
    writer.add_gag_pattern("alpha", "managed", 0)

    effective, store = apply_managed_settings(bundle)

    assert bundle.worlds.worlds["alpha"].gag.patterns == ("from base",)
    assert effective.worlds.worlds["alpha"].gag.patterns == ("from base", "managed")
    assert store.revision == 1


def test_shared_spellcheck_and_streak_settings_persist_and_apply(tmp_path: Path) -> None:
    bundle = configuration_bundle(tmp_path)
    store = ManagedSettingsStore(
        bundle.main.managed_settings.state_directory,
        bundle.worlds,
        base_ui=bundle.main.ui,
    )

    spellcheck = store.set_spellcheck_enabled(True, 0)
    streaks = store.set_speaking_streaks_enabled(False, 1)
    effective, reloaded = apply_managed_settings(bundle)

    assert spellcheck.revision == 1
    assert streaks.revision == 2
    assert effective.main.ui.spellcheck.enabled is True
    assert effective.main.ui.speaking_streaks.enabled is False
    assert reloaded.get_spellcheck_enabled().enabled is True
    assert reloaded.get_speaking_streaks_enabled().enabled is False


def test_shared_ui_settings_use_global_compare_and_swap_revision(tmp_path: Path) -> None:
    bundle = configuration_bundle(tmp_path)
    first = ManagedSettingsStore(
        bundle.main.managed_settings.state_directory,
        bundle.worlds,
        base_ui=bundle.main.ui,
    )
    stale = ManagedSettingsStore(
        bundle.main.managed_settings.state_directory,
        bundle.worlds,
        base_ui=bundle.main.ui,
    )

    first.set_spellcheck_enabled(True, 0)

    with pytest.raises(ManagedSettingsRevisionConflict):
        stale.set_speaking_streaks_enabled(False, 0)


async def test_gateway_runtime_uses_managed_world_configuration(tmp_path: Path) -> None:
    bundle = configuration_bundle(tmp_path)
    writer = ManagedSettingsStore(bundle.main.managed_settings.state_directory, bundle.worlds)
    writer.add_gag_pattern("alpha", "managed", 0)

    runtime = await GatewayRuntime.from_configuration(bundle)
    try:
        alpha = next(session for session in runtime.sessions if session.world == "alpha")
        assert alpha.config.gag.patterns == ("from base", "managed")
        assert runtime.managed_settings is not None
        assert runtime.managed_settings.revision == 1
    finally:
        await runtime.stop()


async def test_gateway_gag_mutation_persists_then_hot_swaps_policy(tmp_path: Path) -> None:
    runtime = await GatewayRuntime.from_configuration(configuration_bundle(tmp_path))
    hidden = Event(
        session_id=runtime.sessions[0].session_id,
        world="alpha",
        connection_generation=1,
        sequence=1,
        direction=Direction.INBOUND,
        kind=EventKind.RAW_OUTPUT,
        canonical_text="BING goes the bell",
        plain_text="BING goes the bell",
        display_text="BING goes the bell",
    )
    try:
        assert runtime.gag_policy is not None
        assert runtime.gag_policy.allows(hidden) is True

        result = await runtime.manage_gag(
            "alpha", "add", pattern=r"^bing", expected_revision=0
        )

        assert result["revision"] == 1
        assert result["restart_required"] is False
        assert runtime.gag_policy.allows(hidden) is False
        reloaded = ManagedSettingsStore(
            runtime.managed_settings.state_directory, worlds()  # type: ignore[union-attr]
        )
        assert reloaded.list_gag_patterns("alpha").patterns == ("from base", r"^bing")
    finally:
        await runtime.stop()


async def test_gateway_reports_restart_when_persisted_policy_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await GatewayRuntime.from_configuration(configuration_bundle(tmp_path))
    assert runtime.gag_policy is not None

    def fail_swap(_worlds: WorldsConfig) -> None:
        raise RuntimeError("unexpected swap failure")

    monkeypatch.setattr(runtime.gag_policy, "replace_configuration", fail_swap)
    try:
        result = await runtime.manage_gag(
            "alpha", "add", pattern="persisted", expected_revision=0
        )

        assert result["revision"] == 1
        assert result["restart_required"] is True
        assert runtime.managed_settings is not None
        assert runtime.managed_settings.list_gag_patterns("alpha").patterns == (
            "from base",
            "persisted",
        )
        assert any(
            entry.code == "runtime_swap_failed"
            for entry in runtime.plugins.diagnostics.entries()
        )
    finally:
        await runtime.stop()


def test_add_remove_reset_and_reload(tmp_path: Path) -> None:
    state = tmp_path / "settings"
    store = ManagedSettingsStore(state, worlds())

    assert store.list_gag_patterns("alpha").patterns == ("from base",)
    added = store.add_gag_pattern("alpha", "database saving", 0)
    assert added.changed is True
    assert added.revision == 1
    assert added.patterns == ("from base", "database saving")

    reloaded = ManagedSettingsStore(state, worlds())
    assert reloaded.list_gag_patterns("alpha").patterns == added.patterns
    removed = reloaded.remove_gag_pattern("alpha", "from base", 1)
    assert removed.patterns == ("database saving",)
    reset = reloaded.reset_gag_patterns("alpha", 2)
    assert reset.patterns == ("from base",)
    assert reset.revision == 3


def test_explicit_empty_override_differs_from_reset(tmp_path: Path) -> None:
    store = ManagedSettingsStore(tmp_path / "settings", worlds())

    removed = store.remove_gag_pattern("alpha", "from base", 0)
    assert removed.patterns == ()
    assert "alpha" in store.document.worlds
    assert store.effective_worlds.worlds["alpha"].gag.patterns == ()

    reset = store.reset_gag_patterns("alpha", 1)
    assert reset.patterns == ("from base",)
    assert "alpha" not in store.document.worlds


def test_noop_does_not_write_or_increment_revision(tmp_path: Path) -> None:
    store = ManagedSettingsStore(tmp_path / "settings", worlds())
    result = store.add_gag_pattern("alpha", "from base", 0)

    assert result.changed is False
    assert result.revision == 0
    assert not store.path.exists()


def test_stale_revision_and_unknown_world_are_rejected(tmp_path: Path) -> None:
    store = ManagedSettingsStore(tmp_path / "settings", worlds())
    store.add_gag_pattern("alpha", "one", 0)

    with pytest.raises(ManagedSettingsRevisionConflict):
        store.add_gag_pattern("alpha", "two", 0)
    with pytest.raises(ManagedSettingsError, match="unknown world"):
        store.add_gag_pattern("missing", "two", 1)


def test_revision_compare_and_swap_is_shared_across_store_instances(tmp_path: Path) -> None:
    state = tmp_path / "settings"
    first = ManagedSettingsStore(state, worlds())
    stale = ManagedSettingsStore(state, worlds())

    first.add_gag_pattern("alpha", "one", 0)
    with pytest.raises(ManagedSettingsRevisionConflict):
        stale.add_gag_pattern("alpha", "two", 0)

    assert ManagedSettingsStore(state, worlds()).list_gag_patterns("alpha").patterns == (
        "from base",
        "one",
    )


@pytest.mark.parametrize("pattern", ["", "x" * 513, "(unclosed"])
def test_invalid_patterns_are_rejected_without_a_write(tmp_path: Path, pattern: str) -> None:
    store = ManagedSettingsStore(tmp_path / "settings", worlds())

    with pytest.raises(ValueError):
        store.add_gag_pattern("alpha", pattern, 0)
    assert store.revision == 0
    assert not store.path.exists()


def test_private_atomic_files_and_bounded_previous_revision(tmp_path: Path) -> None:
    state = tmp_path / "settings"
    store = ManagedSettingsStore(state, worlds())
    store.add_gag_pattern("alpha", "one", 0)
    first = store.path.read_bytes()
    store.add_gag_pattern("alpha", "two", 1)

    assert store.previous_path.read_bytes() == first
    if os.name == "posix":
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.previous_path.stat().st_mode) == 0o600


def test_symlink_destination_and_directory_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("do not replace", encoding="utf-8")
    state = tmp_path / "settings"
    state.mkdir(mode=0o700)
    (state / "managed-overrides.json").symlink_to(target)

    with pytest.raises(ManagedSettingsError):
        ManagedSettingsStore(state, worlds())
    assert target.read_text(encoding="utf-8") == "do not replace"

    real_directory = tmp_path / "real"
    real_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(ManagedSettingsError):
        ManagedSettingsStore(linked_directory, worlds())


def test_oversized_and_unknown_world_documents_are_rejected(tmp_path: Path) -> None:
    state = tmp_path / "settings"
    state.mkdir(mode=0o700)
    path = state / "managed-overrides.json"
    path.write_bytes(b" " * (MAX_MANAGED_SETTINGS_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(ManagedSettingsError, match="maximum document size"):
        ManagedSettingsStore(state, worlds())

    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": 1,
                "worlds": {"missing": {"gag": {"patterns": ["one"]}}},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    with pytest.raises(ManagedSettingsError, match="unknown world"):
        ManagedSettingsStore(state, worlds())


def test_failed_persistence_leaves_memory_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ManagedSettingsStore(tmp_path / "settings", worlds())

    def fail(_document: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(OSError, match="disk full"):
        store.add_gag_pattern("alpha", "one", 0)
    assert store.revision == 0
    assert store.list_gag_patterns("alpha").patterns == ("from base",)


def test_audit_is_redacted(tmp_path: Path) -> None:
    audit: list[dict[str, object]] = []
    store = ManagedSettingsStore(tmp_path / "settings", worlds(), audit=audit.append)
    store.add_gag_pattern("alpha", "secret pattern", 0)

    assert audit == [
        {
            "operation": "add_gag_pattern",
            "world": "alpha",
            "revision": 1,
            "result": "committed",
        }
    ]
    assert "secret pattern" not in repr(audit)
