from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import DummyInput, Input, create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from tfr.agents import AgentInspection
from tfr.borders import BorderEdge
from tfr.config import (
    AgentsConfig,
    ConfigurationBundle,
    MainConfig,
    PluginSource,
    ThemeConfig,
    UpdateConfig,
    WorldCapabilitiesConfig,
    WorldConfig,
    WorldDefaults,
    WorldsConfig,
)
from tfr.core import CommandBus, EventBus
from tfr.events import Actor, ActorType, Direction, Event, EventKind, Provenance
from tfr.pager import PagerMode
from tfr.plugin_api import BorderFragment, ScreenClearContext, TextDecoration, TextEffectKind
from tfr.plugin_sources import PluginSourceNotice, PluginUpdateChecker, PluginUpdateResult
from tfr.plugins import BossViewEvent, PluginManager
from tfr.sessions import SessionManager, SessionState, WorldSession
from tfr.tui import (
    TfrTui,
    _format_elapsed,
    _multiline_paste_commands,
    _osc52_sequence,
    run_client,
)
from tfr.updates import BuildIdentity, UpdateChecker


def make_tui(
    *,
    input: Input | None = None,
    plugins: PluginManager | None = None,
    update_checker: UpdateChecker | None = None,
    plugin_update_checker: PluginUpdateChecker | None = None,
    gateway_build: BuildIdentity | None = None,
    theme: ThemeConfig | None = None,
    output_color: str | None = None,
    server: str = "generic",
    unicode: bool = False,
    world_aliases: Mapping[str, tuple[str, ...]] | None = None,
) -> TfrTui:
    event_bus = EventBus()
    command_bus = CommandBus()
    sessions = [
        WorldSession(
            world=alias,
            config=WorldConfig(
                host="localhost",
                port=4201,
                aliases=(world_aliases or {}).get(alias, ()),
                autoconnect=False,
                server=server,
                capabilities=WorldCapabilitiesConfig(unicode=unicode),
            ),
            defaults=WorldDefaults(),
            event_bus=event_bus,
            command_bus=command_bus,
        )
        for alias in ("alpha", "beta")
    ]
    return TfrTui(
        sessions=sessions,
        manager=SessionManager(sessions),
        event_bus=event_bus,
        command_bus=command_bus,
        scrollback_lines={"alpha": 100, "beta": 100},
        agent_worlds={"beta"},
        pager_enabled=True,
        pager_overlap=1,
        plugins=plugins,
        update_checker=update_checker,
        plugin_update_checker=plugin_update_checker,
        gateway_build=gateway_build,
        theme=theme,
        output_color=output_color,
        input=input or DummyInput(),
        output=DummyOutput(),
    )


def entry_point(name: str, plugin: object) -> SimpleNamespace:
    return SimpleNamespace(name=name, load=lambda: plugin)


def test_catppuccin_theme_applies_to_ui_output_and_notices() -> None:
    tui = make_tui(theme=ThemeConfig(preset="catppuccin-mocha"))

    assert tui.normal_root.style == "class:application"
    assert tui.theme.styles["application"] == "fg:#cdd6f4 bg:#1e1e2e"
    assert tui.active_view.display.default_style == "fg:#cdd6f4"

    tui.add_notice("alpha", "Themed notice")

    assert "#f9e2af" in tui.active_view.display.rows[-1][0][0]


def test_output_color_overrides_theme_plain_text_color() -> None:
    tui = make_tui(
        theme=ThemeConfig(preset="catppuccin-mocha"),
        output_color="#010203",
    )

    assert tui.active_view.display.default_style == "fg:#010203"


async def add_speaker_effects(
    tui: TfrTui,
    *,
    effect: TextEffectKind = TextEffectKind.CAPITALIZATION_ROLL,
    interval_seconds: float = 0.5,
    loop: bool = True,
    base_color: str = "#a9914a",
) -> None:
    class SpeakerFixture:
        api_version = 1

        def register(self, registrar: Any, _config: object) -> None:
            def decorate(event: Event, text: str) -> tuple[TextDecoration, ...]:
                sender = event.provenance.sender_name if event.provenance is not None else None
                if (
                    event.kind not in {EventKind.SAY, EventKind.POSE}
                    or sender is None
                    or sender.casefold() != "alice"
                ):
                    return ()
                if text[:5].casefold() != "alice":
                    return ()
                return (
                    TextDecoration(
                        start=0,
                        end=5,
                        effect=effect,
                        base_color=base_color,
                        accent_color="#e6c965",
                        interval_seconds=interval_seconds,
                        repeat_seconds=10,
                        loop=loop,
                    ),
                )

            registrar.register_display_decorator("speaker-fixture", decorate)  # type: ignore[attr-defined]

    tui.plugins = await PluginManager.load(
        enabled=("speaker-fixture",),
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=(entry_point("speaker-fixture", SpeakerFixture()),),
        scope="ui",
    )


async def add_animated_border(tui: TfrTui) -> None:
    class BorderFixture:
        api_version = 1

        def register(self, registrar: object, _config: object) -> None:
            registrar.register_border_effect(  # type: ignore[attr-defined]
                "border-fixture",
                lambda _context, fragment: BorderFragment(
                    fragment.character,
                    f"{fragment.style} reverse",
                ),
                frames_per_second=8,
            )

    tui.plugins = await PluginManager.load(
        enabled=("border-fixture",),
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=(entry_point("border-fixture", BorderFixture()),),
        scope="ui",
    )


async def add_screen_clear_effects(
    tui: TfrTui,
    names: tuple[str, ...] = ("clear-one",),
) -> None:
    class ClearFixture:
        api_version = 1

        def register(self, registrar: object, _config: object) -> None:
            def render(context: ScreenClearContext) -> tuple[tuple[str, str], ...]:
                return (("", "\n".join(context.lines)),)

            registrar.register_screen_clear_effect(  # type: ignore[attr-defined]
                render,
                duration_seconds=2.1,
                frames_per_second=24,
            )

    tui.plugins = await PluginManager.load(
        enabled=names,
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=tuple(entry_point(name, ClearFixture()) for name in names),
        scope="ui",
    )


def test_world_switching_preserves_drafts_and_marks_agents() -> None:
    tui = make_tui()
    tui.views["alpha"].input_buffer.text = "draft alpha"

    tui.switch_world("beta")
    tui.views["beta"].input_buffer.text = "draft beta"
    tui.switch_world("alpha")

    assert tui.views["alpha"].input_buffer.text == "draft alpha"
    assert tui.views["beta"].input_buffer.text == "draft beta"
    world_bar = fragment_list_to_text(tui.world_bar())
    assert "[H] alpha" in world_bar
    assert "[A] beta" in world_bar


def test_world_bar_entries_switch_worlds_on_left_click() -> None:
    tui = make_tui()
    beta_fragment = tui.world_bar()[1]
    assert len(beta_fragment) == 3

    beta_fragment[2](
        MouseEvent(
            position=Point(x=10, y=0),
            event_type=MouseEventType.MOUSE_UP,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )

    assert tui.active_alias == "beta"
    assert tui.application.layout.current_buffer is tui.views["beta"].input_buffer


def test_format_elapsed_uses_the_largest_reasonable_unit() -> None:
    assert _format_elapsed(0) == "0s"
    assert _format_elapsed(59) == "59s"
    assert _format_elapsed(60) == "1m"
    assert _format_elapsed(125) == "2m"
    assert _format_elapsed(3599) == "59m"
    assert _format_elapsed(3600) == "1h"
    assert _format_elapsed(86399) == "23h"
    assert _format_elapsed(86400) == "1d"
    assert _format_elapsed(-5) == "0s"


def test_world_bar_shows_no_activity_indicator_before_any_inbound_event() -> None:
    tui = make_tui()

    assert tui.active_view.last_inbound_at is None
    assert "(" not in fragment_list_to_text(tui.world_bar())


def test_world_bar_shows_elapsed_time_since_last_inbound_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: 1000.0)
    tui = make_tui()
    tui.handle_event(
        Event(
            session_id=tui.active_view.session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.INBOUND,
            kind=EventKind.RAW_OUTPUT,
            canonical_text="hello",
            plain_text="hello",
            display_text="hello",
        )
    )
    assert tui.active_view.last_inbound_at == 1000.0

    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: 1125.0)

    assert "[H] alpha (2m)" in fragment_list_to_text(tui.world_bar())


def test_activity_indicator_ignores_outbound_command_events() -> None:
    tui = make_tui()

    tui.handle_event(
        Event(
            session_id=tui.active_view.session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.OUTBOUND,
            kind=EventKind.COMMAND,
            canonical_text="look",
            plain_text="look",
            display_text="look",
            actor=Actor(ActorType.HUMAN, "me"),
        )
    )

    assert tui.active_view.last_inbound_at is None


async def test_activity_ticker_runs_once_the_ui_starts_animating() -> None:
    tui = make_tui()
    tui._animations_started = True
    tui._sync_animation_task()

    assert tui._activity_ticker_task is not None
    task = tui._activity_ticker_task
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_activity_ticker_stops_under_low_bandwidth_and_boss_mode() -> None:
    tui = make_tui()
    tui._animations_started = True
    tui._sync_animation_task()
    assert tui._activity_ticker_task is not None

    await tui._handle_client_command("alpha", "/lowbw on")
    assert tui._activity_ticker_task is None

    await tui._handle_client_command("alpha", "/lowbw off")
    assert tui._activity_ticker_task is not None

    await tui.activate_boss()
    assert tui._activity_ticker_task is None


def test_dragging_output_selects_highlights_and_copies_plain_text() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=20, height=5)
    view.display.append("\x1b[31mfirst\x1b[0m\nsecond")
    copied: list[str] = []
    view._copy_handler = copied.append

    # The pane is 5 rows tall but only 2 rows are buffered, so the real
    # content is bottom-anchored at pane rows 3-4 (3 blank rows padded
    # above); y=3/y=4 below target "first"/"second" respectively.
    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=1, y=3),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )
    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=2, y=4),
            event_type=MouseEventType.MOUSE_MOVE,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )
    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=2, y=4),
            event_type=MouseEventType.MOUSE_UP,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )

    assert copied == ["irst\nsec"]
    selected = [text for style, text, *_ in view.output_text() if "class:selection" in style]
    assert "".join(selected) == "irstsec"


def test_click_without_drag_does_not_copy_text() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.append("text")
    copied: list[str] = []
    view._copy_handler = copied.append
    for event_type in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
        view.handle_output_mouse(
            MouseEvent(
                position=Point(x=1, y=0),
                event_type=event_type,
                button=MouseButton.LEFT,
                modifiers=frozenset(),
            )
        )

    assert copied == []
    assert not any("class:selection" in style for style, _text, *_ in view.output_text())


def test_click_on_a_url_opens_it_without_copying() -> None:
    tui = make_tui()
    view = tui.active_view
    text = "see http://example.com now"
    view.display.resize(width=80, height=1)
    view.display.append(text)
    copied: list[str] = []
    view._copy_handler = copied.append
    opened: list[str] = []
    view._open_url_handler = opened.append
    column = text.index("http://") + 1

    for event_type in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
        view.handle_output_mouse(
            MouseEvent(
                position=Point(x=column, y=0),
                event_type=event_type,
                button=MouseButton.LEFT,
                modifiers=frozenset(),
            )
        )

    assert opened == ["http://example.com"]
    assert copied == []


def test_click_outside_a_url_does_not_open_anything() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=80, height=1)
    view.display.append("see http://example.com now")
    opened: list[str] = []
    view._open_url_handler = opened.append

    for event_type in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
        view.handle_output_mouse(
            MouseEvent(
                position=Point(x=0, y=0),
                event_type=event_type,
                button=MouseButton.LEFT,
                modifiers=frozenset(),
            )
        )

    assert opened == []


def test_dragging_over_a_url_copies_text_instead_of_opening_it() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=40, height=5)
    text = "see http://example.com now"
    view.display.append(text)
    copied: list[str] = []
    view._copy_handler = copied.append
    opened: list[str] = []
    view._open_url_handler = opened.append
    start = text.index("http://")

    # The pane is 5 rows tall but only 1 row is buffered, so the real
    # content is bottom-anchored at pane row 4 (4 blank rows padded above).
    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=start, y=4),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )
    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=start + 5, y=4),
            event_type=MouseEventType.MOUSE_MOVE,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )
    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=start + 5, y=4),
            event_type=MouseEventType.MOUSE_UP,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )

    assert opened == []
    assert copied == ["http:/"]


def test_url_text_is_rendered_with_an_underline() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.append("see http://example.com now")

    underlined = "".join(text for style, text, *_ in view.output_text() if "underline" in style)

    assert underlined == "http://example.com"


def test_osc52_clipboard_sequence_contains_utf8_selection() -> None:
    sequence = _osc52_sequence("hello π")

    assert sequence.startswith("\x1b]52;c;")
    assert sequence.endswith("\x07")
    payload = sequence.removeprefix("\x1b]52;c;").removesuffix("\x07")
    assert base64.b64decode(payload).decode("utf-8") == "hello π"


async def test_help_lists_commands_keybindings_markers_and_loaded_plugins() -> None:
    tui = make_tui()
    tui.active_view.display.resize(width=80, height=100)

    class HelpFixture:
        def register(self, registrar: object, _config: object) -> None:
            registrar.register_command(  # type: ignore[attr-defined]
                "fixture",
                lambda _context, _arguments: None,
                help="run the harmless fixture",
            )

    tui.plugins = await PluginManager.load(
        enabled=("fixture",),
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=(entry_point("fixture", HelpFixture()),),
    )

    await tui._handle_client_command("alpha", "/help")

    help_text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "/boss (tfr.boss) - open or configure" in help_text
    assert "/sh - temporarily open" in help_text
    assert "! command" in help_text
    assert "/nospoof show|hide|status" in help_text
    assert "/recall X" in help_text
    assert "/update status|check" in help_text
    assert "/plugins - show configured" in help_text
    assert "[H] human-operated world" in help_text
    assert "left-click selects a world" in help_text
    assert "/fixture (fixture) - run the harmless fixture" in help_text


async def test_plugins_command_reports_loaded_and_missing_plugins() -> None:
    tui = make_tui()

    class Fixture:
        def register(self, registrar: object, _config: object) -> None:
            registrar.register_command(  # type: ignore[attr-defined]
                "fixture", lambda _context, _arguments: None
            )

    tui.plugins = await PluginManager.load(
        enabled=("fixture", "gag"),
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=(entry_point("fixture", Fixture()),),
    )

    await tui._handle_client_command("alpha", "/plugins")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "fixture: loaded" in text
    assert "gag: failed (no tfr.plugins.v1 entry point named gag)" in text


async def test_plugins_command_does_not_expose_registration_error_details() -> None:
    tui = make_tui()

    class BrokenFixture:
        def register(self, _registrar: object, _config: object) -> None:
            raise ValueError("secret-token-value")

    tui.plugins = await PluginManager.load(
        enabled=("broken",),
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=(entry_point("broken", BrokenFixture()),),
    )

    await tui._handle_client_command("alpha", "/plugins")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "broken: failed (ValueError)" in text
    assert "secret-token-value" not in text


async def test_update_check_reports_ui_and_gateway_versions(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 1,
        "project": "tfr",
        "channel": "stable",
        "version": "1.2.3",
        "tag": "v1.2.3",
        "commit": "a" * 40,
        "protocol": {"minimum": 1, "maximum": 1},
        "release_url": "https://github.com/Blaag/tfr/releases/tag/v1.2.3",
        "artifact": {
            "url": "https://github.com/Blaag/tfr/releases/download/v1.2.3/tfr.whl",
            "size": 10,
            "sha256": "b" * 64,
        },
    }

    def fetch(_url: str, _etag: str | None, _timeout: float) -> object:
        import json

        return SimpleNamespace(content=json.dumps(manifest).encode(), etag=None)

    checker = UpdateChecker(
        UpdateConfig(state_directory=tmp_path),
        build=BuildIdentity("1.0.0", "c" * 40),
        fetch=fetch,  # type: ignore[arg-type]
    )
    plugin_source = PluginSource(
        repo="owner/plugins",
        policy="stable-notify",
        manifest_url="https://example.invalid/plugin-manifest.json",
    )
    plugin_result = PluginUpdateResult(
        repo=plugin_source.repo,
        policy=plugin_source.policy,
        checked_at=datetime.now(UTC),
        current_version="0.4.0",
        latest_version="0.5.0",
        release_url="https://example.invalid/releases/v0.5.0",
    )
    plugin_checker = PluginUpdateChecker(
        (plugin_source,),
        plugins_directory=tmp_path / "plugins",
        config=checker.config,
        check_source=lambda _source, _directory, _timeout: plugin_result,
    )
    tui = make_tui(
        update_checker=checker,
        plugin_update_checker=plugin_checker,
        gateway_build=BuildIdentity("1.1.0", "d" * 40),
    )
    tui.active_view.display.resize(width=100, height=20)

    await tui._handle_client_command("alpha", "/update check")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Checking for stable TFR and plugin updates" in text
    assert "UI update available: 1.2.3 (running 1.0.0" in text
    assert "Gateway update available: 1.2.3 (running 1.1.0" in text
    assert "Plugin update available: owner/plugins 0.5.0 (current 0.4.0)" in text


async def test_restart_is_available_only_for_gateway_attached_ui() -> None:
    tui = make_tui()

    await tui._handle_client_command("alpha", "/restart")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "only for a gateway-attached UI" in text


async def test_reload_is_an_alias_for_restart() -> None:
    tui = make_tui()

    await tui._handle_client_command("alpha", "/reload")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "only for a gateway-attached UI" in text


async def test_recall_appends_retained_rows_after_a_screen_clear() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=40, height=10)
    view.display.append("first")
    view.display.append("second")
    view.display.append("third")
    view.display.clear_screen()

    await tui._handle_client_command("alpha", "/recall 2")

    rows = view.display.visible_rows()
    assert fragment_list_to_text(view.display.formatted_text()) == "-- Recall 2\nsecond\nthird"
    assert "ansiyellow" in rows[0][0][0]


async def test_recall_output_is_anchored_to_the_bottom_of_the_pane() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=40, height=10)
    view.display.append("first")
    view.display.append("second")
    view.display.append("third")
    view.display.clear_screen()

    await tui._handle_client_command("alpha", "/recall 2")

    lines = fragment_list_to_text(view.output_text()).split("\n")
    assert lines == ["", "", "", "", "", "", "", "-- Recall 2", "second", "third"]


async def test_recall_replays_speaker_and_terminal_reveal_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: 50.0)
    tui = make_tui()
    tui._animation_epoch = 20
    view = tui.active_view
    view.display.resize(width=40, height=2)
    speaker = TextDecoration(
        start=0,
        end=5,
        effect=TextEffectKind.CAPITALIZATION_ROLL,
        base_color="#a9914a",
        accent_color="#e6c965",
        interval_seconds=0.5,
    )
    reveal = TextDecoration(
        start=0,
        end=11,
        effect=TextEffectKind.TERMINAL_REVEAL,
        base_color="#d7ff5f",
        accent_color="#d7ff5f",
        interval_seconds=0.1,
        frames_per_second=20,
        loop=False,
        glitch_characters="#",
    )
    view.display.append("Alice waves", decorations=(speaker, reveal))
    await tui._handle_client_command("alpha", "/recall 1")

    replayed = view.display._entry_decorations[-1]
    assert {decoration.effect for decoration in replayed} == {
        TextEffectKind.CAPITALIZATION_ROLL,
        TextEffectKind.TERMINAL_REVEAL,
    }
    assert all(decoration.cycle_phase(30) == pytest.approx(0) for decoration in replayed)
    visible = view.display.visible_rows(elapsed_seconds=30, animations_enabled=True)
    assert fragment_list_to_text(list(visible[-1])) == " " * len("Alice waves")


async def test_output_after_a_clear_is_anchored_to_the_bottom_of_the_pane() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=40, height=6)
    view.display.append("old text")
    view.display.clear_screen()

    view.display.append("hello")

    lines = fragment_list_to_text(view.output_text()).split("\n")
    assert lines == ["", "", "", "", "", "hello"]


def test_dragging_within_blank_padding_selects_nothing() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=20, height=5)
    view.display.append("only one line")

    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )
    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=3, y=1),
            event_type=MouseEventType.MOUSE_MOVE,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )
    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=3, y=1),
            event_type=MouseEventType.MOUSE_UP,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )

    assert not any("class:selection" in style for style, _text, *_ in view.output_text())


async def test_recall_does_not_recall_previous_recall_blocks() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=40, height=10)
    view.display.append("source line")

    await tui._handle_client_command("alpha", "/recall 1")
    await tui._handle_client_command("alpha", "/recall 1")

    assert fragment_list_to_text(view.display.formatted_text()).endswith("-- Recall 1\nsource line")
    recalled = view.display.recent_rows(1)
    assert fragment_list_to_text(list(recalled[0])) == "source line"


async def test_recall_uses_only_the_requested_world_history() -> None:
    tui = make_tui()
    tui.views["alpha"].display.append("alpha line")
    tui.views["beta"].display.append("beta line")

    await tui._handle_client_command("beta", "/recall 1")

    beta_text = fragment_list_to_text(tui.views["beta"].display.formatted_text())
    assert beta_text.endswith("-- Recall 1\nbeta line")
    assert "alpha line" not in beta_text


async def test_recall_excludes_local_client_command_notices() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.append("world output")

    await tui._handle_client_command("alpha", "/help")
    await tui._handle_client_command("alpha", "/recall 1")

    text = fragment_list_to_text(view.display.formatted_text())
    assert text.endswith("-- Recall 1\nworld output")
    assert "TFR commands" not in text.rsplit("-- Recall 1", 1)[-1]


@pytest.mark.parametrize("command", ["/recall", "/recall two", "/recall 1 2"])
async def test_recall_rejects_invalid_arguments(command: str) -> None:
    tui = make_tui()

    await tui._handle_client_command("alpha", command)

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Usage: /recall X" in text


async def test_recall_rejects_non_positive_counts() -> None:
    tui = make_tui()

    await tui._handle_client_command("alpha", "/recall 0")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "positive integer" in text


async def test_gateway_reconnect_command_preserves_the_running_ui() -> None:
    tui = make_tui()
    calls = 0

    async def reconnect() -> str:
        nonlocal calls
        calls += 1
        return "Reconnected to Gateway; restored 3 missed events"

    tui.gateway_reconnect = reconnect

    await tui._handle_client_command("alpha", "/gateway reconnect")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert calls == 1
    assert "Reconnecting to Gateway" in text
    assert "restored 3 missed events" in text
    assert tui.restart_requested is False


async def test_n_and_p_are_shortcuts_for_next_and_previous_world() -> None:
    tui = make_tui()
    assert tui.active_alias == "alpha"

    await tui._handle_client_command("alpha", "/n")
    assert tui.active_alias == "beta"

    await tui._handle_client_command("beta", "/p")
    assert tui.active_alias == "alpha"


async def test_world_switch_aliases_validate_arguments_and_switch_worlds() -> None:
    tui = make_tui(world_aliases={"beta": ("B",)})

    await tui._handle_client_command("alpha", "/b extra")
    assert tui.active_alias == "alpha"
    assert "Usage: /b" in fragment_list_to_text(tui.active_view.display.formatted_text())

    await tui._handle_client_command("alpha", "/B")
    assert tui.active_alias == "beta"


async def test_core_and_plugin_commands_take_priority_over_world_aliases() -> None:
    calls: list[str] = []

    class AliasFixture:
        def register(self, registrar: object, _config: object) -> None:
            registrar.register_command(  # type: ignore[attr-defined]
                "fixture",
                lambda context, _arguments: calls.append(context.world),
                help="run the fixture",
            )

    event_bus = EventBus()
    command_bus = CommandBus()
    plugins = await PluginManager.load(
        enabled=("fixture",),
        config={},
        event_bus=event_bus,
        command_bus=command_bus,
        targets={},
        discovered=(entry_point("fixture", AliasFixture()),),
        scope="ui",
    )
    tui = make_tui(
        plugins=plugins,
        world_aliases={"alpha": ("n", "fixture"), "beta": ("b",)},
    )

    await tui._handle_client_command("alpha", "/n")
    assert tui.active_alias == "beta"

    tui.switch_world("alpha")
    await tui._handle_client_command("alpha", "/fixture")
    assert tui.active_alias == "alpha"
    assert calls == ["alpha"]

    help_text = tui.help_text()
    assert "/b - switch to beta" in help_text
    assert "/n - switch to alpha; shadowed by core command" in help_text
    assert "/fixture - switch to alpha; shadowed by fixture plugin command" in help_text
    assert {world for world, _text in tui._startup_notices} == {"alpha", "beta"}
    assert all(
        "World-switch aliases shadowed by commands" in text
        for _world, text in tui._startup_notices
    )


async def test_nospoof_command_toggles_prefix_visibility() -> None:
    tui = make_tui()
    session = tui.active_view.session
    assert session.show_nospoof_prefix is False

    await tui._handle_client_command("alpha", "/nospoof show")
    assert session.show_nospoof_prefix is True

    await tui._handle_client_command("alpha", "/nospoof hide")
    assert session.show_nospoof_prefix is False


async def test_low_bandwidth_command_toggles_ui_animation() -> None:
    tui = make_tui()

    await tui._handle_client_command("alpha", "/lowbw on")

    assert tui.low_bandwidth is True
    assert "LOWBW" in fragment_list_to_text(tui.status_bar())

    await tui._handle_client_command("alpha", "/lowbw off")

    assert tui.low_bandwidth is False
    assert "LOWBW" not in fragment_list_to_text(tui.status_bar())


async def test_animations_command_removes_and_restores_border_effects() -> None:
    tui = make_tui()
    await add_animated_border(tui)

    assert any(
        "reverse" in style
        for style, _text, *_ in tui._border_text(tui.active_view, "output", BorderEdge.TOP, 2)
    )

    await tui._handle_client_command("alpha", "/animations off")

    assert not any(
        "reverse" in style
        for style, _text, *_ in tui._border_text(tui.active_view, "output", BorderEdge.TOP, 2)
    )
    assert "ANIM OFF" in fragment_list_to_text(tui.status_bar())

    await tui._handle_client_command("alpha", "/animations on")

    assert any(
        "reverse" in style
        for style, _text, *_ in tui._border_text(tui.active_view, "output", BorderEdge.TOP, 2)
    )


async def test_animations_command_stops_and_restarts_continuous_scheduler() -> None:
    tui = make_tui()
    await add_animated_border(tui)
    tui._animations_started = True

    try:
        tui._sync_animation_task()
        first_task = tui._animation_task
        assert first_task is not None

        await tui._handle_client_command("alpha", "/animations off")
        await asyncio.sleep(0)
        assert first_task.cancelled()
        assert tui._animation_task is None

        await tui._handle_client_command("alpha", "/animations on")
        assert tui._animation_task is not None
        assert tui._animation_task is not first_task
    finally:
        tui._animations_started = False
        tui._sync_animation_task()


async def test_screen_clear_plugin_overlays_snapshot_then_reveals_new_output() -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui)
    tui.animations_enabled = False
    view = tui.active_view
    view.display.resize(width=20, height=4)
    view.display.append("old text")

    tui.start_screen_clear("alpha")

    task = tui._screen_clear_task
    assert task is not None
    assert tui._screen_clear_world == "alpha"
    assert "old text" in fragment_list_to_text(tui.screen_clear_text())
    assert view.display.screen_is_cleared is True

    view.display.append("new text")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tui._screen_clear_world is None
    assert fragment_list_to_text(view.display.formatted_text()) == "new text"


async def test_low_bandwidth_clears_immediately_without_scheduling_screen_effect() -> None:
    tui = make_tui()
    rendered = False

    class ClearFixture:
        def register(self, registrar: object, _config: object) -> None:
            def render(_context: ScreenClearContext) -> tuple[tuple[str, str], ...]:
                nonlocal rendered
                rendered = True
                return (("", "animated"),)

            registrar.register_screen_clear_effect(  # type: ignore[attr-defined]
                render,
                duration_seconds=1,
                frames_per_second=20,
            )

    tui.plugins = await PluginManager.load(
        enabled=("clear-fixture",),
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=(entry_point("clear-fixture", ClearFixture()),),
        scope="ui",
    )
    tui._set_low_bandwidth(True)
    tui.active_view.display.append("clear me")

    tui.start_screen_clear("alpha")

    assert tui.active_view.display.screen_is_cleared is True
    assert tui._screen_clear_task is None
    assert tui._screen_clear_world is None
    assert tui._screen_clear_plugin is None
    assert rendered is False


async def test_enabling_low_bandwidth_cancels_active_screen_clear() -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui)
    tui.active_view.display.append("clear me")
    tui.start_screen_clear("alpha")
    task = tui._screen_clear_task
    assert task is not None

    tui._set_low_bandwidth(True)
    await asyncio.sleep(0)

    assert task.cancelled()
    assert tui._screen_clear_task is None
    assert tui._screen_clear_world is None
    assert tui._screen_clear_plugin is None


async def test_screen_clear_context_includes_visible_text_styles() -> None:
    tui = make_tui()
    captured: list[ScreenClearContext] = []

    class StyledClearFixture:
        def register(self, registrar: object, _config: object) -> None:
            def render(context: ScreenClearContext) -> tuple[tuple[str, str], ...]:
                captured.append(context)
                return (("", ""),)

            registrar.register_screen_clear_effect(  # type: ignore[attr-defined]
                render,
                duration_seconds=1,
                frames_per_second=20,
            )

    tui.plugins = await PluginManager.load(
        enabled=("styled-clear",),
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=(entry_point("styled-clear", StyledClearFixture()),),
        scope="ui",
    )
    view = tui.active_view
    view.display.resize(width=20, height=2)
    view.display.append("\x1b[31mred\x1b[0m plain")

    tui.start_screen_clear("alpha")
    task = tui._screen_clear_task
    assert task is not None
    tui.screen_clear_text()

    assert captured[0].lines == ("", "red plain")
    assert captured[0].styled_lines[0] == ()
    assert "ansired" in captured[0].styled_lines[1][0][0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_screen_clear_after_recall_uses_the_full_pane_as_its_floor() -> None:
    tui = make_tui()
    captured: list[ScreenClearContext] = []

    class CapturingClearFixture:
        def register(self, registrar: object, _config: object) -> None:
            def render(context: ScreenClearContext) -> tuple[tuple[str, str], ...]:
                captured.append(context)
                return (("", "\n".join(context.lines)),)

            registrar.register_screen_clear_effect(  # type: ignore[attr-defined]
                render,
                duration_seconds=1,
                frames_per_second=20,
            )

    tui.plugins = await PluginManager.load(
        enabled=("capturing-clear",),
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=(entry_point("capturing-clear", CapturingClearFixture()),),
        scope="ui",
    )
    view = tui.active_view
    view.display.resize(width=40, height=10)
    view.display.append("first")
    view.display.append("second")
    view.display.append("third")
    view.display.clear_screen()

    await tui._handle_client_command("alpha", "/recall 2")
    tui.start_screen_clear("alpha")
    task = tui._screen_clear_task
    assert task is not None
    tui.screen_clear_text()

    # /recall left only 3 buffered rows ("-- Recall 2", "second", "third") in
    # a 10-row pane. The animation's geometry must still span the pane's
    # true height, with the recalled text anchored at the bottom -- not
    # shrink to only the buffered rows, which would put the animation's
    # floor at the last recalled row instead of the pane's actual bottom.
    assert len(captured[0].lines) == 10
    assert captured[0].lines[:7] == ("",) * 7
    assert captured[0].lines[7:] == ("-- Recall 2", "second", "third")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_page_up_ends_an_active_screen_clear_before_scrolling() -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui)
    tui.animations_enabled = False
    view = tui.active_view
    view.display.append("first")
    tui.start_screen_clear("alpha")
    assert tui._screen_clear_task is not None
    assert view.display.screen_is_cleared is True

    page_up = next(
        binding for binding in tui.application.key_bindings.bindings if Keys.PageUp in binding.keys
    )
    page_up.handler(SimpleNamespace(app=tui.application))

    assert tui._screen_clear_task is None
    assert tui._screen_clear_world is None
    # The requested action (revealing pre-clear scrollback) still happened,
    # rather than being silently absorbed by the still-active clear overlay.
    assert view.display.screen_is_cleared is False


async def test_page_down_ends_an_active_screen_clear() -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui)
    tui.animations_enabled = False
    tui.active_view.display.append("first")
    tui.start_screen_clear("alpha")
    assert tui._screen_clear_task is not None

    page_down = next(
        binding
        for binding in tui.application.key_bindings.bindings
        if Keys.PageDown in binding.keys
    )
    page_down.handler(SimpleNamespace(app=tui.application))

    assert tui._screen_clear_task is None
    assert tui._screen_clear_world is None


async def test_jump_to_end_key_ends_an_active_screen_clear() -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui)
    tui.animations_enabled = False
    tui.active_view.display.append("first")
    tui.start_screen_clear("alpha")
    assert tui._screen_clear_task is not None

    end_key = next(
        binding for binding in tui.application.key_bindings.bindings if Keys.End in binding.keys
    )
    end_key.handler(SimpleNamespace(app=tui.application))

    assert tui._screen_clear_task is None
    assert tui._screen_clear_world is None


async def test_end_command_ends_an_active_screen_clear() -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui)
    tui.animations_enabled = False
    tui.active_view.display.append("first")
    tui.start_screen_clear("alpha")
    assert tui._screen_clear_task is not None

    await tui._handle_client_command("alpha", "/end")

    assert tui._screen_clear_task is None
    assert tui._screen_clear_world is None


async def test_switching_worlds_ends_an_active_screen_clear() -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui)
    tui.animations_enabled = False
    tui.active_view.display.append("first")
    tui.start_screen_clear("alpha")
    task = tui._screen_clear_task
    assert task is not None

    tui.switch_world("beta")

    assert tui._screen_clear_task is None
    assert tui._screen_clear_world is None
    assert task.cancelling()


async def test_paging_does_not_end_a_screen_clear_started_for_an_inactive_world() -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui)
    tui.animations_enabled = False
    tui.views["beta"].display.append("background world text")
    tui.start_screen_clear("beta")
    assert tui._screen_clear_world == "beta"
    task = tui._screen_clear_task
    assert task is not None
    page_up = next(
        binding for binding in tui.application.key_bindings.bindings if Keys.PageUp in binding.keys
    )
    page_up.handler(SimpleNamespace(app=tui.application))

    assert tui._screen_clear_world == "beta"
    assert tui._screen_clear_task is task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_screen_clear_waits_for_plugin_completion_after_duration() -> None:
    tui = make_tui()
    complete = False

    class CompletingClearFixture:
        def register(self, registrar: object, _config: object) -> None:
            registrar.register_screen_clear_effect(  # type: ignore[attr-defined]
                lambda context: (("", "\n".join(context.lines)),),
                duration_seconds=0.01,
                frames_per_second=30,
                is_complete=lambda: complete,
            )

    tui.plugins = await PluginManager.load(
        enabled=("completing-clear",),
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=(entry_point("completing-clear", CompletingClearFixture()),),
        scope="ui",
    )
    tui.active_view.display.append("wait for me")

    tui.start_screen_clear("alpha")
    task = tui._screen_clear_task
    assert task is not None
    await asyncio.sleep(0.05)

    assert task.done() is False
    assert tui._screen_clear_world == "alpha"

    complete = True
    await asyncio.wait_for(task, timeout=0.1)
    await asyncio.sleep(0)

    assert tui._screen_clear_world is None


async def test_screen_clear_selection_can_cycle_randomize_or_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui, ("clear-one", "clear-two"))

    assert tui._select_screen_clear_plugin() == "clear-one"
    assert tui._select_screen_clear_plugin() == "clear-two"

    monkeypatch.setattr("tfr.tui.secrets.choice", lambda values: values[-1])
    await tui._handle_client_command("alpha", "/clear random")
    assert tui._select_screen_clear_plugin() == "clear-two"

    await tui._handle_client_command("alpha", "/clear lock clear-one")
    assert tui._select_screen_clear_plugin() == "clear-one"


async def test_screen_clear_cycle_continues_after_selected_effect_is_removed() -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui, ("clear-one", "clear-two"))

    assert tui._select_screen_clear_plugin() == "clear-one"
    assert tui.plugins is not None
    tui.plugins.registry.screen_clear_effects.pop("clear-one")

    assert tui._select_screen_clear_plugin() == "clear-two"


async def test_screen_clear_lock_matches_entry_point_names_case_insensitively() -> None:
    tui = make_tui()
    await add_screen_clear_effects(tui, ("Flame.Clear",))

    await tui._handle_client_command("alpha", "/clear lock flame.clear")

    assert tui.screen_clear_effect == "Flame.Clear"
    assert tui._select_screen_clear_plugin() == "Flame.Clear"


async def test_invalid_screen_clear_effect_ends_overlay_immediately() -> None:
    tui = make_tui()

    class InvalidClearFixture:
        def register(self, registrar: object, _config: object) -> None:
            registrar.register_screen_clear_effect(  # type: ignore[attr-defined]
                lambda _context: (("", "too wide"),),
                duration_seconds=10,
                frames_per_second=1,
            )

    tui.plugins = await PluginManager.load(
        enabled=("invalid-clear",),
        config={},
        event_bus=tui.event_bus,
        command_bus=tui.command_bus,
        targets={},
        discovered=(entry_point("invalid-clear", InvalidClearFixture()),),
        scope="ui",
    )
    view = tui.active_view
    view.display.resize(width=2, height=1)
    view.display.append("ok")

    tui.start_screen_clear("alpha")
    assert tui.screen_clear_text() == []

    assert tui._screen_clear_task is None
    assert tui._screen_clear_world is None
    assert tui._screen_clear_plugin is None


async def test_standalone_client_loads_all_plugin_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tfr.gateway import GatewayRuntime

    runtime = SimpleNamespace(
        sessions=[],
        manager=object(),
        event_bus=EventBus(),
        command_bus=CommandBus(),
        plugins=object(),
        agents=object(),
        plugin_source_messages=("Plugin source owner/plugins: update available",),
        plugin_source_notices=(
            PluginSourceNotice(
                repo="owner/plugins",
                message="update available",
                available_version="0.2.0",
            ),
        ),
    )
    received_scope: str | None = None

    async def from_configuration(
        _cls: type[GatewayRuntime],
        _bundle: object,
        *,
        plugin_scope: str,
    ) -> object:
        nonlocal received_scope
        received_scope = plugin_scope
        return runtime

    values: dict[str, object] = {}
    notices: list[tuple[str, str]] = []

    class FakeTui:
        def __init__(self, **received: object) -> None:
            values.update(received)
            self.active_alias = "alpha"

        def queue_startup_notice(self, alias: str, message: str) -> None:
            notices.append((alias, message))

        async def run(self) -> int:
            return 17

    monkeypatch.setattr(GatewayRuntime, "from_configuration", classmethod(from_configuration))
    monkeypatch.setattr("tfr.tui.TfrTui", FakeTui)
    screen_clear = SimpleNamespace(mode="cycle", effect=None)
    boss = SimpleNamespace(mode="cycle", screen=None)
    ui = SimpleNamespace(
        pager=SimpleNamespace(enabled=True, overlap_lines=1),
        recent_input_lines=3,
        animations_enabled=True,
        low_bandwidth=False,
        output_color="#d7d7d7",
        theme=ThemeConfig(),
        screen_clear=screen_clear,
        boss=boss,
    )
    bundle = SimpleNamespace(
        main=SimpleNamespace(
            ui=ui,
            updates=UpdateConfig(enabled=False),
            plugins=SimpleNamespace(
                sources=(
                    PluginSource(
                        repo="owner/plugins",
                        policy="stable-notify",
                        manifest_url="https://example.invalid/plugin-manifest.json",
                    ),
                ),
                state_directory=Path("plugins"),
            ),
        ),
        worlds=SimpleNamespace(worlds={}),
        agents=SimpleNamespace(agents={}),
    )

    assert await run_client(bundle) == 17  # type: ignore[arg-type]
    assert received_scope == "all"
    assert notices == [("alpha", "Plugin source owner/plugins: update available")]
    checker = values["plugin_update_checker"]
    assert isinstance(checker, PluginUpdateChecker)
    assert ("owner/plugins", "0.2.0") in checker._notified


async def test_low_bandwidth_stops_and_resumes_border_scheduler() -> None:
    tui = make_tui()
    await add_animated_border(tui)
    tui._animations_started = True

    try:
        tui._sync_animation_task()
        first_task = tui._animation_task
        assert first_task is not None

        await tui._handle_client_command("alpha", "/lowbw on")
        await asyncio.sleep(0)
        assert first_task.cancelled()
        assert tui._animation_task is None

        await tui._handle_client_command("alpha", "/lowbw off")
        assert tui._animation_task is not None
        assert tui._animation_task is not first_task
    finally:
        tui._animations_started = False
        tui._sync_animation_task()


def test_low_bandwidth_freezes_and_resumes_animation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 10.0
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: now)
    tui = make_tui()

    now = 14.0
    tui._set_low_bandwidth(True)
    now = 30.0
    assert tui._animation_elapsed_seconds() == 4.0

    tui._set_low_bandwidth(False)
    now = 31.0
    assert tui._animation_elapsed_seconds() == 5.0


async def test_speaker_effects_animate_visually_but_keep_stored_text() -> None:
    tui = make_tui()
    await add_speaker_effects(tui)
    tui.active_view.display.resize(width=80, height=1)
    event = Event(
        session_id=tui.active_view.session.session_id,
        world="alpha",
        connection_generation=1,
        sequence=0,
        direction=Direction.INBOUND,
        kind=EventKind.SAY,
        canonical_text="Alice says, hello",
        plain_text="Alice says, hello",
        display_text="Alice says, hello",
        provenance=Provenance(sender_name="Alice"),
    )

    tui.handle_event(event)
    decoration = tui.active_view.display._entry_decorations[0][0]
    tui._border_frame_elapsed = 0.6 - decoration.phase_offset_seconds

    assert fragment_list_to_text(tui.active_view.output_text()).startswith("aLice")
    assert fragment_list_to_text(tui.active_view.display.formatted_text()).startswith("Alice")


async def test_speaker_effects_keep_static_color_when_animations_are_off() -> None:
    tui = make_tui()
    await add_speaker_effects(tui)
    tui.active_view.display.resize(width=80, height=1)
    tui.animations_enabled = False
    event = Event(
        session_id=tui.active_view.session.session_id,
        world="alpha",
        connection_generation=1,
        sequence=0,
        direction=Direction.INBOUND,
        kind=EventKind.POSE,
        canonical_text="aLiCe waves.",
        plain_text="aLiCe waves.",
        display_text="aLiCe waves.",
        provenance=Provenance(sender_name="alice"),
    )

    tui.handle_event(event)
    output = tui.active_view.output_text()

    assert fragment_list_to_text(output).startswith("aLiCe")
    assert output[0][:2] == ("fg:#a9914a", "aLiCe")


async def test_speaker_effect_cycles_are_anchored_to_event_arrival(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: 50.0)
    monkeypatch.setattr("tfr.tui.time.time", lambda: 100.0)
    tui = make_tui()
    await add_speaker_effects(tui)
    for sequence, timestamp in enumerate((100.0, 96.0)):
        tui.handle_event(
            Event(
                session_id=tui.active_view.session.session_id,
                world="alpha",
                connection_generation=1,
                sequence=sequence,
                direction=Direction.INBOUND,
                kind=EventKind.SAY,
                timestamp=datetime.fromtimestamp(timestamp, UTC),
                canonical_text="Alice says, hello",
                plain_text="Alice says, hello",
                display_text="Alice says, hello",
                provenance=Provenance(sender_name="Alice"),
            )
        )

    first, second = (decorations[0] for decorations in tui.active_view.display._entry_decorations)

    assert first.cycle_phase(0.0) == pytest.approx(0.0)
    assert second.cycle_phase(0.0) == pytest.approx(4.0)


async def test_new_speaker_effect_restarts_a_scheduler_waiting_on_a_later_effect() -> None:
    tui = make_tui()
    await add_speaker_effects(tui)
    tui._animations_started = True
    waiting_task = asyncio.create_task(asyncio.sleep(60))
    tui._animation_task = waiting_task
    try:
        tui.handle_event(
            Event(
                session_id=tui.active_view.session.session_id,
                world="alpha",
                connection_generation=1,
                sequence=0,
                direction=Direction.INBOUND,
                kind=EventKind.SAY,
                canonical_text="Alice says, hello",
                plain_text="Alice says, hello",
                display_text="Alice says, hello",
                provenance=Provenance(sender_name="Alice"),
            )
        )
        replacement_task = tui._animation_task
        await asyncio.sleep(0)

        assert waiting_task.cancelled()
        assert replacement_task is not None
        assert replacement_task is not waiting_task
    finally:
        tui._animations_started = False
        tui._sync_animation_task()


async def test_historical_one_shot_effect_remains_completed_after_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: 50.0)
    monkeypatch.setattr("tfr.tui.time.time", lambda: 100.0)
    tui = make_tui()
    await add_speaker_effects(
        tui,
        effect=TextEffectKind.AGE_DECAY,
        interval_seconds=30,
        loop=False,
        base_color="#6f7782",
    )
    tui.active_view.display.resize(width=80, height=1)
    tui.handle_event(
        Event(
            session_id=tui.active_view.session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            timestamp=datetime.fromtimestamp(65.0, UTC),
            canonical_text="Alice says, hello",
            plain_text="Alice says, hello",
            display_text="Alice says, hello",
            provenance=Provenance(sender_name="Alice"),
        )
    )

    decoration = tui.active_view.display._entry_decorations[0][0]

    assert decoration.cycle_phase(0.0) == pytest.approx(35.0)
    assert tui.active_view.display.animation_frame_delay(0.0) is None
    assert tui.active_view.output_text()[0][:2] == ("fg:#6f7782", "Alice")


def test_border_text_surrounds_the_inner_output_width() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=6, height=2)

    top = fragment_list_to_text(tui._border_text(view, "output", BorderEdge.TOP, 2))
    left = fragment_list_to_text(tui._border_text(view, "output", BorderEdge.LEFT, 2))

    assert top == "┌──────┐"
    assert left == "│\n│"


def test_releasing_selection_over_border_finishes_copy() -> None:
    tui = make_tui()
    view = tui.active_view
    copied: list[str] = []
    view._copy_handler = copied.append
    view.display.resize(width=10, height=2)
    view.display.append("first\nsecond")
    view.handle_output_mouse(
        MouseEvent(Point(x=2, y=0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )

    view.handle_border_mouse(
        MouseEvent(Point(x=4, y=0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()),
        BorderEdge.BOTTOM,
        panel="output",
    )

    assert view._selection_dragging is False
    assert view.selected_text() == "rst\nseco"
    assert copied == ["rst\nseco"]


def test_releasing_selection_over_border_finishes_copy_with_padding() -> None:
    # Same drag as above, but the pane is taller than the buffered content,
    # so the border-mouse coordinate translation must account for the
    # blank rows padded above the real content.
    tui = make_tui()
    view = tui.active_view
    copied: list[str] = []
    view._copy_handler = copied.append
    view.display.resize(width=10, height=5)
    view.display.append("first\nsecond")
    view.handle_output_mouse(
        MouseEvent(Point(x=2, y=3), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )

    view.handle_border_mouse(
        MouseEvent(Point(x=4, y=0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()),
        BorderEdge.BOTTOM,
        panel="output",
    )

    assert view._selection_dragging is False
    assert view.selected_text() == "rst\nseco"
    assert copied == ["rst\nseco"]


async def test_boss_mode_hides_buffered_world_text_until_enter() -> None:
    tui = make_tui()
    tui.active_view.input_buffer.text = "unfinished draft"

    await tui._handle_client_command("alpha", "/boss")

    assert tui.boss_mode is True
    assert tui._boss_refresh_task is not None
    assert tui.boss_control.modal is True
    assert tui.application.layout.has_focus(tui.boss_control)
    assert any(Keys.Any in binding.keys for binding in tui.boss_control.key_bindings.bindings)
    tui.handle_event(
        Event(
            session_id=tui.active_view.session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.INBOUND,
            kind=EventKind.RAW_OUTPUT,
            canonical_text="new private world text",
            plain_text="new private world text",
            display_text="new private world text",
        )
    )
    assert "new private world text" not in fragment_list_to_text(tui.boss_text())
    assert "new private world text" in fragment_list_to_text(
        tui.active_view.display.formatted_text()
    )
    assert "New events on /var/log/alpha: 1 line" in fragment_list_to_text(tui.boss_text())
    assert tui.plugins.emit_boss_event(
        BossViewEvent(kind="fixture", text="Background check completed", source="fixture")
    )

    enter = next(
        binding
        for binding in tui.boss_control.key_bindings.bindings
        if Keys.ControlM in binding.keys
    )
    enter.handler(SimpleNamespace())

    assert tui.boss_mode is False
    assert tui._boss_refresh_task is None
    assert tui.application.layout.current_buffer is tui.active_view.input_buffer
    assert tui.active_view.input_buffer.text == "unfinished draft"


async def test_switching_boss_views_restarts_a_changed_refresh_interval() -> None:
    class RefreshBossFixture:
        def register(self, registrar: object, _config: object) -> None:
            self.slow = registrar.register_boss_view(
                "slow", lambda _context: (("", "slow"),), refresh_interval_seconds=60
            )
            self.fast = registrar.register_boss_view(
                "fast", lambda _context: (("", "fast"),), refresh_interval_seconds=1
            )

    fixture = RefreshBossFixture()
    plugins = await PluginManager.load(
        enabled=("refresh",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(entry_point("refresh", fixture),),
        scope="ui",
    )
    tui = make_tui(plugins=plugins)

    await fixture.slow.activate("alpha")
    slow_task = tui._boss_refresh_task
    assert tui._boss_refresh_interval == 60
    await fixture.fast.activate("alpha")
    await asyncio.sleep(0)

    assert slow_task is not tui._boss_refresh_task
    assert slow_task is not None and slow_task.cancelled()
    assert tui._boss_refresh_interval == 1
    tui.dismiss_boss()


async def test_boss_mode_consumes_normal_terminal_input() -> None:
    with create_pipe_input() as input:
        tui = make_tui(input=input)
        running = asyncio.create_task(tui.run())

        async def wait_for_boss_mode(expected: bool) -> None:
            while tui.boss_mode is not expected:
                await asyncio.sleep(0.005)

        try:
            await asyncio.sleep(0)
            input.send_text("/boss\r")
            await asyncio.wait_for(wait_for_boss_mode(True), timeout=1)

            input.send_bytes(b"\x1b[17~")
            input.send_text("ignored")
            await asyncio.sleep(0.05)
            assert tui.boss_mode is True
            assert tui.active_alias == "alpha"
            assert tui.active_view.input_buffer.text == ""

            input.send_text("\r")
            await asyncio.wait_for(wait_for_boss_mode(False), timeout=1)
        finally:
            input.send_bytes(b"\x11")
            await asyncio.wait_for(running, timeout=1)


def test_inactive_world_events_increment_unread_until_switch() -> None:
    tui = make_tui()
    beta = tui.views["beta"]
    beta.display.resize(width=80, height=10)
    event = Event(
        session_id=beta.session.session_id,
        world="beta",
        connection_generation=1,
        sequence=0,
        direction=Direction.INBOUND,
        kind=EventKind.SAY,
        canonical_text="\x1b[32mSomeone says hello\x1b[0m",
        plain_text="Someone says hello",
        display_text="\x1b[32mSomeone says hello\x1b[0m",
    )

    tui.handle_event(event)

    assert beta.unread_events == 1
    assert fragment_list_to_text(beta.display.formatted_text()) == "Someone says hello"
    tui.switch_world("beta")
    assert beta.unread_events == 0


def test_more_count_is_visible_in_status_bar() -> None:
    tui = make_tui()
    display = tui.active_view.display
    display.resize(width=80, height=3)

    display.append("one\ntwo\nthree\nfour\nfive")

    status = fragment_list_to_text(tui.status_bar())
    assert "rows 1-3/5" in status
    assert "More 2" in status


def test_human_commands_are_shown_in_recent_input_not_world_output() -> None:
    tui = make_tui()
    session = tui.active_view.session
    human_command = Event(
        session_id=session.session_id,
        world="alpha",
        connection_generation=1,
        sequence=0,
        direction=Direction.OUTBOUND,
        kind=EventKind.COMMAND,
        canonical_text="say Hello",
        plain_text="say Hello",
        display_text="say Hello",
        actor=Actor(ActorType.HUMAN, "operator"),
    )
    idle_command = Event(
        session_id=session.session_id,
        world="alpha",
        connection_generation=1,
        sequence=1,
        direction=Direction.OUTBOUND,
        kind=EventKind.COMMAND,
        canonical_text="@@",
        plain_text="@@",
        display_text="@@",
        actor=Actor(ActorType.IDLE, "idle-command"),
    )

    tui.handle_event(human_command)
    tui.handle_event(idle_command)

    assert fragment_list_to_text(tui.active_view.display.formatted_text()) == ""
    assert fragment_list_to_text(tui.active_view.recent_input_text()) == "\n\nsay Hello"

    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=2,
            direction=Direction.OUTBOUND,
            kind=EventKind.COMMAND,
            canonical_text="pose waves.",
            plain_text="pose waves.",
            display_text="pose waves.",
            actor=Actor(ActorType.HUMAN, "operator"),
        )
    )
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=3,
            direction=Direction.OUTBOUND,
            kind=EventKind.COMMAND,
            canonical_text="say Later",
            plain_text="say Later",
            display_text="say Later",
            actor=Actor(ActorType.HUMAN, "operator"),
        )
    )
    assert fragment_list_to_text(tui.active_view.recent_input_text()) == (
        "say Hello\npose waves.\nsay Later"
    )


async def test_submitted_text_uses_active_human_session() -> None:
    tui = make_tui()
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", "look")

    request = queue.get_nowait()
    assert request.world == "alpha"
    assert request.text == "look"
    assert request.actor.type is ActorType.HUMAN
    assert request.actor.id == "operator"


async def test_submitted_text_returns_scrolled_output_to_live() -> None:
    tui = make_tui()
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    tui.command_bus.register(session.session_id)
    display = tui.active_view.display
    display.resize(width=40, height=2)
    for line in ("one", "two", "three", "four"):
        display.append(line)
    display.pager.jump_to_end()
    display.pager.scroll_rows(-1)
    assert display.pager.mode is PagerMode.SCROLLED

    await tui.submit_text("alpha", ":eats a banana")

    assert display.pager.mode is PagerMode.FOLLOW
    assert display.pager.more_rows == 0


def test_multiline_paste_preflight_preserves_formatting_and_escapes_lines() -> None:
    assert _multiline_paste_commands(
        "one\r\n two\n\n",
        server="tinymux",
        encoding="utf-8",
    ) == ("@emit one", "@emit %btwo", "@emit ")


def test_multiline_paste_preflight_rejects_unsupported_worlds_and_long_lines() -> None:
    with pytest.raises(ValueError, match="bare, tinymush, or tinymux"):
        _multiline_paste_commands("one\ntwo", server="generic", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1 exceeds"):
        _multiline_paste_commands("x" * 7_001 + "\ntwo", server="tinymux", encoding="utf-8")
    with pytest.raises(ValueError, match="NUL"):
        _multiline_paste_commands("one\ntwo\x00", server="tinymux", encoding="utf-8")


def test_image_parameters_are_bounded_and_unambiguous() -> None:
    assert TfrTui._parse_image_parameters([]) == (72, None, None)
    assert TfrTui._parse_image_parameters(
        ["--width", "80", "--unicode", "picture with spaces.png"]
    ) == (80, "braille", Path("picture with spaces.png"))
    with pytest.raises(ValueError, match="between 1 and 80"):
        TfrTui._parse_image_parameters(["--width", "81"])
    with pytest.raises(ValueError, match="only one"):
        TfrTui._parse_image_parameters(["--ascii", "--unicode"])
    with pytest.raises(ValueError, match="Usage"):
        TfrTui._parse_image_parameters(["one.png", "two.png"])


async def test_image_preview_defaults_to_world_capability_and_preserves_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = make_tui(server="tinymux", unicode=True)
    tui.active_view.input_buffer.text = "unfinished draft"
    modes: list[str] = []

    def clipboard_image() -> Image.Image:
        return Image.new("RGB", (4, 4), "white")

    def renderer(image: Image.Image, *, width: int, mode: str) -> SimpleNamespace:
        modes.append(mode)
        return SimpleNamespace(lines=("⣿",), width=width, height=1, mode=mode)

    monkeypatch.setattr("tfr.tui.load_clipboard_image", clipboard_image)
    monkeypatch.setattr("tfr.tui.render_image", renderer)

    await tui.open_image_preview("alpha", [])

    assert modes == ["braille"]
    assert tui.image_preview is not None
    assert tui.image_preview.commands == ("@emit ⣿",)
    assert tui.active_view.input_buffer.text == "unfinished draft"

    tui.switch_world("beta")

    assert tui.active_alias == "alpha"


async def test_image_preview_rejects_connection_change_during_source_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = make_tui(server="tinymux")
    session = tui.active_view.session

    def clipboard_image() -> Image.Image:
        session.connection_generation += 1
        return Image.new("RGB", (4, 4), "white")

    monkeypatch.setattr("tfr.tui.load_clipboard_image", clipboard_image)

    await tui.open_image_preview("alpha", ["--ascii"])

    assert tui.image_preview is None
    assert "connection changed" in tui.active_view.display.entries[-1]


async def test_image_preview_rejects_unicode_without_world_capability() -> None:
    tui = make_tui(server="tinymux")

    await tui.open_image_preview("alpha", ["--unicode"])

    assert tui.image_preview is None
    assert "Unicode image glyphs are disabled" in tui.active_view.display.entries[-1]


async def test_invalid_image_options_do_not_latch_operation_guard() -> None:
    tui = make_tui(server="tinymux")

    await tui.open_image_preview("alpha", ["--width", "invalid"])
    await tui.open_image_preview("alpha", ["--unicode"])

    assert tui._image_operation_active is False
    assert "Unicode image glyphs are disabled" in tui.active_view.display.entries[-1]


async def test_confirmed_image_sends_paced_preflighted_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = make_tui(server="tinymux")
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("tfr.tui.asyncio.sleep", sleep)
    await tui._submit_paced_commands(
        "alpha",
        ("@emit first", "@emit second"),
        source="image",
        metadata={"image_width": 72, "image_height": 2, "image_mode": "ascii"},
    )

    requests = [queue.get_nowait(), queue.get_nowait()]
    assert [request.text for request in requests] == ["@emit first", "@emit second"]
    assert sleeps == [0.5]
    assert requests[0].metadata == {
        "image_width": 72,
        "image_height": 2,
        "image_mode": "ascii",
        "image_line": 1,
        "image_total_lines": 2,
    }


async def test_paced_transfer_stops_when_connection_generation_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = make_tui(server="tinymux")
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    async def sleep(_delay: float) -> None:
        session.connection_generation += 1

    monkeypatch.setattr("tfr.tui.asyncio.sleep", sleep)

    await tui._submit_paced_commands(
        "alpha",
        ("@emit first", "@emit second"),
        source="image",
    )

    assert queue.get_nowait().text == "@emit first"
    assert queue.empty()
    assert "stopped accepting" in tui.active_view.display.entries[-1]


async def test_paced_transfer_rejects_stale_pinned_generation() -> None:
    tui = make_tui(server="tinymux")
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)
    generation = session.connection_generation
    session.connection_generation += 1

    await tui._submit_paced_commands(
        "alpha",
        ("@emit first",),
        source="image",
        expected_generation=generation,
        expected_server="tinymux",
        expected_encoding=session.encoding,
    )

    assert queue.empty()
    assert "was not sent" in tui.active_view.display.entries[-1]


async def test_paced_transfer_blocks_other_input() -> None:
    tui = make_tui(server="tinymux")
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)
    tui._active_multiline_pastes.add("alpha")

    await tui.submit_text("alpha", "look")
    await tui.submit_text("alpha", "/reconnect")

    assert queue.empty()
    assert "paced transfer is active" in tui.active_view.display.entries[-1]


async def test_reserved_transfer_releases_when_disconnected() -> None:
    tui = make_tui(server="tinymux")
    tui._active_multiline_pastes.add("alpha")

    await tui._submit_paced_commands(
        "alpha",
        ("@emit one",),
        source="image",
        reserved=True,
    )

    assert "alpha" not in tui._active_multiline_pastes


async def test_image_preflight_rejects_unencodable_braille_before_sending() -> None:
    tui = make_tui(server="tinymux", unicode=True)
    session = tui.active_view.session
    session.config = session.config.model_copy(update={"encoding": "ascii"})
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    with pytest.raises(ValueError, match="cannot be encoded as ascii"):
        tui._image_commands(
            "alpha",
            SimpleNamespace(lines=("⣿",), width=1, height=1, mode="braille"),
        )

    assert queue.empty()


async def test_multiline_paste_sends_first_line_immediately_then_paces_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = make_tui(server="tinymux")
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("tfr.tui.asyncio.sleep", sleep)

    await tui.submit_multiline_paste("alpha", "one\n two\n\n")

    requests = [queue.get_nowait(), queue.get_nowait(), queue.get_nowait()]
    assert [request.text for request in requests] == ["@emit one", "@emit %btwo", "@emit "]
    assert sleeps == [0.5, 0.5]
    assert requests[0].actor == Actor(ActorType.HUMAN, "operator")
    assert requests[0].metadata == {
        "multiline_paste_line": 1,
        "multiline_paste_total_lines": 3,
    }
    assert requests[2].metadata["multiline_paste_line"] == 3
    assert tui._active_multiline_pastes == set()


async def test_bracketed_paste_inserts_one_line_and_emits_multiple_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = make_tui(server="tinymush")
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    async def sleep(_delay: float) -> None:
        pass

    monkeypatch.setattr("tfr.tui.asyncio.sleep", sleep)
    binding = next(
        item for item in tui.application.key_bindings.bindings if Keys.BracketedPaste in item.keys
    )
    buffer = tui.active_view.input_buffer

    binding.handler(SimpleNamespace(data="single", current_buffer=buffer))
    assert buffer.text == "single"

    buffer.text = ""
    binding.handler(SimpleNamespace(data="single\n", current_buffer=buffer))
    assert buffer.text == "single"

    buffer.text = ""
    binding.handler(SimpleNamespace(data="one\r\ntwo", current_buffer=buffer))
    await asyncio.gather(*tuple(tui._background_tasks))

    assert buffer.text == ""
    assert [queue.get_nowait().text, queue.get_nowait().text] == ["@emit one", "@emit two"]


async def test_shell_commands_suspend_terminal_without_using_world_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = make_tui()
    calls: list[tuple[str, bool]] = []

    async def run_system_command(command: str, *, wait_for_enter: bool) -> None:
        calls.append((command, wait_for_enter))

    monkeypatch.setattr(tui.application, "run_system_command", run_system_command)
    monkeypatch.setenv("SHELL", "/bin/test-shell")

    await tui.submit_text("alpha", "! w")
    await tui.submit_text("alpha", "/sh")

    assert calls == [("w", True), ("/bin/test-shell", False)]


async def test_double_bang_sends_literal_bang_to_world() -> None:
    tui = make_tui()
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", "!! w")

    assert queue.get_nowait().text == "! w"


async def test_application_starts_and_quits_from_keyboard() -> None:
    with create_pipe_input() as input:
        tui = make_tui(input=input)
        session = tui.active_view.session
        session.state = SessionState.CONNECTED
        queue = tui.command_bus.register(session.session_id)
        tui.active_view.display.append("old output")
        running = asyncio.create_task(tui.run())
        await asyncio.sleep(0)
        input.send_bytes(b"\x1b[17~")

        async def wait_for_world(alias: str) -> None:
            while tui.active_alias != alias:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_world("beta"), timeout=1)
        input.send_bytes(b"\x1b[15~")
        await asyncio.wait_for(wait_for_world("alpha"), timeout=1)
        input.send_bytes(b"\x0c")

        async def wait_for_clear() -> None:
            while not tui.active_view.display.screen_is_cleared:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_clear(), timeout=1)
        assert fragment_list_to_text(tui.active_view.display.formatted_text()) == ""
        input.send_text("look\r")

        request = await asyncio.wait_for(queue.get(), timeout=1)
        assert request.text == "look"
        input.send_bytes(b"\x11")

        assert await asyncio.wait_for(running, timeout=1) == 0


async def test_two_live_worlds_reach_the_ui_independently() -> None:
    async def wait_until(predicate: Callable[[], bool]) -> None:
        async def poll() -> None:
            while not predicate():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(poll(), timeout=1)

    async def start_world(message: str) -> tuple[asyncio.Server, str, int]:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(f"{message}\r\n".encode())
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        socket = server.sockets[0]
        host, port = socket.getsockname()[:2]
        return server, str(host), int(port)

    alpha_server, alpha_host, alpha_port = await start_world("from alpha")
    beta_server, beta_host, beta_port = await start_world("from beta")
    event_bus = EventBus()
    command_bus = CommandBus()
    sessions = [
        WorldSession(
            world="alpha",
            config=WorldConfig(
                host=alpha_host,
                port=alpha_port,
                autoconnect=True,
                reconnect=False,
            ),
            defaults=WorldDefaults(),
            event_bus=event_bus,
            command_bus=command_bus,
            prompt_flush_seconds=0.01,
        ),
        WorldSession(
            world="beta",
            config=WorldConfig(
                host=beta_host,
                port=beta_port,
                autoconnect=True,
                reconnect=False,
            ),
            defaults=WorldDefaults(),
            event_bus=event_bus,
            command_bus=command_bus,
            prompt_flush_seconds=0.01,
        ),
    ]
    with create_pipe_input() as input:
        tui = TfrTui(
            sessions=sessions,
            manager=SessionManager(sessions),
            event_bus=event_bus,
            command_bus=command_bus,
            scrollback_lines={"alpha": 100, "beta": 100},
            agent_worlds=set(),
            pager_enabled=True,
            pager_overlap=1,
            input=input,
            output=DummyOutput(),
        )
        running = asyncio.create_task(tui.run())
        try:
            await wait_until(
                lambda: (
                    any("from alpha" in entry for entry in tui.views["alpha"].display.entries)
                    and any("from beta" in entry for entry in tui.views["beta"].display.entries)
                )
            )
            tui.switch_world("beta")
            assert tui.active_alias == "beta"
            assert tui.views["beta"].unread_events == 0
            assert sessions[0].state is SessionState.CONNECTED
            assert sessions[1].state is SessionState.CONNECTED
        finally:
            input.send_bytes(b"\x11")
            await asyncio.wait_for(running, timeout=1)
            alpha_server.close()
            beta_server.close()
            await asyncio.gather(alpha_server.wait_closed(), beta_server.wait_closed())


async def test_agent_inspector_and_controls_are_available_from_client_commands() -> None:
    tui = make_tui()
    controller = SimpleNamespace(
        name="bot",
        session=tui.views["beta"].session,
        provider=SimpleNamespace(name="local", endpoint="http://localhost:11434/v1"),
        config=SimpleNamespace(model="test-model"),
        inspection=AgentInspection(
            state="idle",
            validation="accepted:submitted",
            command="say Hello",
            response='{"action":"say","text":"Hello","target":null}',
        ),
    )

    class FakeAgents:
        def __init__(self) -> None:
            self.controllers = {"bot": controller}
            self.paused = False
            self.resumed = False
            self.triggered = False

        def for_world(self, world: str) -> object | None:
            return controller if world == "beta" else None

        async def pause(self, name: str) -> bool:
            self.paused = name == "bot"
            return self.paused

        def resume(self, name: str) -> bool:
            self.resumed = name == "bot"
            return self.resumed

        def trigger(self, name: str) -> bool:
            self.triggered = name == "bot"
            return self.triggered

    agents = FakeAgents()
    tui.agents = agents  # type: ignore[assignment]

    await tui._handle_agent_command("beta", ["inspect", "bot"])
    inspector = fragment_list_to_text(tui.agent_inspector_text())
    assert "Agent: bot" in inspector
    assert "Validation: accepted:submitted" in inspector
    assert "Command: say Hello" in inspector

    await tui._handle_agent_command("beta", ["pause", "bot"])
    await tui._handle_agent_command("beta", ["resume", "bot"])
    await tui._handle_agent_command("beta", ["trigger", "bot"])
    assert agents.paused and agents.resumed and agents.triggered


async def test_replay_events_load_without_connections_and_control_c_exits() -> None:
    with create_pipe_input() as input:
        tui = make_tui(input=input)
        tui.replay_mode = True
        tui.initial_scroll_to_end = False
        tui.initial_events = (
            Event(
                session_id=tui.views["alpha"].session.session_id,
                world="alpha",
                connection_generation=1,
                sequence=0,
                direction=Direction.INBOUND,
                kind=EventKind.RAW_OUTPUT,
                canonical_text="replayed line\r\n",
                plain_text="replayed line\r\n",
                display_text="replayed line\r\n",
            ),
        )
        running = asyncio.create_task(tui.run())

        async def wait_for_replay() -> None:
            while not tui.views["alpha"].display.entries:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_replay(), timeout=1)
        assert "replay" in fragment_list_to_text(tui.status_bar())
        assert tui.views["alpha"].display.entries == ["replayed line\r\n"]
        input.send_bytes(b"\x03")
        assert await asyncio.wait_for(running, timeout=1) == 130


async def test_initial_snapshot_is_rendered_at_live_end_before_service_runtime_starts() -> None:
    with create_pipe_input() as input:
        tui = make_tui(input=input)
        tui.initial_events = tuple(
            Event(
                session_id=tui.views[world].session.session_id,
                world=world,
                connection_generation=1,
                sequence=sequence,
                direction=Direction.INBOUND,
                kind=EventKind.RAW_OUTPUT,
                display_text=f"snapshot {world} {sequence}\r\n",
            )
            for world in ("alpha", "beta")
            for sequence in range(40)
        )
        started = asyncio.Event()

        class Runtime:
            async def start(self) -> None:
                for world in ("alpha", "beta"):
                    display = tui.views[world].display
                    assert len(display.entries) == 40
                    assert display.pager.mode is PagerMode.FOLLOW
                    assert display.pager.visible_end == display.pager.total_rows
                    assert display.pager.more_rows == 0
                started.set()

            async def stop(self) -> None:
                pass

        tui.service_runtime = Runtime()
        running = asyncio.create_task(tui.run())

        await asyncio.wait_for(started.wait(), timeout=1)
        input.send_bytes(b"\x11")
        assert await asyncio.wait_for(running, timeout=1) == 0


async def test_startup_notices_follow_snapshot_and_remain_at_live_end() -> None:
    with create_pipe_input() as input:
        tui = make_tui(input=input)
        tui.initial_events = tuple(
            Event(
                session_id=tui.views["alpha"].session.session_id,
                world="alpha",
                connection_generation=1,
                sequence=sequence,
                direction=Direction.INBOUND,
                kind=EventKind.RAW_OUTPUT,
                display_text=f"snapshot {sequence}\r\n",
            )
            for sequence in range(120)
        )
        for index in range(30):
            tui.queue_startup_notice("alpha", f"Startup notice {index}")
        started = asyncio.Event()

        class Runtime:
            async def start(self) -> None:
                display = tui.views["alpha"].display
                assert all(
                    f"Startup notice {index}" in entry
                    for index, entry in enumerate(display.entries[-30:])
                )
                assert display.pager.mode is PagerMode.FOLLOW
                assert display.pager.visible_end == display.pager.total_rows
                assert display.pager.visible_range[1] == len(display.rows)
                started.set()

            async def stop(self) -> None:
                pass

        tui.service_runtime = Runtime()
        running = asyncio.create_task(tui.run())

        await asyncio.wait_for(started.wait(), timeout=1)
        input.send_bytes(b"\x11")
        assert await asyncio.wait_for(running, timeout=1) == 0


async def test_run_client_stops_runtime_when_tui_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tfr.gateway import GatewayRuntime

    runtime = SimpleNamespace(
        sessions=(),
        manager=object(),
        event_bus=object(),
        command_bus=object(),
        plugins=object(),
        agents=None,
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        GatewayRuntime,
        "from_configuration",
        AsyncMock(return_value=runtime),
    )

    def fail_tui(**_kwargs: object) -> object:
        raise ValueError("unknown boss screen: missing")

    monkeypatch.setattr("tfr.tui.TfrTui", fail_tui)
    bundle = ConfigurationBundle(
        main_path=Path("config.jsonc"),
        worlds_path=Path("worlds.jsonc"),
        agents_path=Path("agents.jsonc"),
        main=MainConfig(),
        worlds=WorldsConfig(),
        agents=AgentsConfig(),
    )

    with pytest.raises(ValueError, match="unknown boss screen: missing"):
        await run_client(bundle)

    runtime.stop.assert_awaited_once()


async def test_reload_snapshot_returns_each_world_to_live_output() -> None:
    with create_pipe_input() as input:
        tui = make_tui(input=input)
        tui.initial_events = tuple(
            Event(
                session_id=tui.views["alpha"].session.session_id,
                world="alpha",
                connection_generation=1,
                sequence=sequence,
                direction=Direction.INBOUND,
                kind=EventKind.RAW_OUTPUT,
                display_text=f"snapshot {sequence}\r\n",
            )
            for sequence in range(40)
        )
        tui.initial_scroll_to_end = True
        started = asyncio.Event()

        class Runtime:
            async def start(self) -> None:
                pager = tui.views["alpha"].display.pager
                assert pager.mode is PagerMode.FOLLOW
                assert pager.visible_end == pager.total_rows
                assert pager.more_rows == 0
                started.set()

            async def stop(self) -> None:
                pass

        tui.service_runtime = Runtime()
        running = asyncio.create_task(tui.run())

        await asyncio.wait_for(started.wait(), timeout=1)
        input.send_bytes(b"\x11")
        assert await asyncio.wait_for(running, timeout=1) == 0
