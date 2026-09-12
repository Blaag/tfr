from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import DummyInput, Input, create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from tfr.agents import AgentInspection
from tfr.borders import BorderEdge
from tfr.config import WorldConfig, WorldDefaults
from tfr.core import CommandBus, EventBus
from tfr.events import Actor, ActorType, Direction, Event, EventKind, Provenance
from tfr.pager import PagerMode
from tfr.plugin_api import BorderFragment, ScreenClearContext, TextDecoration, TextEffectKind
from tfr.plugins import PluginManager
from tfr.sessions import SessionManager, SessionState, WorldSession
from tfr.tui import TfrTui, _osc52_sequence, run_client


def make_tui(*, input: Input | None = None) -> TfrTui:
    event_bus = EventBus()
    command_bus = CommandBus()
    sessions = [
        WorldSession(
            world=alias,
            config=WorldConfig(host="localhost", port=4201, autoconnect=False),
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
        input=input or DummyInput(),
        output=DummyOutput(),
    )


def entry_point(name: str, plugin: object) -> SimpleNamespace:
    return SimpleNamespace(name=name, load=lambda: plugin)


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

        def register(self, registrar: object, _config: object) -> None:
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


def test_dragging_output_selects_highlights_and_copies_plain_text() -> None:
    tui = make_tui()
    view = tui.active_view
    view.display.resize(width=20, height=5)
    view.display.append("\x1b[31mfirst\x1b[0m\nsecond")
    copied: list[str] = []
    view._copy_handler = copied.append

    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=1, y=0),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )
    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=2, y=1),
            event_type=MouseEventType.MOUSE_MOVE,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )
    view.handle_output_mouse(
        MouseEvent(
            position=Point(x=2, y=1),
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
    assert "/boss - hide TFR" in help_text
    assert "/sh - temporarily open" in help_text
    assert "! command" in help_text
    assert "/nospoof show|hide|status" in help_text
    assert "/recall X" in help_text
    assert "[H] human-operated world" in help_text
    assert "left-click selects a world" in help_text
    assert "/fixture (fixture) - run the harmless fixture" in help_text


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

    assert captured[0].lines == ("red plain",)
    assert "ansired" in captured[0].styled_lines[0][0][0]
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

    class FakeTui:
        def __init__(self, **_values: object) -> None:
            pass

        async def run(self) -> int:
            return 17

    monkeypatch.setattr(GatewayRuntime, "from_configuration", classmethod(from_configuration))
    monkeypatch.setattr("tfr.tui.TfrTui", FakeTui)
    screen_clear = SimpleNamespace(mode="cycle", effect=None)
    ui = SimpleNamespace(
        pager=SimpleNamespace(enabled=True, overlap_lines=1),
        recent_input_lines=3,
        animations_enabled=True,
        low_bandwidth=False,
        output_color="#d7d7d7",
        screen_clear=screen_clear,
    )
    bundle = SimpleNamespace(
        main=SimpleNamespace(ui=ui),
        worlds=SimpleNamespace(worlds={}),
        agents=SimpleNamespace(agents={}),
    )

    assert await run_client(bundle) == 17  # type: ignore[arg-type]
    assert received_scope == "all"


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


async def test_boss_mode_hides_buffered_world_text_until_enter() -> None:
    tui = make_tui()
    tui.active_view.input_buffer.text = "unfinished draft"

    await tui._handle_client_command("alpha", "/boss")

    assert tui.boss_mode is True
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

    enter = next(
        binding
        for binding in tui.boss_control.key_bindings.bindings
        if Keys.ControlM in binding.keys
    )
    enter.handler(SimpleNamespace())

    assert tui.boss_mode is False
    assert tui.application.layout.current_buffer is tui.active_view.input_buffer
    assert tui.active_view.input_buffer.text == "unfinished draft"


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
