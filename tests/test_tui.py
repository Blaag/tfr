from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

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
    LoginConfig,
    MainConfig,
    PluginSource,
    SpellcheckConfig,
    ThemeConfig,
    TypingGlowConfig,
    UpdateConfig,
    WorldCapabilitiesConfig,
    WorldConfig,
    WorldDefaults,
    WorldsConfig,
)
from tfr.core import CommandBus, EventBus
from tfr.events import (
    Actor,
    ActorType,
    Direction,
    Event,
    EventKind,
    Provenance,
    SpoofAssessment,
    SpoofStatus,
)
from tfr.gateway_protocol import event_from_message, event_message
from tfr.pager import PagerMode
from tfr.plugin_api import (
    BorderFragment,
    EffectDemo,
    EffectDemoSample,
    ScreenClearContext,
    TextDecoration,
    TextEffectKind,
    color_pulse,
)
from tfr.plugin_sources import PluginSourceNotice, PluginUpdateChecker, PluginUpdateResult
from tfr.plugins import BossViewEvent, PluginManager, ProjectedEffectDemo
from tfr.presentation import EffectProgram, effect_programs_as_dict
from tfr.sessions import SessionManager, SessionState, WorldSession
from tfr.tui import (
    _MAX_OBSERVED_SPEAKER_LENGTH,
    _MAX_OBSERVED_SPEAKERS,
    _SETTINGS_DIALOG_STYLE,
    TfrTui,
    _format_elapsed,
    _multiline_paste_commands,
    _osc52_sequence,
    _running_under_mosh,
    run_client,
)
from tfr.ui_settings import LocalUiSettingsStore
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
    mouse_mode: str = "tfr",
    spellcheck: SpellcheckConfig | None = None,
    typing_glow: TypingGlowConfig | None = None,
    character: str | None = None,
    speaking_streaks_enabled: bool = True,
    service_runtime: Any | None = None,
    local_ui_settings: LocalUiSettingsStore | None = None,
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
                login=(
                    LoginConfig(character=character, password="test-secret")
                    if character is not None
                    else None
                ),
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
        mouse_mode=mouse_mode,
        spellcheck=spellcheck,
        typing_glow=typing_glow,
        speaking_streaks_enabled=speaking_streaks_enabled,
        service_runtime=service_runtime,
        local_ui_settings=local_ui_settings,
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
    assert "--" not in fragment_list_to_text(tui.active_view.display.rows[-1])


async def test_settings_dialog_uses_main_renderer_and_keyboard_navigation() -> None:
    started = asyncio.Event()
    with create_pipe_input() as input:
        tui = make_tui(input=input)
        original_ttimeoutlen = tui.application.ttimeoutlen
        original_timeoutlen = tui.application.timeoutlen
        application_ids: list[int] = []

        async def exercise_dialog() -> None:
            await started.wait()
            choice = asyncio.create_task(
                tui._settings_choice(
                    title="Settings",
                    text="Choose",
                    values=[("first", "First"), ("second", "Second")],
                )
            )
            render_counter = tui.application.render_counter
            while tui._settings_dialog is None:
                await asyncio.sleep(0)
            while tui.application.render_counter == render_counter:
                await asyncio.sleep(0)
            application_ids.append(id(tui.application))
            assert tui.application.ttimeoutlen == 0.1
            assert tui.application.timeoutlen == 0.1
            input.send_text("\x1b[B")
            await asyncio.sleep(0.05)
            input.send_text("\r")
            assert await asyncio.wait_for(choice, timeout=1) == "second"
            assert tui._settings_dialog is None

            render_counter = tui.application.render_counter
            entered = asyncio.create_task(
                tui._settings_input(title="Search settings", text="Search")
            )
            while tui._settings_dialog is None:
                await asyncio.sleep(0)
            while tui.application.render_counter == render_counter:
                await asyncio.sleep(0)
            application_ids.append(id(tui.application))
            input.send_text("mouse\r")
            assert await asyncio.wait_for(entered, timeout=1) == "mouse"

            render_counter = tui.application.render_counter
            cancelled = asyncio.create_task(
                tui._settings_choice(
                    title="Settings",
                    text="Cancel",
                    values=[("only", "Only")],
                )
            )
            while tui._settings_dialog is None:
                await asyncio.sleep(0)
            while tui.application.render_counter == render_counter:
                await asyncio.sleep(0)
            application_ids.append(id(tui.application))
            input.send_text("\x1b")
            assert await asyncio.wait_for(cancelled, timeout=0.5) is None
            tui.application.exit(result=0)

        dialog_task = asyncio.create_task(exercise_dialog())
        result = await asyncio.wait_for(
            tui.application.run_async(pre_run=started.set), timeout=2
        )
        await dialog_task

        assert result == 0
        assert application_ids == [id(tui.application)] * 3
        assert tui.application._running_in_terminal is False
        assert tui.application.ttimeoutlen == original_ttimeoutlen
        assert tui.application.timeoutlen == original_timeoutlen


def test_settings_keyboard_cursor_has_visible_terminal_independent_style() -> None:
    radio = _SETTINGS_DIALOG_STYLE.get_attrs_for_style_str("class:radio-selected")
    checkbox = _SETTINGS_DIALOG_STYLE.get_attrs_for_style_str("class:checkbox-selected")

    assert radio.reverse is True and radio.bold is True
    assert checkbox.reverse is True and checkbox.bold is True


def test_settings_dialog_uses_active_theme_roles() -> None:
    tui = make_tui(theme=ThemeConfig(preset="catppuccin-mocha"))
    style = tui.application.style

    body = style.get_attrs_for_style_str("class:dialog.body")
    title = style.get_attrs_for_style_str("class:dialog class:frame.label")
    button = style.get_attrs_for_style_str("class:button.focused")
    cursor = style.get_attrs_for_style_str("class:radio-selected")

    assert body.color == "cdd6f4"
    assert body.bgcolor == "1e1e2e"
    assert title.color == "89b4fa"
    assert button.bgcolor == "585b70"
    assert cursor.reverse is True and cursor.bold is True

    default_tui = make_tui(theme=ThemeConfig(preset="default"))
    default_body = default_tui.application.style.get_attrs_for_style_str(
        "class:dialog.body"
    )
    assert default_body.color != default_body.bgcolor
    assert default_body.color == "d7d7d7"
    assert default_body.bgcolor == "000000"


def test_output_color_overrides_theme_plain_text_color() -> None:
    tui = make_tui(
        theme=ThemeConfig(preset="catppuccin-mocha"),
        output_color="#010203",
    )

    assert tui.active_view.display.default_style == "fg:#010203"


def test_mosh_detection_walks_process_ancestry(monkeypatch: pytest.MonkeyPatch) -> None:
    processes = {
        30: (20, "python"),
        20: (10, "zsh"),
        10: (1, "mosh-server"),
    }
    monkeypatch.setattr("tfr.tui._process_parent", processes.get)

    assert _running_under_mosh(30) is True
    assert _running_under_mosh(20) is True
    assert _running_under_mosh(10) is True
    assert _running_under_mosh(999) is False


async def test_mouse_mode_defaults_to_tfr_and_can_be_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = make_tui()

    assert tui.effective_mouse_mode == "tfr"
    assert tui.application.mouse_support() is True

    await tui._handle_client_command("alpha", "/mouse terminal")
    assert tui.effective_mouse_mode == "terminal"
    assert tui.application.mouse_support() is False

    monkeypatch.setattr("tfr.tui._running_under_mosh", lambda: True)
    await tui._handle_client_command("alpha", "/mouse auto")
    assert tui.effective_mouse_mode == "terminal"
    assert tui.application.mouse_support() is False

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Mouse mode: auto (terminal; mosh-server detected)" in text


def test_gateway_mouse_mode_update_applies_live(monkeypatch: pytest.MonkeyPatch) -> None:
    tui = make_tui(mouse_mode="tfr")
    monkeypatch.setattr("tfr.tui._running_under_mosh", lambda: True)

    tui.apply_mouse_mode("terminal")
    assert tui.mouse_mode == "terminal"
    assert tui.effective_mouse_mode == "terminal"

    tui.apply_mouse_mode("auto")
    assert tui.mouse_mode == "auto"
    assert tui.effective_mouse_mode == "terminal"
    assert tui.application.mouse_support() is False


async def test_mouse_command_persists_only_in_local_ui_store(tmp_path: Path) -> None:
    store = LocalUiSettingsStore(tmp_path / "ui")
    tui = make_tui(local_ui_settings=store)

    await tui._handle_client_command("alpha", "/mouse terminal")

    assert tui.mouse_mode == "terminal"
    assert LocalUiSettingsStore(tmp_path / "ui").mouse_mode == "terminal"


async def test_mouse_command_rejects_invalid_mode() -> None:
    tui = make_tui()

    await tui._handle_client_command("alpha", "/mouse invalid")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Usage: /mouse auto|terminal|tfr|status" in text


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
            registrar.register_effect_demo(  # type: ignore[attr-defined]
                EffectDemo(
                    id="speaker:alice",
                    label="Alice",
                    category="speaker",
                    samples=(
                        EffectDemoSample(EventKind.SAY, 'Alice says, "Preview message."'),
                        EffectDemoSample(EventKind.POSE, "Alice previews the pose effect."),
                    ),
                )
            )

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


async def test_help_uses_topics_and_structured_command_and_plugin_details() -> None:
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
    tui.gateway_reconnect = AsyncMock()

    await tui._handle_client_command("alpha", "/help")
    await tui._handle_client_command("alpha", "/help config")
    await tui._handle_client_command("alpha", "/help fixture")

    help_text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "TFR help topics" in help_text
    assert "configuration — Gateway-owned settings" in help_text
    assert "! command" in help_text
    assert "/config — Open settings or manage Gateway configuration" in help_text
    assert "Usage: /config [summary|status|list|show" in help_text
    assert "/fixture — run the harmless fixture" in help_text
    assert "Availability: fixture plugin" in help_text
    assert "Help request: /help\n\nTFR help topics" in help_text
    assert "Help request: /help config\n\n/config" in help_text
    assert "Help request: /help fixture\n\n/fixture" in help_text
    assert " --" not in help_text


async def test_topic_help_explains_command_drill_down_and_separates_responses() -> None:
    tui = make_tui()
    tui.active_view.display.resize(width=100, height=100)
    tui.gateway_reconnect = AsyncMock()

    await tui._handle_client_command("alpha", "/help connections")
    await tui._handle_client_command("alpha", "/help gateway")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Open command details with /help COMMAND." in text
    assert "/gateway — Manage Gateway connections and profiles" in text
    assert "\n\nHelp request: /help gateway\n\n/gateway" in text
    assert "More help: /help config, /help ui" in text


async def test_contextual_help_filters_index_and_explains_unavailable_commands() -> None:
    tui = make_tui()

    index = tui.help_text()
    gateway_detail = tui.help_text("config")
    correction = tui.help_text("plguins")

    assert "configuration —" not in index
    assert "Availability: unavailable — connect this UI to a Gateway" in gateway_detail
    assert "Did you mean: plugins?" in correction


async def test_settings_browser_infers_current_ui_scope_and_saves_boolean() -> None:
    tui = make_tui()
    runtime = SimpleNamespace(
        config_list=AsyncMock(
            return_value=[
                {
                    "key": "ui.animations_enabled",
                    "type": "boolean",
                    "scope": "ui",
                    "validation": {"nullable": False},
                    "restart": "live",
                    "sensitive": False,
                }
            ]
        ),
        config_show=AsyncMock(
            return_value={
                "key": "ui.animations_enabled",
                "type": "boolean",
                "scope": "ui",
                "value": False,
                "source": "default",
            }
        ),
        config_status=AsyncMock(return_value={"revision": 4}),
        config_mutate=AsyncMock(
            side_effect=[
                {"revision": 4, "restart": "live", "requires_confirmation": True},
                {"revision": 5, "restart": "live", "requires_confirmation": False},
            ]
        ),
    )
    tui.service_runtime = runtime
    choices = iter(["browse", "Ui", "edit", "true", "current", None, None])
    tui._settings_choice = AsyncMock(side_effect=lambda **_kwargs: next(choices))
    tui._settings_confirm = AsyncMock(return_value=True)

    await tui._open_settings_menu("alpha")

    assert runtime.config_mutate.await_args_list[0].kwargs == {
        "value": True,
        "scope": "ui",
        "target": None,
        "expected_revision": 4,
        "confirm": False,
    }
    assert runtime.config_mutate.await_args_list[1].kwargs["confirm"] is True
    value_prompt = tui._settings_choice.await_args_list[3].kwargs
    assert value_prompt["text"] == "Current value: Off"
    assert value_prompt["values"] == [("true", "On"), ("false", "Off")]
    assert value_prompt["default"] == "false"
    target_prompt = tui._settings_choice.await_args_list[4].kwargs
    assert target_prompt["title"] == "Apply setting to"
    assert target_prompt["default"] == "current"
    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Saved Animations Enabled = true at revision 5" in text


async def test_settings_escape_returns_one_navigation_level() -> None:
    tui = make_tui()
    tui.service_runtime = SimpleNamespace(
        config_list=AsyncMock(
            return_value=[
                {
                    "key": "ui.mouse_mode",
                    "type": "string",
                    "scope": "ui",
                    "validation": {"choices": ["auto", "terminal", "tfr"]},
                    "restart": "client-restart",
                    "sensitive": False,
                }
            ]
        )
    )
    choices = iter(["browse", "Ui", None, None, None])
    tui._settings_choice = AsyncMock(side_effect=lambda **_kwargs: next(choices))
    tui._open_setting_editor = AsyncMock()

    await tui._open_settings_menu("alpha")

    assert [call.kwargs["title"] for call in tui._settings_choice.await_args_list] == [
        "TFR Settings",
        "TFR Settings",
        "TFR Settings",
        "TFR Settings",
    ]
    tui._open_setting_editor.assert_awaited_once()
    assert tui._settings_choice.await_args_list[2].kwargs["default"] == "Ui"
    assert tui._settings_choice.await_args_list[3].kwargs["default"] == "browse"


async def test_single_setting_category_opens_directly_and_returns_to_category() -> None:
    tui = make_tui()
    tui.service_runtime = SimpleNamespace(
        config_list=AsyncMock(
            return_value=[
                {
                    "key": "ui.mouse_mode",
                    "type": "string",
                    "scope": "ui",
                    "validation": {"choices": ["auto", "terminal", "tfr"]},
                    "restart": "client-restart",
                    "sensitive": False,
                }
            ]
        )
    )
    choices = iter(["browse", "Ui", None, None])
    tui._settings_choice = AsyncMock(side_effect=lambda **_kwargs: next(choices))
    tui._open_setting_editor = AsyncMock()

    await tui._open_settings_menu("alpha")

    tui._open_setting_editor.assert_awaited_once()
    assert tui._settings_choice.await_args_list[2].kwargs["default"] == "Ui"
    assert all(call.kwargs["title"] != "Ui" for call in tui._settings_choice.await_args_list)


async def test_plugins_enabled_uses_discovered_checkbox_manager() -> None:
    tui = make_tui()
    runtime = SimpleNamespace(
        plugins_catalog=AsyncMock(
            return_value=[
                {"name": "speaker_effects", "loaded": True, "failed": False},
                {"name": "spotlight", "loaded": False, "failed": False},
            ]
        )
    )
    tui.service_runtime = runtime
    tui._settings_multiselect = AsyncMock(return_value=["speaker_effects", "spotlight"])
    tui._save_settings_value = AsyncMock(return_value=True)
    setting = {"key": "plugins.enabled", "scope": "shared", "type": "string-list"}

    await tui._edit_enabled_plugins(
        "alpha", setting, {"value": ["speaker_effects"]}
    )

    prompt = tui._settings_multiselect.await_args.kwargs
    assert prompt["title"] == "Manage plugins"
    assert prompt["defaults"] == ["speaker_effects"]
    assert prompt["values"] == [
        ("speaker_effects", "speaker_effects · available"),
        ("spotlight", "spotlight · available"),
    ]
    assert prompt["apply_text"] == "Apply"
    tui._save_settings_value.assert_awaited_once_with(
        "alpha",
        setting,
        ["speaker_effects", "spotlight"],
        old_value=["speaker_effects"],
    )


async def test_speaker_effect_form_builds_comet_rule_without_json() -> None:
    tui = make_tui()
    inputs = iter(["Barnaby", "1.2", "3"])
    tui._settings_input = AsyncMock(side_effect=lambda **_kwargs: next(inputs))
    tui._settings_choice = AsyncMock(return_value="comet")
    tui._settings_color = AsyncMock(side_effect=["#ffdd76", "#ffffff"])
    tui._settings_multiselect = AsyncMock(side_effect=[[], ["say", "pose"]])

    rule = await tui._speaker_rule_form()

    assert rule == {
        "speaker": "Barnaby",
        "effect": "comet",
        "color": "#ffdd76",
        "accent_color": "#ffffff",
        "duration_seconds": 1.2,
        "trail_width": 3.0,
        "kinds": ["say", "pose"],
    }
    assert tui._settings_choice.await_args.kwargs["default"] is None


async def test_speaker_effect_form_removes_fields_invalid_for_new_effect() -> None:
    tui = make_tui()
    tui._settings_input = AsyncMock(return_value="Adrick")
    tui._settings_choice = AsyncMock(return_value="color_pulse")
    tui._settings_color = AsyncMock(side_effect=["#005f87", "#5fd7ff"])
    tui._settings_multiselect = AsyncMock(side_effect=[["glob"], ["say", "pose"]])

    rule = await tui._speaker_rule_form(
        {
            "speaker": "Adrick",
            "effect": "comet",
            "color": "#005f87",
            "accent_color": "#5fd7ff",
            "duration_seconds": 1.2,
            "trail_width": 3,
        }
    )

    assert rule is not None
    assert rule["effect"] == "color_pulse"
    assert "trail_width" not in rule
    assert tui._settings_choice.await_args.kwargs["default"] == "comet"


async def test_color_picker_shows_swatches_and_resolved_default() -> None:
    tui = make_tui()
    tui._settings_choice = AsyncMock(return_value="")

    assert await tui._settings_color(
        title="Speaker effect",
        label="Accent color",
        current=None,
        default_color="#d7ffff",
        default_label="Use effect default",
    ) == ""

    values = tui._settings_choice.await_args.kwargs["values"]
    default_fragments = values[-1][1]
    assert "#d7ffff" in fragment_list_to_text(default_fragments)
    assert any("bg:#d7ffff" in style for style, _text in default_fragments)


async def test_invalid_speaker_rule_can_be_deleted_from_recovery_editor() -> None:
    tui = make_tui()
    tui.service_runtime = SimpleNamespace(
        plugins_catalog=AsyncMock(return_value=[{"name": "speaker_effects", "loaded": False}])
    )
    tui._settings_choice = AsyncMock(side_effect=["rule:0", "delete", "save"])
    tui._settings_confirm = AsyncMock(return_value=True)
    tui._save_settings_batch = AsyncMock(return_value=True)
    setting = {"key": "plugins.config.speaker_effects.rules"}
    invalid = {
        "speaker": "Bob",
        "effect": "combo_godlike",
        "accent_color": "#ffffff",
    }

    await tui._edit_speaker_rules("alpha", setting, {"value": [invalid]})

    first_values = tui._settings_choice.await_args_list[0].kwargs["values"]
    assert "Invalid: unsupported speaker effect" in first_values[1][1]
    tui._save_settings_batch.assert_awaited_once_with(
        "alpha", setting, [], old_value=[invalid]
    )


async def test_unknown_command_suggests_correction_and_next_action() -> None:
    tui = make_tui()

    await tui._handle_client_command("alpha", "/plguins")
    await tui._handle_client_command("alpha", "/utterlyunknown")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Did you mean /plugins?" in text
    assert "Use /help to browse available comman" in text


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
    assert "UI update available [stable]: 1.2.3 (running 1.0.0" in text
    assert "Gateway update available [stable]: 1.2.3 (running 1.1.0" in text
    assert "Plugin update available: owner/plugins 0.5.0 (current 0.4.0)" in text


async def test_update_check_reports_configured_candidate_channel(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 1,
        "project": "tfr",
        "channel": "candidate",
        "version": "1.2.3",
        "tag": "v1.2.3-candidate.1",
        "commit": "a" * 40,
        "protocol": {"minimum": 1, "maximum": 1},
        "release_url": "https://github.com/Blaag/tfr/releases/tag/v1.2.3-candidate.1",
        "artifact": {
            "url": "https://example.invalid/tfr.whl",
            "size": 10,
            "sha256": "b" * 64,
        },
    }

    def fetch(_url: str, _etag: str | None, _timeout: float) -> object:
        import json

        return SimpleNamespace(content=json.dumps(manifest).encode(), etag=None)

    checker = UpdateChecker(
        UpdateConfig(channel="candidate", state_directory=tmp_path),
        build=BuildIdentity("1.2.3", "a" * 40),
        fetch=fetch,  # type: ignore[arg-type]
    )
    tui = make_tui(
        update_checker=checker,
        gateway_build=BuildIdentity("1.2.3", "a" * 40),
    )

    await tui._handle_client_command("alpha", "/update check")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Checking for candidate TFR updates" in text
    assert "Checking for stable TFR updates" not in text
    assert "UI 1.2.3 (aaaaaaaaaaaa) [candidate]: up to date" in text
    assert "Gateway 1.2.3 (aaaaaaaaaaaa) [candidate]: up to date" in text
    assert "Check or coordinate managed updates" in tui.help_text("update")


async def test_web_commands_manage_devices_through_attached_gateway() -> None:
    device_id = UUID("eb4fd272-a20a-444e-8b4d-93f2e0ad2713")

    class WebRuntime:
        def __init__(self) -> None:
            self.revoked: list[UUID] = []
            self.forgotten: list[str] = []

        async def web_status(self) -> dict[str, object]:
            return {
                "enabled": True,
                "origin": "https://gateway.example.ts.net",
                "device_count": 1,
            }

        async def config_summary(self) -> dict[str, object]:
            return {
                "setting_count": 80,
                "template_count": 20,
                "sensitive_count": 4,
                "unset_required_count": 0,
                "sources": {"default": 60, "jsonc": 18, "managed": 2},
                "scopes": {"gateway": 30, "shared": 10, "ui": 40},
            }

        async def config_list(self, filter_value: str | None = None) -> list[dict[str, object]]:
            assert filter_value == "shared"
            return [
                {
                    "key": "ui.spellcheck.enabled",
                    "type": "boolean",
                    "scope": "shared",
                    "category": "ui",
                    "sensitive": False,
                    "restart": "live",
                },
                {
                    "key": "plugins.enabled",
                    "type": "string-list",
                    "scope": "shared",
                    "category": "plugins",
                    "sensitive": False,
                    "restart": "client-reload",
                },
            ]

        async def config_show(self, key: str, *, ui: str | None = None) -> dict[str, object]:
            assert key == "ui.mouse_mode"
            assert ui == "office-mac · Terminal"
            return {
                "key": key,
                "descriptor_key": key,
                "type": "string",
                "scope": "ui",
                "consumers": ["native-ui"],
                "restart": "live",
                "sensitive": False,
                "value": "terminal",
                "source": "jsonc",
                "source_detail": "/config.jsonc",
                "ui": {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "name": "office-mac · Terminal",
                },
            }

        async def config_status(self) -> dict[str, object]:
            return {
                "revision": 4,
                "counts": {"current": 1, "behind": 1, "ahead": 0, "unreported": 1},
                "uis": [
                    {
                        "display_name": "office-mac · Terminal",
                        "client_type": "terminal",
                        "applied_revision": 4,
                        "convergence": "current",
                    },
                    {
                        "display_name": "Chris iPhone · PWA",
                        "client_type": "pwa",
                        "applied_revision": 2,
                        "convergence": "behind",
                    },
                    {
                        "display_name": "old laptop · Terminal",
                        "client_type": "terminal",
                        "applied_revision": None,
                        "convergence": "unreported",
                    },
                ],
            }

        async def pair_web_device(self, label: str) -> str:
            assert label == "Chris iPhone"
            return "https://gateway.example.ts.net/#pair=test-secret"

        async def list_web_devices(self) -> list[dict[str, object]]:
            return [
                {
                    "device_id": str(device_id),
                    "label": "Chris iPhone",
                    "tailscale_login": "chris@example.com",
                    "allowed_worlds": ["alpha"],
                    "active": True,
                    "created_at": "2026-10-15T00:00:00Z",
                    "last_used_at": "2026-10-16T00:00:00Z",
                    "expires_at": "2027-01-01T00:00:00Z",
                }
            ]

        async def list_uis(self) -> list[dict[str, object]]:
            return [
                {
                    "ui_id": "11111111-1111-4111-8111-111111111111",
                    "display_name": "office-mac · Terminal",
                    "client_type": "terminal",
                    "platform": "office-mac",
                    "last_seen_at": "2026-10-16T00:00:00Z",
                    "applied_config_revision": 4,
                },
                {
                    "ui_id": "22222222-2222-4222-8222-222222222222",
                    "display_name": "Chris iPhone · PWA",
                    "client_type": "pwa",
                    "platform": "Chris iPhone",
                    "last_seen_at": "2026-10-15T00:00:00Z",
                    "applied_config_revision": None,
                },
            ]

        async def show_ui(self, name: str) -> dict[str, object]:
            assert name == "office-mac · Terminal"
            return {
                "ui_id": "11111111-1111-4111-8111-111111111111",
                "display_name": "office-mac · Terminal",
                "client_type": "terminal",
                "platform": "office-mac",
                "versions": {"protocol": "2", "tfr": "0.1.67"},
                "capabilities": ["color", "timeline"],
                "created_at": "2026-10-14T00:00:00Z",
                "last_seen_at": "2026-10-16T00:00:00Z",
                "applied_config_revision": 4,
            }

        async def rename_ui(self, name: str) -> dict[str, object]:
            assert name == "Writing desk"
            return {"display_name": name}

        async def rename_other_ui(self, target: str, name: str) -> dict[str, object]:
            assert target == "Chris iPhone · PWA"
            assert name == "Chris phone"
            return {"display_name": name}

        async def forget_ui(self, target: str, *, confirm: bool) -> dict[str, object]:
            assert confirm is True
            self.forgotten.append(target)
            return {"display_name": target}

        async def revoke_web_device(self, received: UUID) -> None:
            self.revoked.append(received)

        async def diagnostics(self, category: str | None = None) -> list[dict[str, object]]:
            return [
                {
                    "timestamp": "2026-10-16T00:00:00Z",
                    "severity": "warning",
                    "component": "presentation",
                    "code": "client_rejected",
                    "message": "The PWA rejected Wedge's presentation",
                    "client": "pwa",
                }
            ]

        async def clear_diagnostics(self) -> None:
            return None

    runtime = WebRuntime()
    tui = make_tui(service_runtime=runtime)
    tui.active_view.display.max_rows = 500
    tui.active_view.display.resize(width=120, height=300)

    for command in (
        "/web status",
        "/config summary",
        "/config list shared",
        '/config show ui.mouse_mode --ui "office-mac · Terminal"',
        "/config status",
        "/web repair",
        '/web pair "Chris iPhone"',
        "/web devices",
        "/ui list",
        "/ui show office-mac · Terminal",
        '/ui rename "Writing desk"',
        '/ui rename "Chris iPhone · PWA" "Chris phone"',
        '/ui forget "Chris phone"',
        '/ui forget "Chris phone" confirm',
        f"/web revoke {device_id}",
        "/diagnostics presentations",
    ):
        await tui._handle_client_command("alpha", command)

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "PWA origin: https://gateway.example.ts.net" in text
    assert "Gateway configuration inventory" in text
    assert "Settings: 80 (+ 20 dynamic templates)" in text
    assert "Sources: default: 60, jsonc: 18, managed: 2" in text
    assert "Configuration settings — shared" in text
    assert "ui.spellcheck.enabled (boolean; shared)" in text
    assert "plugins.enabled (string-list; shared, client-reload)" in text
    assert "Configuration: ui.mouse_mode" in text
    assert "Value: 'terminal'" in text
    assert "Source: jsonc (/config.jsonc)" in text
    assert "Type: string; scope: ui" in text
    assert "Consumers: native-ui" in text
    assert "Restart: live" in text
    assert "UI: office-mac · Terminal" in text
    assert "Configuration convergence — revision 4" in text
    assert "current: 1; behind: 1; ahead: 0; unreported: 1" in text
    assert "Chris iPhone · PWA [pwa]: behind (applied 2)" in text
    assert "old laptop · Terminal [terminal]: unreported (applied not reported)" in text
    assert "PWA repair page:\nhttps://gateway.example.ts.net/repair.html" in text
    assert "https://gateway.example.ts.net/#pair=test-secret" in text
    assert any(glyph in text for glyph in "▀▄█")
    assert "Chris iPhone  chris@example.com" in text
    assert "active now; created: 2026-10-15T00:00:00Z" in text
    assert f"Revoked web device {device_id}" in text
    assert runtime.revoked == [device_id]
    assert "Gateway-known UIs" in text
    assert "office-mac · Terminal  [terminal]" in text
    assert "config revision: 4" in text
    assert "Chris iPhone · PWA  [pwa]" in text
    assert "config revision: not reported" in text
    assert "UI: office-mac · Terminal" in text
    assert "Versions: protocol 2, tfr 0.1.67" in text
    assert "Capabilities: color, timeline" in text
    assert "Renamed this UI to Writing desk" in text
    assert "Renamed Chris iPhone · PWA to Chris phone" in text
    assert "Confirm with: /ui forget 'Chris phone' confirm" in text
    assert "Forgot UI Chris phone" in text
    assert runtime.forgotten == ["Chris phone"]
    assert "pwa/presentation  client_rejected" in text


async def test_config_show_never_displays_secret_value() -> None:
    class SecretRuntime:
        async def config_show(self, key: str, *, ui: str | None = None) -> dict[str, object]:
            assert key == "worlds.alpha.login.password"
            assert ui is None
            return {
                "key": key,
                "descriptor_key": "worlds.*.login.password",
                "type": "string",
                "scope": "secret",
                "consumers": ["gateway"],
                "restart": "live",
                "sensitive": True,
                "value": None,
                "redacted": True,
                "source": "jsonc",
                "source_detail": "/worlds.jsonc",
            }

    tui = make_tui(service_runtime=SecretRuntime())
    tui.active_view.display.resize(width=100, height=20)

    await tui._handle_client_command("alpha", "/config show worlds.alpha.login.password")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Configuration: worlds.alpha.login.password" in text
    assert "Value: <redacted>" in text
    assert "super-private-value" not in text


async def test_config_show_identifies_managed_override_revision() -> None:
    class ManagedRuntime:
        async def config_show(self, key: str, *, ui: str | None = None) -> dict[str, object]:
            assert key == "ui.spellcheck.enabled"
            assert ui is None
            return {
                "key": key,
                "descriptor_key": key,
                "type": "boolean",
                "scope": "shared",
                "consumers": ["native-ui", "pwa"],
                "restart": "live",
                "sensitive": False,
                "value": True,
                "source": "managed",
                "source_detail": "managed override revision 7",
            }

    tui = make_tui(service_runtime=ManagedRuntime())
    tui.active_view.display.resize(width=100, height=20)

    await tui._handle_client_command("alpha", "/config show ui.spellcheck.enabled")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Value: True" in text
    assert "Source: managed (managed override revision 7)" in text


async def test_config_mutations_preview_confirm_list_and_undo() -> None:
    class MutationRuntime:
        def __init__(self) -> None:
            self.revision = 0

        async def config_show(self, key: str, *, ui: str | None = None) -> dict[str, object]:
            return {
                "key": key,
                "scope": "shared",
                "restart": "live",
                "consumers": ["native-ui", "pwa"],
                "type": "boolean",
                "sensitive": False,
                "source": "default",
                "source_detail": "built-in default",
                "value": False,
            }

        async def config_status(self) -> dict[str, object]:
            return {"revision": self.revision, "counts": {}, "uis": []}

        async def config_mutate(
            self,
            operation: str,
            key: str,
            *,
            value: object = None,
            scope: str,
            target: str | None,
            expected_revision: int,
            confirm: bool,
        ) -> dict[str, object]:
            assert operation in {"set", "reset"}
            assert key == "ui.spellcheck.enabled"
            assert value is (True if operation == "set" else None)
            assert scope == "shared"
            assert target is None
            assert expected_revision == self.revision
            if confirm:
                self.revision += 1
            return {
                "revision": self.revision,
                "operation": operation,
                "restart": "live",
                "requires_confirmation": not confirm,
            }

        async def config_changes(self, limit: int = 50) -> dict[str, object]:
            assert limit == 5
            return {
                "revision": self.revision,
                "changes": [
                    {
                        "revision": 1,
                        "operation": "set",
                        "key": "ui.spellcheck.enabled",
                        "scope": "shared",
                        "actor_ui_name": "Writing desk",
                        "restart": "live",
                    }
                ],
            }

        async def config_undo(self, *, expected_revision: int, confirm: bool) -> dict[str, object]:
            assert expected_revision == self.revision
            if confirm:
                self.revision += 1
            return {
                "revision": self.revision,
                "key": "ui.spellcheck.enabled",
                "restart": "live",
                "requires_confirmation": not confirm,
            }

    runtime = MutationRuntime()
    tui = make_tui(service_runtime=runtime)
    tui.active_view.display.resize(width=120, height=40)

    for command in (
        "/config set ui.spellcheck.enabled true",
        "/config set ui.spellcheck.enabled true --confirm",
        "/config reset ui.spellcheck.enabled",
        "/config reset ui.spellcheck.enabled --confirm",
        "/config changes 5",
        "/config undo",
        "/config undo confirm",
    ):
        await tui._handle_client_command("alpha", command)

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Proposed set for ui.spellcheck.enabled; required action: live" in text
    assert "Saved ui.spellcheck.enabled at revision 1; required action: live" in text
    assert "Proposed reset for ui.spellcheck.enabled; required action: live" in text
    assert "Saved ui.spellcheck.enabled at revision 2; required action: live" in text
    assert "r1 set ui.spellcheck.enabled [shared] by Writing desk — live" in text
    assert "Confirm with: /config undo confirm" in text
    assert "Undid ui.spellcheck.enabled at revision 3; action: live" in text


async def test_web_pair_explains_disabled_gateway_configuration_before_pairing() -> None:
    class DisabledWebRuntime:
        def __init__(self) -> None:
            self.pair_called = False

        async def web_status(self) -> dict[str, object]:
            return {"enabled": False, "origin": None, "device_count": 0}

        async def pair_web_device(self, _label: str) -> str:
            self.pair_called = True
            raise AssertionError("pairing must not be attempted while web is disabled")

    runtime = DisabledWebRuntime()
    tui = make_tui(service_runtime=runtime)

    await tui._handle_client_command("alpha", '/web pair "New phone"')

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Web device pairing is disabled on the Gateway" in text
    assert "web_gateway.mode" in text
    assert "on-demand" in text.replace("\n", "")
    assert "active config.jsonc" in text
    assert runtime.pair_called is False


async def test_gag_commands_list_and_persist_with_gateway_revision() -> None:
    class GagRuntime:
        def __init__(self) -> None:
            self.revision = 4
            self.patterns = ["from base"]
            self.mutations: list[tuple[str, str, str | None, int | None]] = []

        async def manage_gag(
            self,
            world: str,
            operation: str,
            *,
            pattern: str | None = None,
            expected_revision: int | None = None,
        ) -> dict[str, object]:
            if operation == "list":
                return {
                    "revision": self.revision,
                    "patterns": list(self.patterns),
                    "changed": False,
                }
            self.mutations.append((world, operation, pattern, expected_revision))
            self.revision += 1
            if operation == "add" and pattern is not None:
                self.patterns.append(pattern)
            return {
                "revision": self.revision,
                "patterns": list(self.patterns),
                "changed": True,
                "restart_required": False,
            }

    runtime = GagRuntime()
    tui = make_tui(service_runtime=runtime)

    await tui._handle_client_command("alpha", "/gag list")
    await tui._handle_client_command("alpha", "/gag add ^bing goes")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Gag patterns for alpha (managed revision 4)" in text
    assert "'from base'" in text
    assert runtime.mutations == [("alpha", "add", "^bing goes", 4)]
    assert "Saved in Gateway-managed settings (revision 5; active now)" in text
    assert "Canonical logging and agent processing are unchanged" in text


async def test_bare_update_reports_the_coordinated_release_link(tmp_path: Path) -> None:
    checker = UpdateChecker(UpdateConfig(state_directory=tmp_path))

    class UpdateRuntime:
        async def request_update(self, *, force: bool = False) -> dict[str, str]:
            assert force is False
            return {
                "version": "1.2.3",
                "release_url": "https://example.invalid/tfr/v1.2.3",
            }

    tui = make_tui(update_checker=checker)
    tui.service_runtime = UpdateRuntime()  # type: ignore[assignment]
    tui.active_view.display.resize(width=100, height=20)

    await tui._handle_client_command("alpha", "/update")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Checking for stable updates on the Gateway and all connected UIs" in text
    assert "Update 1.2.3 transaction started" in text
    assert "connected UIs and the Gateway are preparing" in text
    assert "https://example.invalid/tfr/v1.2.3" in text.replace("\n", "")


async def test_bare_update_reports_when_everything_is_current(tmp_path: Path) -> None:
    checker = UpdateChecker(UpdateConfig(state_directory=tmp_path))

    class UpdateRuntime:
        async def request_update(self, *, force: bool = False) -> dict[str, object]:
            assert force is False
            return {"updated": False}

    tui = make_tui(update_checker=checker)
    tui.service_runtime = UpdateRuntime()  # type: ignore[assignment]
    tui.active_view.display.resize(width=100, height=20)

    await tui._handle_client_command("alpha", "/update")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Checking for stable updates on the Gateway and all connected UIs" in text
    assert "TFR and stable-auto plugins are up to date" in text
    assert "transaction started" not in text


async def test_force_update_is_candidate_only_and_passes_force_to_gateway(
    tmp_path: Path,
) -> None:
    calls: list[bool] = []

    class UpdateRuntime:
        async def request_update(self, *, force: bool = False) -> dict[str, object]:
            calls.append(force)
            return {"updated": False}

    candidate = make_tui(
        update_checker=UpdateChecker(
            UpdateConfig(channel="candidate", state_directory=tmp_path / "candidate")
        )
    )
    candidate.service_runtime = UpdateRuntime()  # type: ignore[assignment]

    await candidate._handle_client_command("alpha", "/update force")

    assert calls == [True]
    candidate_text = fragment_list_to_text(candidate.active_view.display.formatted_text())
    assert "Checking for candidate updates on the Gateway and all connected UIs" in candidate_text

    stable = make_tui(
        update_checker=UpdateChecker(UpdateConfig(state_directory=tmp_path / "stable"))
    )
    stable.service_runtime = UpdateRuntime()  # type: ignore[assignment]
    stable.active_view.display.resize(width=100, height=20)

    await stable._handle_client_command("alpha", "/update force")

    assert calls == [True]
    text = fragment_list_to_text(stable.active_view.display.formatted_text())
    assert "available only on the candidate channel" in text


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
    assert fragment_list_to_text(view.display.formatted_text()) == "Recall 2\nsecond\nthird"
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
    assert lines == ["", "", "", "", "", "", "", "Recall 2", "second", "third"]


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

    assert fragment_list_to_text(view.display.formatted_text()).endswith("Recall 1\nsource line")
    recalled = view.display.recent_rows(1)
    assert fragment_list_to_text(list(recalled[0])) == "source line"


async def test_recall_uses_only_the_requested_world_history() -> None:
    tui = make_tui()
    tui.views["alpha"].display.append("alpha line")
    tui.views["beta"].display.append("beta line")

    await tui._handle_client_command("beta", "/recall 1")

    beta_text = fragment_list_to_text(tui.views["beta"].display.formatted_text())
    assert beta_text.endswith("Recall 1\nbeta line")
    assert "alpha line" not in beta_text


async def test_recall_excludes_local_client_command_notices() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.append("world output")

    await tui._handle_client_command("alpha", "/help")
    await tui._handle_client_command("alpha", "/recall 1")

    text = fragment_list_to_text(view.display.formatted_text())
    assert text.endswith("Recall 1\nworld output")
    assert "TFR commands" not in text.rsplit("Recall 1", 1)[-1]


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


async def test_gateway_profile_commands_list_rename_export_and_forget(tmp_path: Path) -> None:
    from tfr.gateway_profiles import GatewayConnectionProfile, GatewayProfileStore

    store = GatewayProfileStore(tmp_path / "connections")
    store.add(
        GatewayConnectionProfile(
            name="home",
            host="gateway.example",
            port=7347,
            tls_server_name="gateway.example",
            tls_ca=None,
            credential_reference="credential:pending",
            ui_id=uuid4(),
            ui_name="Home Terminal",
        ),
        "a" * 64,
    )
    tui = make_tui()
    tui.gateway_profiles = store

    await tui._handle_client_command("alpha", "/gateway profiles")
    await tui._handle_client_command("alpha", "/gateway rename home house")
    await tui._handle_client_command("alpha", "/gateway profiles export")
    await tui._handle_client_command("alpha", "/gateway forget house --confirm")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "gateway.example:7347" in text
    assert "Renamed Gateway profile to house" in text
    assert "credential_reference" not in text
    assert "Forgot Gateway profile house" in text


async def test_gateway_restart_command_is_immediate_for_owner_ui() -> None:
    tui = make_tui()
    runtime = SimpleNamespace(restart_gateway=AsyncMock(return_value={"restarting": True}))
    tui.service_runtime = runtime

    assert tui.gateway_profiles is None
    await tui._handle_client_command("alpha", "/gateway restart")

    runtime.restart_gateway.assert_awaited_once_with()
    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Gateway restart requested" in text


async def test_config_migration_commands_submit_choose_apply_and_report_status() -> None:
    tui = make_tui()
    preview = {
        "base_revision": 4,
        "accepted": [],
        "conflicts": [
            {
                "key": "plugins.config.speaker_effects.rules",
                "candidates": [
                    {
                        "ui_name": "Gateway",
                        "value": [
                            {"speaker": "Blood", "effect": "shimmer"},
                            {"speaker": "Jarin", "effect": "rainbow_wave"},
                        ],
                    },
                    {
                        "ui_name": "Laptop Terminal",
                        "value": [{"speaker": "Barnaby", "effect": "comet"}],
                    },
                ],
            }
        ],
        "skipped": [],
    }
    runtime = SimpleNamespace(
        config_migrate=AsyncMock(return_value=preview),
        config_migration_preview=AsyncMock(
            return_value={**preview, "conflicts": [], "accepted": [{}]}
        ),
        config_status=AsyncMock(return_value={"revision": 4}),
        config_migration_apply=AsyncMock(
            return_value={
                "revision": 5,
                "applied": True,
                "restart": "gateway-restart",
                "restarting": True,
            }
        ),
        config_migration_status=AsyncMock(
            return_value={
                "revision": 5,
                "proposal_count": 2,
                "applied_revision": 5,
                "proposals": [],
            }
        ),
    )
    tui.service_runtime = runtime

    await tui._handle_client_command("alpha", "/config migrate")
    conflict_text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Migration needs a decision (1 remaining). Nothing changed." in conflict_text
    assert "plugins.config.speaker_effects.rules" in conflict_text
    assert "2 speaker rules: Blood — Shimmer, Jarin — Rainbow Wave" in conflict_text
    assert "Laptop Terminal" in conflict_text
    assert "migrate choose" in conflict_text
    assert "migrate skip" in conflict_text
    assert '"effect"' not in conflict_text
    await tui._handle_client_command(
        "alpha", "/config migrate choose plugins.config.speaker_effects.rules Gateway"
    )
    await tui._handle_client_command("alpha", "/config migrate apply")
    await tui._handle_client_command("alpha", "/config migration-status")

    runtime.config_migration_preview.assert_awaited_with(
        {"plugins.config.speaker_effects.rules": "Gateway"}
    )
    runtime.config_migration_apply.assert_awaited_with(
        expected_revision=4,
        choices={"plugins.config.speaker_effects.rules": "Gateway"},
        confirm=True,
    )
    text = "\n".join(
        fragment_list_to_text(row) for row in tui.active_view.display.rows
    )
    assert "Migration applied atomically at revision 5" in text
    assert "Gateway restart requested" in text
    assert "reconnecting" in text
    assert "proposals: 2" in text


async def test_central_plugin_commands_list_show_and_enable_spotlight() -> None:
    tui = make_tui()
    plugin = {
        "name": "spotlight",
        "version": "1.0",
        "summary": "Portable spotlight clear effect",
        "category": "effects",
        "enabled": False,
        "loaded": False,
        "failed": False,
        "portable": True,
        "capabilities": ["screen-clear"],
    }
    runtime = SimpleNamespace(
        plugins_catalog=AsyncMock(return_value=[plugin]),
        plugin_show=AsyncMock(
            return_value={
                "plugin": plugin,
                "clients": [
                    {
                        "ui_name": "Old Terminal",
                        "compatible": False,
                        "reason": "plugin capability not reported",
                    }
                ],
            }
        ),
        config_status=AsyncMock(return_value={"revision": 3}),
        plugin_enablement=AsyncMock(
            side_effect=[
                {
                    "name": "spotlight",
                    "enabled": True,
                    "revision": 3,
                    "requires_confirmation": True,
                    "restart": "client-reload",
                },
                {
                    "name": "spotlight",
                    "enabled": True,
                    "revision": 4,
                    "requires_confirmation": False,
                    "restart": "client-reload",
                },
            ]
        ),
    )
    tui.service_runtime = runtime
    tui.gateway_reconnect = AsyncMock()

    await tui._handle_client_command("alpha", "/plugins available")
    await tui._handle_client_command("alpha", "/plugins show spotlight")
    await tui._handle_client_command("alpha", "/plugins enable spotlight")
    await tui._handle_client_command("alpha", "/plugins enable spotlight confirm")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "spotlight 1.0" in text
    assert "incompatible: Old Terminal" in text
    assert "/plugins enable spotlight confirm" in text
    assert "enabled at revision 4" in text


async def test_plugin_catalog_groups_states_and_omits_unknown_versions() -> None:
    tui = make_tui()
    tui.active_view.display.resize(width=100, height=100)
    tui.service_runtime = SimpleNamespace(
        plugins_catalog=AsyncMock(
            return_value=[
                {
                    "name": "acid_rain",
                    "version": "unknown",
                    "loaded": True,
                    "failed": False,
                    "portable": True,
                },
                {
                    "name": "spotlight",
                    "version": "unknown",
                    "loaded": False,
                    "failed": False,
                    "portable": True,
                },
            ]
        )
    )

    await tui._handle_client_command("alpha", "/plugins available")

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Command result: /plugins available" in text
    assert "Plugins: available (2)" in text
    assert "Loaded (1)\n  acid_rain  [portable]" in text
    assert "Disabled (1)\n  spotlight  [portable]" in text
    assert "Details: /plugins show NAME" in text
    assert "unknown" not in text
    assert "-- Plugins" not in text


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

    help_text = tui.help_text("worlds")
    assert "/world — Switch to a named world" in help_text
    assert {world for world, _text in tui._startup_notices} == {"alpha", "beta"}
    assert all(
        "World-switch aliases shadowed by commands" in text for _world, text in tui._startup_notices
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

    view.input_buffer.text = "unfinished draft"
    view.recent_commands.append("look")

    tui.start_screen_clear("alpha")

    task = tui._screen_clear_task
    assert task is not None
    assert tui._screen_clear_world == "alpha"
    assert "old text" in fragment_list_to_text(tui.screen_clear_text())
    assert view.display.screen_is_cleared is True
    assert view.input_buffer.text == "unfinished draft"
    assert tuple(view.recent_commands) == ("look",)

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

    # /recall left only 3 buffered rows ("Recall 2", "second", "third") in
    # a 10-row pane. The animation's geometry must still span the pane's
    # true height, with the recalled text anchored at the bottom -- not
    # shrink to only the buffered rows, which would put the animation's
    # floor at the last recalled row instead of the pane's actual bottom.
    assert len(captured[0].lines) == 10
    assert captured[0].lines[:7] == ("",) * 7
    assert captured[0].lines[7:] == ("Recall 2", "second", "third")
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


def test_blank_enter_pushes_blank_rows_through_recent_input() -> None:
    tui = make_tui()
    view = tui.active_view
    view.recent_commands.extend(("look", "say hello", "inventory"))

    for _ in range(view.recent_input_lines):
        view.input_buffer.validate_and_handle()

    assert tuple(view.recent_commands) == ("", "", "")
    assert fragment_list_to_text(view.recent_input_text()) == "\n\n"


async def test_blank_enter_preserves_immediate_up_arrow_history() -> None:
    with create_pipe_input() as input:
        tui = make_tui(input=input)
        view = tui.active_view
        view.input_buffer.history.append_string("look")
        running = asyncio.create_task(tui.run())

        async def history_loaded() -> None:
            while view.input_buffer.working_index == 0:
                await asyncio.sleep(0.005)

        try:
            await asyncio.wait_for(history_loaded(), timeout=1)
            input.send_text("\r")
            input.send_bytes(b"\x1b[A")

            async def history_recalled() -> None:
                while view.input_buffer.text != "look":
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(history_recalled(), timeout=1)
            assert tuple(view.recent_commands) == ("",)
        finally:
            input.send_bytes(b"\x11")
            await asyncio.wait_for(running, timeout=1)


async def test_ctrl_l_clears_visible_input_and_output_but_preserves_history() -> None:
    with create_pipe_input() as input:
        tui = make_tui(input=input)
        view = tui.active_view
        view.input_buffer.history.append_string("look")
        view.recent_commands.extend(("look", "say hello"))
        view.display.append("old output")
        running = asyncio.create_task(tui.run())

        async def history_loaded() -> None:
            while view.input_buffer.working_index == 0:
                await asyncio.sleep(0.005)

        try:
            await asyncio.wait_for(history_loaded(), timeout=1)
            input.send_text("unfinished command")

            async def draft_entered() -> None:
                while view.input_buffer.text != "unfinished command":
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(draft_entered(), timeout=1)
            input.send_bytes(b"\x0c")

            async def screen_cleared() -> None:
                while not view.display.screen_is_cleared:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(screen_cleared(), timeout=1)
            assert view.input_buffer.text == ""
            assert tuple(view.recent_commands) == ()
            assert fragment_list_to_text(view.recent_input_text()) == "\n\n"

            input.send_bytes(b"\x1b[A")

            async def history_recalled() -> None:
                while view.input_buffer.text != "look":
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(history_recalled(), timeout=1)
        finally:
            input.send_bytes(b"\x11")
            await asyncio.wait_for(running, timeout=1)


async def test_clear_command_clears_visible_input_and_output() -> None:
    tui = make_tui()
    view = tui.active_view
    view.input_buffer.text = "unfinished command"
    view.recent_commands.extend(("look", "say hello"))
    view.display.append("old output")

    await tui._handle_client_command("alpha", "/clear")

    assert view.input_buffer.text == ""
    assert tuple(view.recent_commands) == ()
    assert fragment_list_to_text(view.recent_input_text()) == "\n\n"
    assert view.display.screen_is_cleared is True


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


def test_tab_pages_paused_output_when_input_is_empty() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=40, height=3)
    for index in range(6):
        view.display.append(f"line {index}")
    assert view.display.pager.mode is PagerMode.PAUSED
    assert view.display.pager.more_rows == 3
    binding = next(
        binding
        for binding in tui.application.key_bindings.bindings
        if Keys.ControlI in binding.keys
    )

    assert binding.filter() is True
    binding.handler(SimpleNamespace(app=tui.application))

    assert view.display.pager.mode is PagerMode.PAUSED
    assert view.display.pager.more_rows == 1

    assert binding.filter() is True
    binding.handler(SimpleNamespace(app=tui.application))

    assert view.display.pager.mode is PagerMode.FOLLOW
    assert view.display.pager.more_rows == 0


def test_tab_pages_manually_scrolled_output_when_input_is_empty() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=40, height=3)
    for index in range(8):
        view.display.append(f"line {index}")
    view.display.pager.jump_to_end()
    view.display.pager.scroll_rows(-2)
    assert view.display.pager.mode is PagerMode.SCROLLED
    assert view.display.pager.more_rows == 2
    binding = next(
        binding
        for binding in tui.application.key_bindings.bindings
        if Keys.ControlI in binding.keys
    )

    assert binding.filter() is True
    binding.handler(SimpleNamespace(app=tui.application))

    assert view.display.pager.mode is PagerMode.FOLLOW
    assert view.display.pager.more_rows == 0


def test_tab_paging_filter_preserves_normal_tab_when_paging_is_unavailable() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=40, height=3)
    for index in range(6):
        view.display.append(f"line {index}")
    binding = next(
        binding
        for binding in tui.application.key_bindings.bindings
        if Keys.ControlI in binding.keys
    )

    view.input_buffer.text = "draft"
    assert binding.filter() is False

    view.input_buffer.reset()
    tui.inspector_agent = "agent"
    assert binding.filter() is False

    tui.inspector_agent = None
    tui.image_preview = SimpleNamespace()
    assert binding.filter() is False

    tui.image_preview = None
    view.display.pager.jump_to_end()
    assert binding.filter() is False


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
        mouse_mode="tfr",
        spellcheck=SpellcheckConfig(),
        animations_enabled=True,
        low_bandwidth=False,
        speaking_streaks=SimpleNamespace(enabled=True),
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
    assert values["spellcheck"] == SpellcheckConfig()
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


async def test_bare_pose_infers_first_token_only_for_display_effects() -> None:
    tui = make_tui(server="bare")
    await add_speaker_effects(tui)
    tui.low_bandwidth = True
    event = Event(
        session_id=tui.active_view.session.session_id,
        world="alpha",
        connection_generation=1,
        sequence=0,
        direction=Direction.INBOUND,
        kind=EventKind.RAW_OUTPUT,
        canonical_text="Alice's cigarette goes out.",
        plain_text="Alice's cigarette goes out.",
        display_text="Alice's cigarette goes out.",
        parser_name="bare",
    )

    tui.handle_event(event)

    output = tui.active_view.output_text()
    alice = next((style, text) for style, text in output if text == "Alice")
    assert alice == ("fg:#a9914a", "Alice")
    assert event.kind is EventKind.RAW_OUTPUT
    assert event.provenance is None

    unrelated = replace(
        event,
        sequence=1,
        canonical_text="Someone mentions Alice later.",
        plain_text="Someone mentions Alice later.",
        display_text="Someone mentions Alice later.",
    )
    tui.handle_event(unrelated)
    assert not tui.active_view.display._entry_decorations[1]


def combo_say(tui: TfrTui, sequence: int, speaker: str = "Alice") -> Event:
    text = f'{speaker} says, "message {sequence}"'
    return Event(
        session_id=tui.active_view.session.session_id,
        world="alpha",
        connection_generation=1,
        sequence=sequence,
        direction=Direction.INBOUND,
        kind=EventKind.SAY,
        canonical_text=text,
        plain_text=text,
        display_text=text,
        provenance=Provenance(sender_name=speaker),
    )


def test_combo_streak_bolds_third_message_body_and_overlays_border() -> None:
    tui = make_tui()
    tui.active_view.display.resize(width=60, height=4)

    for sequence in range(1, 4):
        tui.handle_event(combo_say(tui, sequence))
    notice = tui._combo_notices["alpha"]
    tui._combo_notices["alpha"] = replace(notice, started_at=notice.started_at - 0.6)

    output = tui.active_view.output_text()
    message_style = next(style for style, text in output if text == "message 3")
    assert "bold" in message_style
    assert "bold" not in next(style for style, text in output if "Alice" in text)
    border = fragment_list_to_text(
        tui._border_text(tui.active_view, "output", BorderEdge.BOTTOM, 4)
    )
    assert "> Speaking Spree! <" in border


def test_combo_notice_is_confined_to_output_bottom_border() -> None:
    tui = make_tui()
    for sequence in range(1, 4):
        tui.handle_event(combo_say(tui, sequence))
    notice = tui._combo_notices["alpha"]
    tui._combo_notices["alpha"] = replace(notice, started_at=notice.started_at - 0.6)

    for panel in ("output", "input"):
        for edge in BorderEdge:
            rendered = fragment_list_to_text(tui._border_text(tui.active_view, panel, edge, 4))
            if panel == "output" and edge is BorderEdge.BOTTOM:
                assert "> Speaking Spree! <" in rendered
            else:
                assert "Speaking Spree" not in rendered
                assert "Spree" not in rendered


def test_combo_notice_sweep_renders_above_activity_and_more_labels() -> None:
    tui = make_tui()
    tui.active_view.display.resize(width=80, height=1)
    tui.views["beta"].unread_events = 4
    for index in range(4):
        tui.active_view.display.append(f"line {index}")
    for sequence in range(1, 7):
        tui.handle_event(combo_say(tui, sequence))
    notice = tui._combo_notices["alpha"]
    tui._combo_notices["alpha"] = replace(notice, started_at=time.monotonic() - 1.2)

    border = tui._border_text(tui.active_view, "output", BorderEdge.BOTTOM, 1)

    assert border[13][1] == " "
    assert "reverse" in border[13][0]


def test_combo_streak_counts_silently_while_low_bandwidth() -> None:
    tui = make_tui()
    tui.low_bandwidth = True
    for sequence in range(1, 4):
        tui.handle_event(combo_say(tui, sequence))

    assert not tui._combo_notices
    assert not tui.active_view.display._entry_decorations[-1]

    tui.low_bandwidth = False
    tui.handle_event(combo_say(tui, 4))

    assert tui._combo_notices["alpha"].text == "> Rampage! <"
    assert any(
        decoration.effect is TextEffectKind.COMBO_PULSE
        for decoration in tui.active_view.display._entry_decorations[-1]
    )


def test_combo_streak_counts_in_inactive_world_without_delayed_visual() -> None:
    tui = make_tui()
    for sequence in range(1, 4):
        event = replace(combo_say(tui, sequence), world="beta")
        tui.handle_event(event)

    assert "beta" not in tui._combo_notices
    tui.switch_world("beta")
    assert "> Speaking Spree! <" not in fragment_list_to_text(
        tui._border_text(tui.active_view, "output", BorderEdge.BOTTOM, 4)
    )

    tui.handle_event(replace(combo_say(tui, 4), world="beta"))
    assert tui._combo_notices["beta"].text == "> Rampage! <"


def test_tinymux_local_says_and_poses_share_combo_streak() -> None:
    tui = make_tui(server="tinymux")
    session = tui.active_view.session
    session.config = session.config.model_copy(
        update={"login": LoginConfig(character="Hamilton", password="secret")}
    )
    provenance = Provenance(sender_dbref=42, adapter="tinymux")
    messages = (
        (EventKind.SAY, 'You say, "one"'),
        (EventKind.SPEECH, "Hamilton poses two"),
        (EventKind.SAY, 'You say, "three"'),
    )
    for sequence, (kind, text) in enumerate(messages, 1):
        tui.handle_event(
            Event(
                session_id=session.session_id,
                world="alpha",
                connection_generation=1,
                sequence=sequence,
                direction=Direction.INBOUND,
                kind=kind,
                canonical_text=text,
                plain_text=text,
                display_text=text,
                parser_name="tinymux",
                provenance=provenance,
            )
        )

    assert tui._combo_notices["alpha"].text == "> Speaking Spree! <"
    assert session.config.login is not None
    assert session.config.login.password.get_secret_value() == "secret"


def test_repeated_empty_speech_does_not_stop_later_output() -> None:
    tui = make_tui()
    session = tui.active_view.session
    for sequence in range(10):
        text = 'Alice says, ""'
        tui.handle_event(
            Event(
                session_id=session.session_id,
                world="alpha",
                connection_generation=1,
                sequence=sequence,
                direction=Direction.INBOUND,
                kind=EventKind.SAY,
                canonical_text=text,
                plain_text=text,
                display_text=text,
                provenance=Provenance(sender_name="Alice"),
            )
        )

    following = 'Bob says, "still visible"'
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=11,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            canonical_text=following,
            plain_text=following,
            display_text=following,
            provenance=Provenance(sender_name="Bob"),
        )
    )

    assert fragment_list_to_text(tui.active_view.output_text()).endswith(following)
    assert not tui._combo_notices


async def test_effects_lab_commands_are_local_and_use_real_renderers() -> None:
    tui = make_tui()
    await add_speaker_effects(tui)
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", "/effects")
    await tui.submit_text("alpha", "/teststreak 6")
    await tui.submit_text("alpha", "/testspeaker Alice")

    assert tui.effects_lab_open is True
    assert queue.empty()
    assert tui._effects_notice is not None
    assert tui._effects_notice.text == "> Unstoppable! <"
    rendered = fragment_list_to_text(tui.effects_display.formatted_text())
    assert "Configured speaker preview: Alice" in rendered
    assert "Alice says" in rendered

    await tui.submit_text("alpha", "this must not be sent")
    assert queue.empty()

    await tui.submit_text("alpha", "/effects close")
    assert tui.effects_lab_open is False


async def test_remote_effects_lab_uses_gateway_projected_speaker_demo() -> None:
    tui = make_tui()
    text = 'Dan says, "Gateway preview."'
    event = Event(
        session_id=tui.active_view.session.session_id,
        world="alpha",
        connection_generation=0,
        sequence=1,
        direction=Direction.INBOUND,
        kind=EventKind.SAY,
        canonical_text=text,
        plain_text=text,
        display_text=text,
        provenance=Provenance(sender_name="Dan"),
    )
    program = color_pulse(
        0,
        3,
        base_color="#005faf",
        accent_color="#ffffff",
        duration_seconds=1,
        repeat_seconds=10,
        repeat_count=1,
    )
    _cursor, projected = event_from_message(
        event_message(
            1,
            event,
            presentations=[
                {
                    "source": "speaker_effects",
                    "presentation": effect_programs_as_dict((program,)),
                }
            ],
            presentation_text=text,
        )
    )
    demo = ProjectedEffectDemo(
        id="speaker:dan",
        label="Dan",
        category="speaker",
        samples=(projected,),
    )
    tui.gateway_reconnect = lambda: None  # type: ignore[assignment]
    tui.gateway_effect_demos = lambda: (demo,)

    await tui.submit_text("alpha", "/effects")
    await tui.submit_text("alpha", "/testspeaker Dan")

    rendered = fragment_list_to_text(tui.effects_display.formatted_text())
    assert "Configured speaker preview: Dan" in rendered
    assert 'Dan says, "Gateway preview."' in rendered
    assert tui.effects_display._entry_presentations[-1][0].program == program


async def test_effects_lab_is_a_local_navigable_world_with_its_own_input() -> None:
    tui = make_tui()
    assert "Effects Lab" not in fragment_list_to_text(tui.world_bar())

    await tui.submit_text("alpha", "/effects")
    tui.effects_input_buffer.text = "lab draft"

    assert tui.effects_lab_open is True
    assert tui.application.layout.current_buffer is tui.effects_input_buffer
    assert "[L] Effects Lab" in fragment_list_to_text(tui.world_bar())
    tui.handle_event(combo_say(tui, 1))
    assert tui.views["alpha"].unread_events == 1

    await tui.submit_text("alpha", "/next")

    assert tui.effects_lab_open is False
    assert tui.active_alias == "alpha"
    assert tui.application.layout.current_buffer is tui.views["alpha"].input_buffer

    tui.switch_relative(-1)

    assert tui.effects_lab_open is True
    assert tui.effects_input_buffer.text == "lab draft"

    await tui.submit_text("alpha", "/effects close")

    assert tui.effects_lab_enabled is False
    assert tui.effects_lab_open is False
    assert "Effects Lab" not in fragment_list_to_text(tui.world_bar())
    tui.switch_relative(-1)
    assert tui.active_alias == "beta"


async def test_effects_lab_demo_commands_require_explicit_activation() -> None:
    tui = make_tui()

    await tui.submit_text("alpha", "/teststreak all")
    await tui.submit_text("alpha", "/testspeaker Alice")

    assert tui.effects_lab_enabled is False
    assert tui.effects_lab_open is False
    assert tui._effects_task is None
    output = fragment_list_to_text(tui.active_view.output_text())
    assert output.count("Effects Lab is closed; use /effects") == 2


def test_general_help_hides_effects_lab_only_commands() -> None:
    tui = make_tui()

    help_text = tui.help_text("appearance")

    assert "/effects — Open the Effects Lab" in help_text
    assert "/teststreak" not in help_text
    assert "/testspeaker" not in help_text
    assert "/effects effect" not in help_text
    assert "/effects force" not in help_text
    assert "/effects close" not in help_text


async def test_effects_lab_rejects_non_lab_commands_with_command_list() -> None:
    tui = make_tui()
    await tui.submit_text("alpha", "/effects")

    await tui.submit_text("alpha", "/connect")
    await tui.submit_text("alpha", "not a command")

    rendered = fragment_list_to_text(tui.effects_display.formatted_text())
    assert "Effects Lab only accepts valid lab commands" in rendered
    assert "/effects effect NAME" in rendered
    assert "/effects effect all" in rendered
    assert "/teststreak all" in rendered
    assert "/teststreak 3..7" in rendered


async def test_effects_lab_effect_all_previews_every_effect_as_wilford_brimley() -> None:
    tui = make_tui()
    tui.effects_display.resize(width=200, height=100)

    await tui.submit_text("alpha", "/effects effect all")

    rendered = fragment_list_to_text(tui.effects_display.formatted_text())
    assert rendered.count('WilfordBrimley says, "') == len(TextEffectKind)
    for effect in TextEffectKind:
        assert f"{effect.value}: The quick brown fox previews this effect." in rendered


def test_speaker_rule_effect_preview_targets_only_the_speaker_name() -> None:
    tui = make_tui()

    tui._show_text_effect_demo("shimmer", speaker="Blood", target_speaker=True)

    rendered = fragment_list_to_text(tui.effects_display.formatted_text())
    decoration = tui.effects_display._entry_decorations[-1][0]
    assert rendered == 'Blood says, "shimmer: The quick brown fox previews this effect."'
    assert (decoration.start, decoration.end) == (0, len("Blood"))


def test_settings_speaker_preview_advances_with_modal_elapsed_time() -> None:
    tui = make_tui()
    rule = {
        "speaker": "Blood",
        "effect": "shimmer",
        "color": "#d70000",
        "accent_color": "#ffffff",
        "period_seconds": 1.4,
        "repeat_seconds": 10,
        "loop": True,
    }

    first = tui._speaker_rule_preview(rule, 0.0)
    later = tui._speaker_rule_preview(rule, 0.7)

    assert fragment_list_to_text(first) == "Blood"
    assert fragment_list_to_text(later) == "Blood"
    assert first != later
    assert all("noreverse" in style and "nobold" in style for style, _text in later)


def test_settings_bold_preview_can_reenable_bold_after_selection_reset() -> None:
    tui = make_tui()
    rule = {
        "speaker": "Noogies",
        "effect": "bold_sweep",
        "color": "#fafafa",
        "duration_seconds": 1.0,
        "repeat_seconds": 10,
        "loop": True,
    }

    frames = [tui._speaker_rule_preview(rule, elapsed) for elapsed in (0.1, 0.5, 0.9)]

    assert all(
        all("noreverse" in style and "nobold" in style for style, _text in frame)
        for frame in frames
    )
    assert any(" bold" in style for frame in frames for style, _text in frame)


def test_generic_effect_preview_continues_to_target_the_message_body() -> None:
    tui = make_tui()

    tui._show_text_effect_demo("shimmer", speaker="Blood")

    rendered = fragment_list_to_text(tui.effects_display.formatted_text())
    decoration = tui.effects_display._entry_decorations[-1][0]
    sample = "shimmer: The quick brown fox previews this effect."
    assert rendered[decoration.start : decoration.end] == sample


async def test_effects_lab_full_streak_playback_is_cancellable() -> None:
    tui = make_tui()
    await tui.submit_text("alpha", "/effects")
    await tui.submit_text("alpha", "/teststreak all")
    assert tui._effects_task is not None

    await tui.submit_text("alpha", "/effects close")
    await asyncio.sleep(0)

    assert tui._effects_task is None
    assert not tui._firework_particles


async def test_effects_lab_force_motion_is_local_and_resets_on_close() -> None:
    tui = make_tui()
    tui._set_low_bandwidth(True)

    await tui.submit_text("alpha", "/effects force on")

    assert tui.effects_lab_force_motion is True
    assert tui._effects_motion_enabled() is True
    assert tui.low_bandwidth is True

    await tui.submit_text("alpha", "/effects close")

    assert tui.effects_lab_force_motion is False
    assert tui.low_bandwidth is True


def test_disabling_motion_cancels_combo_visuals_without_resume() -> None:
    tui = make_tui()
    for sequence in range(1, 5):
        tui.handle_event(combo_say(tui, sequence))
    assert tui.active_view.display._entry_decorations[-1]

    tui._set_low_bandwidth(True)
    tui._set_low_bandwidth(False)

    assert not tui._combo_notices
    assert not tui._firework_particles
    assert not tui.active_view.display._entry_decorations[-1]


def test_godlike_starts_bounded_fireworks_once_and_higher_counts_only_update_notice() -> None:
    tui = make_tui()
    tui._position_firework_particles(80, 24)
    expected = {
        4: TextEffectKind.COMBO_PULSE,
        5: TextEffectKind.COMBO_FLASH_UPPER,
        6: TextEffectKind.COMBO_CYLON,
        7: TextEffectKind.COMBO_GODLIKE,
    }
    for sequence in range(1, 8):
        tui.handle_event(combo_say(tui, sequence))
        if sequence in expected:
            assert any(
                decoration.effect is expected[sequence]
                for decoration in tui.active_view.display._entry_decorations[-1]
            )

    particle_count = len(tui._firework_particles)
    assert particle_count in {12, 24, 36}
    assert tui._combo_notices["alpha"].text == "> GODLIKE! <"
    now = time.monotonic()
    tui._firework_particles = [
        replace(particle, started_at=now - 0.2) for particle in tui._firework_particles
    ]
    occupied = sum(
        character not in {" ", "\n"}
        for character in fragment_list_to_text(tui._firework_layer_text())
    )
    assert occupied > 0

    tui.handle_event(combo_say(tui, 8))

    assert len(tui._firework_particles) == particle_count
    assert tui._combo_notices["alpha"].text == "> GODLIKE x2 <"
    assert not tui.active_view.display._entry_decorations[-1]


async def test_speaking_streak_command_disables_tracking_and_restarts_fresh() -> None:
    tui = make_tui()
    for sequence in range(1, 3):
        tui.handle_event(combo_say(tui, sequence))

    await tui.submit_text("alpha", "/streaks off")
    tui.handle_event(combo_say(tui, 3))

    assert tui.speaking_streaks_enabled is False
    assert not tui._combo_notices
    assert not tui._firework_particles

    await tui.submit_text("alpha", "/streaks on")
    for sequence in range(4, 6):
        tui.handle_event(combo_say(tui, sequence))
    assert not tui._combo_notices
    tui.handle_event(combo_say(tui, 6))
    assert tui._combo_notices["alpha"].text == "> Speaking Spree! <"


async def test_speaking_streak_status_and_usage() -> None:
    tui = make_tui(speaking_streaks_enabled=False)

    await tui.submit_text("alpha", "/streaks status")
    await tui.submit_text("alpha", "/streaks invalid")

    rendered = fragment_list_to_text(tui.active_view.output_text())
    assert "Speaking streak effects are off" in rendered
    assert "Usage: /streaks on|off|status" in rendered


async def test_spellcheck_and_streak_commands_persist_on_gateway() -> None:
    class SharedRuntime:
        def __init__(self) -> None:
            self.revision = 2
            self.values = {"spellcheck": False, "speaking_streaks": True}
            self.sets: list[tuple[str, bool, int]] = []

        async def manage_shared_ui_setting(
            self,
            setting: str,
            operation: str,
            *,
            enabled: bool | None = None,
            expected_revision: int | None = None,
        ) -> dict[str, object]:
            if operation == "set":
                assert enabled is not None and expected_revision == self.revision
                self.sets.append((setting, enabled, expected_revision))
                self.values[setting] = enabled
                self.revision += 1
            return {
                "revision": self.revision,
                "enabled": self.values[setting],
                "changed": operation == "set",
            }

    runtime = SharedRuntime()
    tui = make_tui(service_runtime=runtime)

    await tui._handle_client_command("alpha", "/spellcheck on")
    await tui._handle_client_command("alpha", "/streaks off")

    assert runtime.sets == [
        ("spellcheck", True, 2),
        ("speaking_streaks", False, 3),
    ]
    assert tui.spellcheck_enabled is True
    assert tui.speaking_streaks_enabled is False


def test_firework_density_scales_with_viewport_area() -> None:
    event = combo_say(make_tui(), 7)
    standard = make_tui()
    standard._position_firework_particles(80, 24)
    standard._start_fireworks(event)
    large = make_tui()
    large._position_firework_particles(160, 48)
    large._start_fireworks(event)

    assert len(large._firework_particles) == len(standard._firework_particles) * 4
    assert len(large._firework_particles) <= 1_024


def test_fireworks_discard_particles_outside_viewport_instead_of_clamping_to_edges() -> None:
    tui = make_tui()
    tui._position_firework_particles(80, 24)
    for sequence in range(1, 8):
        tui.handle_event(combo_say(tui, sequence))
    particle = tui._firework_particles[0]
    tui._firework_particles = [
        replace(
            particle,
            started_at=time.monotonic() - 1,
            origin_x=-0.5,
            origin_y=-0.5,
            velocity_x=0,
            velocity_y=0,
        )
    ]

    assert tui._firework_cells("alpha") == {}


async def test_empty_particle_layer_never_masks_output_or_help() -> None:
    tui = make_tui()
    tui.active_view.display.append("visible world output")
    tui._position_firework_particles(80, 24)

    assert "visible world output" in fragment_list_to_text(tui.active_view.output_text())

    await tui._handle_client_command("alpha", "/help")

    assert "TFR help topics" in fragment_list_to_text(tui.active_view.output_text())


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


async def test_portable_presentation_runs_in_tui_and_uses_event_time() -> None:
    class PortablePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_presentation_decorator(
                "speaker",
                lambda _event, _text: (
                    color_pulse(
                        0,
                        5,
                        base_color="#000000",
                        accent_color="#ffffff",
                        duration_seconds=2,
                        repeat_seconds=5,
                        repeat_count=1,
                    ),
                ),
            )

    manager = await PluginManager.load(
        enabled=("portable",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(entry_point("portable", PortablePlugin()),),
        scope="ui",
    )
    tui = make_tui(plugins=manager)
    tui.active_view.display.resize(width=80, height=1)
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
        )
    )
    presentation = tui.active_view.display._entry_presentations[0][0]
    tui._border_frame_elapsed = 1 - presentation.phase_offset_seconds

    assert tui.active_view.output_text()[0][:2] == ("fg:#ffffff", "Alice")
    assert fragment_list_to_text(tui.active_view.display.formatted_text()).startswith("Alice")


async def test_spoofed_speaker_is_permanently_reversed_with_speaker_effect() -> None:
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
        canonical_text='Bob says, "I like grapes!"',
        plain_text='Bob says, "I like grapes!"',
        display_text='Bob says, "I like grapes!"',
        spoof=SpoofAssessment(
            status=SpoofStatus.SPOOFED,
            speaker="Bob",
            speaker_span=(0, 3),
        ),
        metadata={"message_text": 'Bob says, "I like grapes!"'},
    )

    tui.handle_event(event)
    output = tui.active_view.output_text()
    reversed_text = "".join(text for style, text in output if "reverse" in style)

    assert reversed_text == "Bob"
    assert fragment_list_to_text(output) == 'Bob says, "I like grapes!"'


async def test_spoofed_speaker_remains_reversed_after_body_display_transform() -> None:
    class BodyTransform:
        api_version = 1

        def register(self, registrar: Any, _config: object) -> None:
            registrar.register_display_transform(
                "body-transform",
                lambda _event, text: text.replace("thanks", "th4nks"),
            )

    manager = await PluginManager.load(
        enabled=("body-transform",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(entry_point("body-transform", BodyTransform()),),
        scope="ui",
    )
    tui = make_tui(plugins=manager)
    tui.active_view.display.resize(width=80, height=1)
    tui.handle_event(
        Event(
            session_id=tui.active_view.session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            canonical_text='Jarin says, "thanks"',
            plain_text='Jarin says, "thanks"',
            display_text='Jarin says, "thanks"',
            spoof=SpoofAssessment(
                status=SpoofStatus.SPOOFED,
                speaker="Jarin",
                speaker_span=(0, 5),
            ),
            metadata={"message_text": 'Jarin says, "thanks"'},
        )
    )
    output = tui.active_view.output_text()

    assert "".join(text for style, text in output if "reverse" in style) == "Jarin"
    assert fragment_list_to_text(output) == 'Jarin says, "th4nks"'


def test_world_transition_notices_include_local_and_utc_timestamps() -> None:
    tui = make_tui()
    tui.active_view.display.resize(width=200, height=20)
    timestamp = datetime(2026, 9, 28, 18, 34, 56, tzinfo=UTC)
    for sequence, (state, metadata) in enumerate(
        (
            (SessionState.CONNECTED, {}),
            (SessionState.DISCONNECTED, {"error": "remote closed"}),
            (SessionState.RECONNECT_WAIT, {"delay_seconds": 5}),
        )
    ):
        tui.handle_event(
            Event(
                session_id=tui.active_view.session.session_id,
                world="alpha",
                connection_generation=1,
                sequence=sequence,
                timestamp=timestamp,
                direction=Direction.INTERNAL,
                kind=EventKind.CONNECTION,
                metadata={"state": state.value, **metadata},
            )
        )

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Connected · [local " in text
    assert "Disconnected: remote closed · [local " in text
    assert "Reconnecting in 5s · [local " in text
    assert text.count("UTC 2026-09-28 18:34:56 UTC") == 3


def test_remote_tui_renders_gateway_portable_presentations() -> None:
    tui = make_tui()
    tui.gateway_reconnect = lambda: None  # type: ignore[assignment]
    program = color_pulse(0, 5, base_color="#112233", accent_color="#ffffff")
    event = Event(
        session_id=tui.active_view.session.session_id,
        world="alpha",
        connection_generation=1,
        sequence=1,
        direction=Direction.INBOUND,
        kind=EventKind.SAY,
        canonical_text="Alice says hello",
        plain_text="Alice says hello",
        display_text="Alice says hello",
        metadata={
            "gateway_presentations": [
                {
                    "source": "speaker_effects",
                    "presentation": effect_programs_as_dict((program,)),
                }
            ]
        },
    )

    tui.handle_event(event)

    presentations = tui.active_view.display._entry_presentations[-1]
    assert len(presentations) == 1
    assert presentations[0].program.fallback.foreground == "#112233"


def test_remote_tui_rejects_presentations_for_a_different_text_projection() -> None:
    tui = make_tui()
    tui.gateway_reconnect = lambda: None  # type: ignore[assignment]
    program = color_pulse(0, 5, base_color="#112233", accent_color="#ffffff")
    event = Event(
        session_id=tui.active_view.session.session_id,
        world="alpha",
        connection_generation=1,
        sequence=1,
        direction=Direction.INBOUND,
        kind=EventKind.SAY,
        canonical_text="[prefix] Alice says hello",
        plain_text="Alice says hello",
        display_text="[prefix] Alice says hello",
        metadata={
            "gateway_presentation_text": "Alice says hello",
            "gateway_presentations": [
                {
                    "source": "speaker_effects",
                    "presentation": effect_programs_as_dict((program,)),
                }
            ],
        },
    )

    tui.handle_event(event)

    assert tui.active_view.display._entry_presentations[-1] == ()


async def test_remote_tui_uses_only_gateway_portable_presentations() -> None:
    class LocalPresentation:
        api_version = 1

        def register(self, registrar: Any, _config: object) -> None:
            def fail_decoration(_event: Event, _text: str) -> tuple[object, ...]:
                raise AssertionError("remote UIs must not evaluate imperative text decorators")

            def fail_if_called(_event: Event, _text: str) -> tuple[EffectProgram, ...]:
                raise AssertionError("remote UIs must not evaluate portable presentation policy")

            registrar.register_display_decorator("local", fail_decoration)
            registrar.register_presentation_decorator("local", fail_if_called)

    manager = await PluginManager.load(
        enabled=("speaker_effects",),
        config={"speaker_effects": {"rules": []}},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(entry_point("speaker_effects", LocalPresentation()),),
        scope="ui",
    )
    tui = make_tui(plugins=manager)
    tui.gateway_reconnect = lambda: None  # type: ignore[assignment]
    speaker = color_pulse(0, 5, base_color="#112233", accent_color="#ffffff")
    reveal = color_pulse(6, 10, base_color="#778899", accent_color="#ffffff")
    event = Event(
        session_id=tui.active_view.session.session_id,
        world="alpha",
        connection_generation=1,
        sequence=1,
        direction=Direction.INBOUND,
        kind=EventKind.SAY,
        canonical_text="Alice says hello",
        plain_text="Alice says hello",
        display_text="Alice says hello",
        metadata={
            "gateway_presentations": [
                {
                    "source": "speaker_effects",
                    "presentation": effect_programs_as_dict((speaker,)),
                },
                {
                    "source": "terminal_reveal",
                    "presentation": effect_programs_as_dict((reveal,)),
                },
            ]
        },
    )

    tui.handle_event(event)

    presentations = tui.active_view.display._entry_presentations[-1]
    assert [item.program.fallback.foreground for item in presentations] == [
        "#112233",
        "#778899",
    ]


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


async def test_output_border_summarizes_unread_world_activity() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=50, height=2)
    tui.views["beta"].unread_events = 2

    bottom = tui._border_text(view, "output", BorderEdge.BOTTOM, 2)

    assert fragment_list_to_text(bottom) == "└──────────── Activity in world(s): beta +2 ───────┘"
    activity_styles = [style for style, text, *_mouse in bottom if text.isalpha()]
    assert activity_styles and all(style == "class:border.activity" for style in activity_styles)

    await add_animated_border(tui)
    effected = tui._border_text(view, "output", BorderEdge.BOTTOM, 2)
    label = [
        (style, text) for index, (style, text, *_mouse) in enumerate(effected) if 13 <= index < 43
    ]
    assert "Activity in world(s): beta +2" in fragment_list_to_text(label)
    assert all(style == "class:border.activity" for style, _text in label)

    tui.switch_world("beta")
    assert "Activity in world(s)" not in fragment_list_to_text(
        tui._border_text(tui.active_view, "output", BorderEdge.BOTTOM, 2)
    )


def test_output_border_activity_uses_complete_entries_when_narrow() -> None:
    tui = make_tui()
    tui.active_view.display.resize(width=52, height=2)
    tui.views["alpha"].unread_events = 11
    tui.views["beta"].unread_events = 2

    bottom = fragment_list_to_text(
        tui._border_text(tui.active_view, "output", BorderEdge.BOTTOM, 2)
    )

    assert "Activity in world(s): alpha +11, ..." in bottom
    assert "beta" not in bottom

    tui.active_view.display.resize(width=20, height=2)
    hidden = fragment_list_to_text(
        tui._border_text(tui.active_view, "output", BorderEdge.BOTTOM, 2)
    )
    assert hidden == "└────────────────────┘"


def test_output_border_activity_replaces_non_single_cell_alias_characters() -> None:
    tui = make_tui()
    beta = tui.views.pop("beta")
    tui.views["界"] = beta
    tui.aliases[1] = "界"
    beta.unread_events = 2

    bottom = fragment_list_to_text(
        tui._border_text(tui.active_view, "output", BorderEdge.BOTTOM, 2)
    )

    assert "Activity in world(s): ? +2" in bottom
    assert len(bottom) == tui.active_view.display.width + 2


def test_output_border_activity_replaces_combining_alias_characters() -> None:
    tui = make_tui()
    beta = tui.views.pop("beta")
    tui.views["e\u0301"] = beta
    tui.aliases[1] = "e\u0301"
    beta.unread_events = 2

    bottom = fragment_list_to_text(
        tui._border_text(tui.active_view, "output", BorderEdge.BOTTOM, 2)
    )

    assert "Activity in world(s): e? +2" in bottom
    assert len(bottom) == tui.active_view.display.width + 2


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


def test_nospoof_message_preserves_ansi_color_in_tui_output() -> None:
    tui = make_tui(server="tinymush")
    session = tui.active_view.session
    event = Event(
        session_id=session.session_id,
        world=session.world,
        connection_generation=1,
        sequence=0,
        direction=Direction.INBOUND,
        kind=EventKind.SAY,
        canonical_text='[Sender(#1)] Sender says, "\x1b[1;31mhi\x1b[0m"\r\n',
        plain_text='[Sender(#1)] Sender says, "hi"\r\n',
        display_text='Sender says, "\x1b[1;31mhi\x1b[0m"\r\n',
        provenance=Provenance(sender_name="Sender", sender_dbref=1, adapter="tinymush"),
        metadata={"message_text": 'Sender says, "hi"\r\n'},
    )

    tui.handle_event(event)

    fragments = tui.active_view.display.formatted_text()
    assert fragment_list_to_text(fragments) == 'Sender says, "hi"'
    hi_style = next(style for style, text in fragments if text == "hi")
    assert "ansired" in hi_style
    assert "bold" in hi_style


def test_more_count_is_fixed_width_first_in_output_border() -> None:
    tui = make_tui()
    display = tui.active_view.display
    display.resize(width=80, height=3)

    display.append("one\ntwo\nthree\nfour\nfive")

    status = fragment_list_to_text(tui.status_bar())
    bottom = tui._border_text(tui.active_view, "output", BorderEdge.BOTTOM, 3)
    assert "rows 1-3/5" in status
    assert "More" not in status
    assert fragment_list_to_text(bottom).startswith("└── More    2")
    assert all(style == "class:border.more" for style, _text, *_mouse in bottom[3:13])


def test_more_count_caps_at_9999_and_precedes_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=80, height=3)
    tui.views["beta"].unread_events = 4
    monkeypatch.setattr(type(view.display.pager), "more_rows", property(lambda _pager: 20_000))

    bottom = fragment_list_to_text(tui._border_text(view, "output", BorderEdge.BOTTOM, 3))

    assert bottom.startswith("└── More 9999 Activity in world(s): beta +4 ")


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


async def test_spellcheck_highlights_corrected_words_in_world_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: now)
    tui = make_tui(spellcheck=SpellcheckConfig(enabled=True))
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", '"i took teh math test and failed')
    request = queue.get_nowait()

    assert request.text == '"i took the math test and failed'
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.OUTBOUND,
            kind=EventKind.COMMAND,
            canonical_text=request.text,
            plain_text=request.text,
            display_text=request.text,
            actor=request.actor,
            correlation_id=request.request_id,
        )
    )
    assert tui.active_view.recent_commands[-1] == request.text
    recent_corrected = [
        (style, text) for style, text in tui.active_view.recent_input_text() if text == "the"
    ]
    assert len(recent_corrected) == 1
    assert "underline" in recent_corrected[0][0] and "bold" in recent_corrected[0][0]

    echo = 'You say, "i took the math test and failed"\r\n'
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=1,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            canonical_text=echo,
            plain_text=echo,
            display_text=echo,
            metadata={"message_text": echo},
        )
    )

    output = tui.active_view.output_text()
    assert fragment_list_to_text(output).endswith(echo.rstrip())
    corrected = [(style, text) for style, text in output if text == "the"]
    assert len(corrected) == 1
    assert "bold" in corrected[0][0]
    assert "underline" in corrected[0][0]
    assert "fg:" in corrected[0][0]

    now = 101.5
    midpoint = next(style for style, text in tui.active_view.output_text() if text == "the")
    assert midpoint != corrected[0][0]

    now = 103.0
    expired = tui.active_view.output_text()
    assert not any("underline" in style for style, _text in expired)


async def test_spellcheck_highlights_bare_echo_after_gateway_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: now)
    tui = make_tui(
        server="bare",
        spellcheck=SpellcheckConfig(enabled=True),
        character="Hamilton",
    )
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    session.connection_generation = 1
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", '"I liek this.')
    request = queue.get_nowait()
    outbound = Event(
        session_id=session.session_id,
        world="alpha",
        connection_generation=1,
        sequence=0,
        direction=Direction.OUTBOUND,
        kind=EventKind.COMMAND,
        canonical_text=request.text,
        plain_text=request.text,
        display_text=request.text,
        actor=request.actor,
        correlation_id=request.request_id,
    )
    _cursor, received_outbound = event_from_message(event_message(1, outbound))
    tui.handle_event(received_outbound)

    inbound_queue = tui.event_bus.subscribe()
    await session._publish_inbound('Hamilton says, "I like this."\r\n')
    inbound = inbound_queue.get_nowait()
    tui.event_bus.unsubscribe(inbound_queue)
    assert inbound.kind is EventKind.SAY
    assert inbound.parser_name == "bare"
    _cursor, received_inbound = event_from_message(event_message(2, inbound))
    tui.handle_event(received_inbound)

    assert any(
        text == "like" and "bold" in style and "underline" in style
        for style, text in tui.active_view.recent_input_text()
    )
    assert any(
        text == "like" and "bold" in style and "underline" in style
        for style, text in tui.active_view.output_text()
    )


async def test_spellcheck_does_not_consume_bare_raw_line_that_quotes_command() -> None:
    tui = make_tui(server="bare", spellcheck=SpellcheckConfig(enabled=True))
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", '"I liek this.')
    request = queue.get_nowait()
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.OUTBOUND,
            kind=EventKind.COMMAND,
            canonical_text=request.text,
            plain_text=request.text,
            display_text=request.text,
            actor=request.actor,
            correlation_id=request.request_id,
        )
    )

    quoted = f"Someone repeats: {request.text}"
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=1,
            direction=Direction.INBOUND,
            kind=EventKind.RAW_OUTPUT,
            canonical_text=quoted,
            plain_text=quoted,
            display_text=quoted,
            parser_name="bare",
            metadata={"message_text": quoted},
        )
    )

    assert not any("underline" in style for style, _text in tui.active_view.output_text())
    assert len(tui._spellcheck_echoes["alpha"]) == 1


def test_typing_glow_fades_from_configured_highlight_and_is_disabled_by_lowbw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: now)
    tui = make_tui(
        theme=ThemeConfig(preset="catppuccin-mocha"),
        typing_glow=TypingGlowConfig(duration_seconds=0.5),
    )
    view = tui.active_view

    view.input_buffer.insert_text("ab")
    initial = view.typing_glow_processor.apply_transformation(
        SimpleNamespace(fragments=[("", "ab")])  # type: ignore[arg-type]
    ).fragments
    assert all("fg:#ffffff" in style for style, _text, *_handler in initial)

    now = 100.25
    midpoint = view.typing_glow_processor.apply_transformation(
        SimpleNamespace(fragments=[("", "ab")])  # type: ignore[arg-type]
    ).fragments
    assert midpoint[0][0] not in {initial[0][0], ""}

    now = 100.5
    expired = view.typing_glow_processor.apply_transformation(
        SimpleNamespace(fragments=[("", "ab")])  # type: ignore[arg-type]
    ).fragments
    assert all("fg:" not in style for style, _text, *_handler in expired)

    tui._set_low_bandwidth(True)
    view.input_buffer.insert_text("c")
    view.typing_glow_processor.apply_transformation(
        SimpleNamespace(fragments=[("", "abc")])  # type: ignore[arg-type]
    )
    assert view.typing_glow_tracker.timestamps[-1] is None


def test_typing_glow_uses_custom_highlight_color() -> None:
    tui = make_tui(typing_glow=TypingGlowConfig(highlight_color="#ff00ff"))
    view = tui.active_view

    view.input_buffer.insert_text("a")
    fragments = view.typing_glow_processor.apply_transformation(
        SimpleNamespace(fragments=[("", "a")])  # type: ignore[arg-type]
    ).fragments

    assert fragments[0][0] == "fg:#ff00ff bold"


def test_typing_glow_tracks_independent_insertions_and_middle_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: now)
    tui = make_tui(typing_glow=TypingGlowConfig(duration_seconds=1.0))
    view = tui.active_view

    view.input_buffer.insert_text("a")
    now = 100.5
    view.input_buffer.insert_text("b")
    now = 100.75
    staggered = view.typing_glow_processor.apply_transformation(
        SimpleNamespace(fragments=[("", "ab")])  # type: ignore[arg-type]
    ).fragments
    assert staggered[0][0] != staggered[1][0]

    view.input_buffer.cursor_position = 0
    view.input_buffer.insert_text("x")
    edited = view.typing_glow_processor.apply_transformation(
        SimpleNamespace(fragments=[("", "xab")])  # type: ignore[arg-type]
    ).fragments
    assert edited[0][0] != edited[1][0]
    assert edited[1][0] != edited[2][0]

    view.input_buffer.cursor_position = 1
    view.input_buffer.delete_before_cursor(count=1)
    after_delete = view.typing_glow_processor.apply_transformation(
        SimpleNamespace(fragments=[("", "ab")])  # type: ignore[arg-type]
    ).fragments
    assert [style for style, _text, *_handler in after_delete] == [
        staggered[0][0],
        staggered[1][0],
    ]


async def test_spellcheck_output_highlight_expires_without_animation_and_is_not_recalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: now)
    tui = make_tui(spellcheck=SpellcheckConfig(enabled=True))
    tui.animations_enabled = False
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", '"I liek this.')
    request = queue.get_nowait()
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.OUTBOUND,
            kind=EventKind.COMMAND,
            canonical_text=request.text,
            plain_text=request.text,
            display_text=request.text,
            actor=request.actor,
            correlation_id=request.request_id,
        )
    )
    echo = 'You say, "I like this."\r\n'
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=1,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            canonical_text=echo,
            plain_text=echo,
            display_text=echo,
            metadata={"message_text": echo},
        )
    )

    initial = next(style for style, text in tui.active_view.output_text() if text == "like")
    now = 102.5
    held = next(style for style, text in tui.active_view.output_text() if text == "like")
    assert held == initial

    tui.active_view.display.clear_screen()
    await tui._handle_client_command("alpha", "/recall 1")
    assert not any("underline" in style for style, _text in tui.active_view.output_text())

    now = 103.0
    assert not any("underline" in style for style, _text in tui.active_view.output_text())


async def test_spellcheck_output_highlight_expires_in_low_bandwidth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: now)
    tui = make_tui(
        server="bare",
        spellcheck=SpellcheckConfig(enabled=True),
        character="Hamilton",
    )
    tui._set_low_bandwidth(True)
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", '"I liek this.')
    request = queue.get_nowait()
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.OUTBOUND,
            kind=EventKind.COMMAND,
            canonical_text=request.text,
            plain_text=request.text,
            display_text=request.text,
            actor=request.actor,
            correlation_id=request.request_id,
        )
    )
    echo = 'Hamilton says, "I like this."\r\n'
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=1,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            canonical_text=echo,
            plain_text=echo,
            display_text=echo,
            parser_name="bare",
            metadata={"message_text": echo},
        )
    )

    assert any("underline" in style for style, _text in tui.active_view.output_text())
    assert any("underline" in style for style, _text in tui.active_view.recent_input_text())
    now = 103.0
    assert not any("underline" in style for style, _text in tui.active_view.output_text())
    assert not any("underline" in style for style, _text in tui.active_view.recent_input_text())


async def test_spellcheck_leaves_non_speech_commands_unchanged() -> None:
    tui = make_tui(spellcheck=SpellcheckConfig(enabled=True))
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", "@emit liek teh fox")

    assert queue.get_nowait().text == "@emit liek teh fox"


async def test_spellcheck_undo_restores_original_draft() -> None:
    tui = make_tui(spellcheck=SpellcheckConfig(enabled=True))
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", '"I liek this.')
    assert queue.get_nowait().text == '"I like this.'

    await tui.submit_text("alpha", "/spellcheck undo")

    assert tui.active_view.input_buffer.text == '"I liek this.'


async def test_spellcheck_does_not_highlight_unrelated_or_expired_echoes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr("tfr.tui.time.monotonic", lambda: now)
    tui = make_tui(spellcheck=SpellcheckConfig(enabled=True))
    session = tui.active_view.session
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", '"I liek this.')
    request = queue.get_nowait()
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.OUTBOUND,
            kind=EventKind.COMMAND,
            canonical_text=request.text,
            plain_text=request.text,
            display_text=request.text,
            actor=request.actor,
            correlation_id=request.request_id,
        )
    )

    unrelated = 'Alice says, "I like this."\r\n'
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=1,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            canonical_text=unrelated,
            plain_text=unrelated,
            display_text=unrelated,
            metadata={"message_text": unrelated},
        )
    )
    assert not any("underline" in style for style, _text in tui.active_view.output_text())

    longer = 'You say, "I like this. Really."\r\n'
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=2,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            canonical_text=longer,
            plain_text=longer,
            display_text=longer,
            metadata={"message_text": longer},
        )
    )
    assert not any("underline" in style for style, _text in tui.active_view.output_text())

    now = 111.0
    echo = 'You say, "I like this."\r\n'
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=3,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            canonical_text=echo,
            plain_text=echo,
            display_text=echo,
            metadata={"message_text": echo},
        )
    )
    assert not any("underline" in style for style, _text in tui.active_view.output_text())


@pytest.mark.parametrize("possessive", ["", "'s", "’s"])
async def test_spellcheck_highlights_bare_pose_echo_for_configured_character(
    possessive: str,
) -> None:
    tui = make_tui(server="bare", spellcheck=SpellcheckConfig(enabled=True))
    session = tui.active_view.session
    session.config = session.config.model_copy(
        update={"login": LoginConfig(character="Black", password="secret")}
    )
    session.state = SessionState.CONNECTED
    queue = tui.command_bus.register(session.session_id)

    await tui.submit_text("alpha", "pose waevs.")
    request = queue.get_nowait()
    assert request.text == "pose waves."
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.OUTBOUND,
            kind=EventKind.COMMAND,
            canonical_text=request.text,
            plain_text=request.text,
            display_text=request.text,
            actor=request.actor,
            correlation_id=request.request_id,
        )
    )

    echo = f"Black{possessive} waves.\r\n"
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=1,
            direction=Direction.INBOUND,
            kind=EventKind.RAW_OUTPUT,
            canonical_text=echo,
            plain_text=echo,
            display_text=echo,
            parser_name="bare",
            metadata={"message_text": echo},
        )
    )

    corrected = [(style, text) for style, text in tui.active_view.output_text() if text == "waves"]
    assert len(corrected) == 1
    assert "bold" in corrected[0][0] and "underline" in corrected[0][0]


def test_spellcheck_observed_speaker_vocabulary_is_bounded() -> None:
    tui = make_tui()
    observed = tui._observed_speakers["alpha"]
    observed.update(f"speaker-{index}" for index in range(_MAX_OBSERVED_SPEAKERS))
    session = tui.active_view.session

    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=0,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            canonical_text="Newcomer says hello.",
            plain_text="Newcomer says hello.",
            display_text="Newcomer says hello.",
            provenance=Provenance(sender_name="Newcomer"),
        )
    )

    assert len(observed) == _MAX_OBSERVED_SPEAKERS
    assert "Newcomer" not in observed

    observed.clear()
    oversized = "x" * (_MAX_OBSERVED_SPEAKER_LENGTH + 1)
    tui.handle_event(
        Event(
            session_id=session.session_id,
            world="alpha",
            connection_generation=1,
            sequence=1,
            direction=Direction.INBOUND,
            kind=EventKind.SAY,
            canonical_text="hello",
            plain_text="hello",
            display_text="hello",
            provenance=Provenance(sender_name=oversized),
        )
    )
    assert not observed


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
    assert TfrTui._parse_image_parameters([]) == (72, None, False, None)
    assert TfrTui._parse_image_parameters(
        ["--width", "80", "--ascii", "--withcolor", "picture with spaces.png"]
    ) == (80, "ascii", True, Path("picture with spaces.png"))
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
    render_options: list[tuple[str, bool]] = []

    def clipboard_image() -> Image.Image:
        return Image.new("RGB", (4, 4), "white")

    def renderer(
        image: Image.Image,
        *,
        width: int,
        mode: str,
        with_color: bool,
    ) -> SimpleNamespace:
        render_options.append((mode, with_color))
        return SimpleNamespace(lines=("⣿",), width=width, height=1, mode=mode)

    monkeypatch.setattr("tfr.tui.load_clipboard_image", clipboard_image)
    monkeypatch.setattr("tfr.tui.render_image", renderer)

    await tui.open_image_preview("alpha", [])

    assert render_options == [("braille", False)]
    assert tui.image_preview is not None
    assert tui.image_preview.commands == ("@emit ⣿",)
    assert tui.active_view.input_buffer.text == "unfinished draft"

    tui.switch_world("beta")

    assert tui.active_alias == "alpha"

    tui.image_preview = None
    await tui.open_image_preview("alpha", ["--ascii"])

    assert render_options[-1] == ("ascii", False)


async def test_ascii_image_preview_defaults_to_grayscale_and_accepts_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tui = make_tui(server="tinymux")
    render_options: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        "tfr.tui.load_clipboard_image",
        lambda: Image.new("RGB", (4, 4), "white"),
    )

    def renderer(
        image: Image.Image,
        *,
        width: int,
        mode: str,
        with_color: bool,
    ) -> SimpleNamespace:
        render_options.append((mode, with_color))
        return SimpleNamespace(lines=("@@",), width=width, height=1, mode=mode)

    monkeypatch.setattr("tfr.tui.render_image", renderer)

    await tui.open_image_preview("alpha", [])
    assert render_options == [("ascii", False)]
    assert tui.image_preview is not None
    assert "ASCII grayscale" in fragment_list_to_text(tui.image_preview_text())

    tui.image_preview = None
    await tui.open_image_preview("alpha", ["--withcolor"])
    assert render_options[-1] == ("ascii", True)
    assert "ASCII color" in fragment_list_to_text(tui.image_preview_text())


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


async def test_version_command_reports_ui_and_gateway_builds() -> None:
    tui = make_tui(gateway_build=BuildIdentity(version="0.1.54", commit="a" * 40))
    await tui._handle_client_command("alpha", "/version")
    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "UI:" in text
    assert "Gateway: 0.1.54 (" + "a" * 12 + ")" in text


async def test_version_command_without_gateway_reports_not_attached() -> None:
    tui = make_tui(gateway_build=None)
    await tui._handle_client_command("alpha", "/version")
    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "UI:" in text
    assert "Gateway: not separately attached (local, standalone, or replay mode)" in text


async def test_version_command_rejects_arguments() -> None:
    tui = make_tui()
    await tui._handle_client_command("alpha", "/version foo")
    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert "Usage: /version" in text


def test_parse_web_pair_arguments_handles_gateway_forms() -> None:
    from tfr.tui import _parse_web_pair_arguments

    assert _parse_web_pair_arguments(["My", "iPhone"]) == (["My", "iPhone"], None, None)
    assert _parse_web_pair_arguments(["My", "iPhone", "--gateway", "bsd.shutdown.com:27047"]) == (
        ["My", "iPhone"],
        "bsd.shutdown.com:27047",
        None,
    )
    assert _parse_web_pair_arguments(["My", "iPhone", "--gateway=bsd.shutdown.com:27047"]) == (
        ["My", "iPhone"],
        "bsd.shutdown.com:27047",
        None,
    )
    # Bare trailing HOST:PORT (the /web pair "Name" 52.53.4.93:27047 form).
    assert _parse_web_pair_arguments(["My", "iPhone", "52.53.4.93:27047"]) == (
        ["My", "iPhone"],
        "52.53.4.93:27047",
        None,
    )


def test_parse_web_pair_arguments_reports_errors() -> None:
    from tfr.tui import _parse_web_pair_arguments

    label, target, error = _parse_web_pair_arguments(["My", "iPhone", "--gateway"])
    assert target is None and error is not None
    label, target, error = _parse_web_pair_arguments(["My", "iPhone", "--gateway", "0.0.0.0:7347"])
    assert target is None and error is not None and "Invalid --gateway" in error


async def test_web_pair_direct_gateway_reports_tls_setup() -> None:
    class DirectWebRuntime:
        def __init__(self) -> None:
            self.paired: list[str] = []

        async def web_status(self) -> dict[str, object]:
            return {
                "enabled": True,
                "active": False,
                "origin": "https://bsd.shutdown.com:27047",
                "device_count": 0,
                "transport": "direct",
            }

        async def pair_web_device(self, label: str) -> dict[str, object]:
            self.paired.append(label)
            return {
                "pairing_url": "https://bsd.shutdown.com:27047/#pair=test-secret",
                "origin": "https://bsd.shutdown.com:27047",
                "setup": {"backend_started": True, "serve_configured": False},
            }

    runtime = DirectWebRuntime()
    tui = make_tui(service_runtime=runtime)
    tui.active_view.display.resize(width=120, height=100)

    await tui._handle_client_command(
        "alpha", '/web pair "My iPhone" --gateway bsd.shutdown.com:27047'
    )

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert runtime.paired == ["My iPhone"]
    assert "https://bsd.shutdown.com:27047/#pair=test-secret" in text
    assert "Direct TLS access is ready; Tailscale is not required." in text
    assert "Tailscale Serve" not in text


async def test_web_pair_gateway_option_rejects_mismatched_origin() -> None:
    class DirectWebRuntime:
        def __init__(self) -> None:
            self.pair_called = False

        async def web_status(self) -> dict[str, object]:
            return {
                "enabled": True,
                "active": False,
                "origin": "https://bsd.shutdown.com:27047",
                "device_count": 0,
                "transport": "direct",
            }

        async def pair_web_device(self, label: str) -> dict[str, object]:
            self.pair_called = True
            return {"pairing_url": "https://bsd.shutdown.com:27047/#pair=x"}

    runtime = DirectWebRuntime()
    tui = make_tui(service_runtime=runtime)
    tui.active_view.display.resize(width=120, height=100)

    await tui._handle_client_command(
        "alpha", '/web pair "My iPhone" --gateway bsd.shutdown.com:9999'
    )

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert runtime.pair_called is False
    assert "configured direct origin is https://bsd.shutdown.com:27047" in text


async def test_web_pair_gateway_option_requires_direct_transport() -> None:
    class TailscaleWebRuntime:
        def __init__(self) -> None:
            self.pair_called = False

        async def web_status(self) -> dict[str, object]:
            return {
                "enabled": True,
                "active": False,
                "origin": "https://gateway.example.ts.net",
                "device_count": 0,
                "transport": "tailscale",
            }

        async def pair_web_device(self, label: str) -> dict[str, object]:
            self.pair_called = True
            return {"pairing_url": "https://gateway.example.ts.net/#pair=x"}

    runtime = TailscaleWebRuntime()
    tui = make_tui(service_runtime=runtime)
    tui.active_view.display.resize(width=120, height=100)

    await tui._handle_client_command(
        "alpha", '/web pair "My iPhone" --gateway bsd.shutdown.com:27047'
    )

    text = fragment_list_to_text(tui.active_view.display.formatted_text())
    assert runtime.pair_called is False
    assert "requires direct transport" in text
