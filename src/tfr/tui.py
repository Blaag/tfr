from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import os
import secrets
import shlex
import signal
import sys
import time
import webbrowser
from collections import deque
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import replace
from typing import Any, Protocol

from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import BufferControl, DynamicContainer, FormattedTextControl, HSplit
from prompt_toolkit.layout.containers import VSplit, Window
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style

from tfr.agents import AgentRuntime
from tfr.borders import BorderEdge, border_cell
from tfr.clear_effects import ScreenClearContext
from tfr.config import ConfigurationBundle
from tfr.core import CommandBus, EventBus, UnknownSessionError
from tfr.events import Actor, ActorType, CommandRequest, Direction, Event, EventKind
from tfr.pager import DisplayBuffer, FormattedRow, PagerMode, rows_to_formatted_text
from tfr.plugins import PluginLifecycleEvent, PluginManager, PluginWorldInfo
from tfr.sessions import SessionManager, SessionState, WorldSession
from tfr.updates import (
    BuildIdentity,
    UpdateChecker,
    UpdateResult,
    format_update_status,
)


class ServiceRuntime(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def _row_text(row: FormattedRow) -> str:
    return "".join(text for _style, text in row)


def _osc52_sequence(text: str) -> str:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\x1b]52;c;{payload}\x07"


def _format_elapsed(seconds: float) -> str:
    total_seconds = int(max(0.0, seconds))
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def _selected_row(row: FormattedRow, start: int, end: int) -> FormattedRow:
    fragments: list[tuple[str, str]] = []
    offset = 0
    for style, text in row:
        local_start = min(len(text), max(0, start - offset))
        local_end = min(len(text), max(0, end - offset))
        if local_start:
            fragments.append((style, text[:local_start]))
        if local_start < local_end:
            selection_style = f"{style} class:selection".strip()
            fragments.append((selection_style, text[local_start:local_end]))
        if local_end < len(text):
            fragments.append((style, text[local_end:]))
        offset += len(text)
    return tuple(fragments)


class MouseSelectionControl(FormattedTextControl):
    def __init__(
        self,
        text: Callable[[], StyleAndTextTuples],
        mouse_handler: Callable[[MouseEvent], object],
    ) -> None:
        super().__init__(text=text, focusable=False, show_cursor=False)
        self._selection_mouse_handler = mouse_handler

    def mouse_handler(self, mouse_event: MouseEvent) -> object:
        return self._selection_mouse_handler(mouse_event)


class WorldView:
    def __init__(
        self,
        *,
        session: WorldSession,
        display: DisplayBuffer,
        is_agent: bool,
        recent_input_lines: int,
        accept_handler: Any,
        copy_handler: Callable[[str], None],
        invalidate_handler: Callable[[], None],
        animation_state: Callable[[], tuple[float, bool]],
        open_url_handler: Callable[[str], None],
    ) -> None:
        self.session = session
        self.display = display
        self.is_agent = is_agent
        self.unread_events = 0
        self.last_inbound_at: float | None = None
        self.recent_input_lines = recent_input_lines
        self.recent_commands: deque[str] = deque(maxlen=recent_input_lines)
        self._copy_handler = copy_handler
        self._invalidate_handler = invalidate_handler
        self._animation_state = animation_state
        self._open_url_handler = open_url_handler
        self._selection_anchor: tuple[int, int] | None = None
        self._selection_head: tuple[int, int] | None = None
        self._selection_dragged = False
        self._selection_active = False
        self._selection_dragging = False
        self.input_buffer = Buffer(
            accept_handler=accept_handler,
            history=InMemoryHistory(),
            multiline=False,
        )
        self.output_control = MouseSelectionControl(
            text=self.output_text,
            mouse_handler=self.handle_output_mouse,
        )
        self.output_window = Window(
            content=self.output_control,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        self.recent_input_window = Window(
            content=FormattedTextControl(self.recent_input_text),
            height=recent_input_lines,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        self.input_window = VSplit(
            [
                Window(
                    content=FormattedTextControl(
                        lambda: [("class:input.prompt", f"[{self.session.world}]> ")]
                    ),
                    dont_extend_width=True,
                ),
                Window(content=BufferControl(buffer=self.input_buffer), height=1),
            ],
            height=1,
        )

    def recent_input_text(self) -> StyleAndTextTuples:
        commands = [
            command.replace("\r", "").replace("\n", " ") for command in self.recent_commands
        ]
        lines = [""] * (self.recent_input_lines - len(commands)) + commands
        return [("class:input.recent", "\n".join(lines))]

    def output_text(self) -> StyleAndTextTuples:
        elapsed_seconds, animations_enabled = self._animation_state()
        rows = self.display.padded_visible_rows(
            elapsed_seconds=elapsed_seconds,
            animations_enabled=animations_enabled,
        )
        # Selection anchor/head coordinates are content-relative (matching
        # _selection_point()), so the padding at the front of `rows` must be
        # skipped before checking the selection range against it.
        pad_count = len(rows) - len(self.display.visible_rows())
        selection = self.selection_range(rows[pad_count:])
        if selection is None:
            return rows_to_formatted_text(rows)
        (start_row, start_column), (end_row, end_column) = selection
        selected_rows = []
        for row_number, row in enumerate(rows):
            content_row_number = row_number - pad_count
            if start_row <= content_row_number <= end_row:
                row_start = start_column if content_row_number == start_row else 0
                row_end = end_column if content_row_number == end_row else len(_row_text(row))
                row = _selected_row(row, row_start, row_end)
            selected_rows.append(row)
        return rows_to_formatted_text(tuple(selected_rows))

    def selection_range(
        self,
        rows: tuple[FormattedRow, ...],
    ) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if (
            not self._selection_active
            or not self._selection_dragged
            or self._selection_anchor is None
            or self._selection_head is None
            or not rows
        ):
            return None
        start, end = sorted((self._selection_anchor, self._selection_head))
        end_row, end_column = end
        end_column = min(len(_row_text(rows[end_row])), end_column + 1)
        return start, (end_row, end_column)

    def selected_text(self) -> str | None:
        rows = self.display.visible_rows()
        selection = self.selection_range(rows)
        if selection is None:
            return None
        (start_row, start_column), (end_row, end_column) = selection
        selected = []
        for row_number in range(start_row, end_row + 1):
            text = _row_text(rows[row_number])
            row_start = start_column if row_number == start_row else 0
            row_end = end_column if row_number == end_row else len(text)
            selected.append(text[row_start:row_end])
        return "\n".join(selected)

    def handle_output_mouse(self, mouse_event: MouseEvent) -> object:
        if mouse_event.event_type is MouseEventType.MOUSE_DOWN:
            if mouse_event.button is not MouseButton.LEFT:
                return NotImplemented
            point = self._selection_point(mouse_event)
            if point is None:
                return None
            self._selection_anchor = point
            self._selection_head = point
            self._selection_dragged = False
            self._selection_active = True
            self._selection_dragging = True
            self._invalidate_handler()
            return None
        if mouse_event.event_type is MouseEventType.MOUSE_MOVE and self._selection_dragging:
            point = self._selection_point(mouse_event)
            if point is not None:
                self._selection_head = point
                self._selection_dragged = self._selection_dragged or (
                    point != self._selection_anchor
                )
                self._invalidate_handler()
            return None
        if mouse_event.event_type is MouseEventType.MOUSE_UP and self._selection_dragging:
            point = self._selection_point(mouse_event)
            if point is not None:
                self._selection_head = point
                self._selection_dragged = self._selection_dragged or (
                    point != self._selection_anchor
                )
            self._selection_dragging = False
            selected = self.selected_text()
            if selected:
                self._copy_handler(selected)
            elif point is not None and not self._selection_dragged:
                url = self.display.url_at(*point)
                if url is not None:
                    self._open_url_handler(url)
            self._invalidate_handler()
            return None
        return NotImplemented

    def handle_border_mouse(
        self,
        mouse_event: MouseEvent,
        edge: BorderEdge,
        *,
        panel: str,
    ) -> object:
        if not self._selection_dragging or mouse_event.event_type not in {
            MouseEventType.MOUSE_MOVE,
            MouseEventType.MOUSE_UP,
        }:
            return NotImplemented
        rows = self.display.visible_rows()
        if not rows:
            return None
        pane_height = self.display.pager.height
        pad_count = max(0, pane_height - len(rows))
        width = max(1, self.display.width)
        if edge in {BorderEdge.TOP, BorderEdge.BOTTOM}:
            column = min(width - 1, max(0, mouse_event.position.x - 1))
        elif edge is BorderEdge.LEFT:
            column = 0
        else:
            column = width - 1
        # `row` is in the same pane-relative coordinates as a real output
        # mouse event's position.y (i.e. row 0 is the pane's top edge, not
        # necessarily the first buffered row), since handle_output_mouse
        # below feeds it straight into _selection_point.
        if panel == "input" or edge is BorderEdge.BOTTOM:
            row = pane_height - 1
        elif edge is BorderEdge.TOP:
            row = pad_count
        else:
            row = min(pane_height - 1, max(pad_count, mouse_event.position.y))
        return self.handle_output_mouse(
            MouseEvent(
                position=Point(x=column, y=row),
                event_type=mouse_event.event_type,
                button=mouse_event.button,
                modifiers=mouse_event.modifiers,
            )
        )

    def clear_selection(self) -> None:
        self._selection_anchor = None
        self._selection_head = None
        self._selection_dragged = False
        self._selection_active = False
        self._selection_dragging = False

    def _selection_point(self, mouse_event: MouseEvent) -> tuple[int, int] | None:
        rows = self.display.visible_rows()
        if not rows:
            return None
        pad_count = max(0, self.display.pager.height - len(rows))
        y = mouse_event.position.y - pad_count
        if y < 0:
            return None
        row = min(max(0, y), len(rows) - 1)
        column = min(max(0, mouse_event.position.x), len(_row_text(rows[row])))
        return row, column


class TfrTui:
    def __init__(
        self,
        *,
        sessions: Sequence[WorldSession],
        manager: SessionManager,
        event_bus: EventBus,
        command_bus: CommandBus,
        scrollback_lines: dict[str, int],
        agent_worlds: set[str],
        pager_enabled: bool,
        pager_overlap: int,
        recent_input_lines: int = 3,
        plugins: PluginManager | None = None,
        agents: AgentRuntime | None = None,
        service_runtime: ServiceRuntime | None = None,
        gateway_reconnect: Callable[[], Coroutine[Any, Any, str]] | None = None,
        restart_supported: bool = False,
        update_checker: UpdateChecker | None = None,
        gateway_build: BuildIdentity | None = None,
        animations_enabled: bool = True,
        low_bandwidth: bool = False,
        output_color: str | None = None,
        screen_clear_mode: str = "cycle",
        screen_clear_effect: str | None = None,
        boss_screen_mode: str = "cycle",
        boss_screen: str | None = None,
        initial_events: Sequence[Event] = (),
        initial_scroll_to_end: bool = True,
        replay_mode: bool = False,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        if not sessions:
            raise ValueError("at least one world is required for the terminal UI")
        if screen_clear_mode not in {"cycle", "random", "locked"}:
            raise ValueError("screen-clear mode must be cycle, random, or locked")
        if screen_clear_mode == "locked" and screen_clear_effect is None:
            raise ValueError("locked screen-clear mode requires an effect")
        if boss_screen_mode not in {"cycle", "random", "locked"}:
            raise ValueError("boss-screen mode must be cycle, random, or locked")
        if boss_screen_mode == "locked" and boss_screen is None:
            raise ValueError("locked boss-screen mode requires a screen")
        self.manager = manager
        self.event_bus = event_bus
        self.command_bus = command_bus
        self.plugins = plugins or PluginManager(
            event_bus=event_bus,
            command_bus=command_bus,
            targets={session.world: session.session_id for session in sessions},
            worlds={
                session.world: PluginWorldInfo(
                    server=session.config.server,
                    encoding=session.encoding,
                )
                for session in sessions
            },
            scope="ui",
        )
        self.agents = agents
        self.service_runtime = service_runtime
        self.gateway_reconnect = gateway_reconnect
        self.restart_supported = restart_supported
        self.update_checker = update_checker
        self.gateway_build = gateway_build
        self.restart_requested = False
        self.animations_enabled = animations_enabled
        self.low_bandwidth = low_bandwidth
        self._animation_epoch = time.monotonic()
        self._animation_paused_at = self._animation_epoch if low_bandwidth else None
        self._border_frame_elapsed = 0.0
        self._animation_task: asyncio.Task[None] | None = None
        self._animations_started = False
        self._activity_ticker_task: asyncio.Task[None] | None = None
        self._boss_refresh_task: asyncio.Task[None] | None = None
        self._boss_refresh_interval: float | None = None
        self.screen_clear_mode = screen_clear_mode
        self.screen_clear_effect = screen_clear_effect
        self._screen_clear_lines: tuple[str, ...] = ()
        self._screen_clear_styled_lines: tuple[FormattedRow, ...] = ()
        self._screen_clear_width = 0
        self._screen_clear_world: str | None = None
        self._screen_clear_plugin: str | None = None
        self._screen_clear_progress = 0.0
        self._screen_clear_elapsed_seconds = 0.0
        self._screen_clear_task: asyncio.Task[None] | None = None
        self._screen_clear_duration = 0.0
        self._screen_clear_frames_per_second = 0.0
        self._screen_clear_last_plugin: str | None = None
        self._screen_clear_seed = 0
        self.initial_events = tuple(initial_events)
        self.initial_scroll_to_end = initial_scroll_to_end
        self.replay_mode = replay_mode
        self.recent_input_lines = recent_input_lines
        self.aliases = [session.world for session in sessions]
        self.active_index = 0
        self.inspector_agent: str | None = None
        self._event_queue: asyncio.Queue[Event] | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self.views: dict[str, WorldView] = {}

        for session in sessions:
            alias = session.world

            def accept(buffer: Buffer, world: str = alias) -> bool:
                text = buffer.text
                if text:
                    self._spawn(self.submit_text(world, text))
                return False

            self.views[alias] = WorldView(
                session=session,
                display=DisplayBuffer(
                    max_rows=scrollback_lines[alias],
                    pager_enabled=pager_enabled,
                    pager_overlap=pager_overlap,
                    default_style=f"fg:{output_color}" if output_color is not None else "",
                ),
                is_agent=alias in agent_worlds,
                recent_input_lines=recent_input_lines,
                accept_handler=accept,
                copy_handler=self._copy_selection,
                invalidate_handler=lambda: self.application.invalidate(),
                animation_state=lambda: (
                    self._border_frame_elapsed,
                    self.animations_enabled and not self.low_bandwidth,
                ),
                open_url_handler=self._open_url,
            )

        self.output_panels = {
            alias: self._bordered_panel(
                view,
                panel="output",
                content=DynamicContainer(
                    lambda view=view: (
                        self.screen_clear_window
                        if self._screen_clear_world == view.session.world
                        else (
                            self.inspector_window
                            if self.inspector_agent is not None and view is self.active_view
                            else view.output_window
                        )
                    )
                ),
                inner_height=None,
            )
            for alias, view in self.views.items()
        }
        self.input_panels = {
            alias: self._bordered_panel(
                view,
                panel="input",
                content=HSplit(
                    [view.recent_input_window, view.input_window],
                    height=recent_input_lines + 1,
                ),
                inner_height=recent_input_lines + 1,
            )
            for alias, view in self.views.items()
        }
        bindings = self._create_bindings()
        self.inspector_window = Window(
            content=FormattedTextControl(self.agent_inspector_text),
            wrap_lines=True,
            always_hide_cursor=True,
        )
        self.screen_clear_window = Window(
            content=FormattedTextControl(self.screen_clear_text),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        self.normal_root = HSplit(
            [
                Window(content=FormattedTextControl(self.world_bar), height=1),
                DynamicContainer(self._active_output_panel),
                DynamicContainer(lambda: self.input_panels[self.active_alias]),
                Window(content=FormattedTextControl(self.status_bar), height=1),
            ]
        )
        self.boss_control = FormattedTextControl(
            self.boss_text,
            focusable=True,
            key_bindings=self._create_boss_bindings(),
            show_cursor=False,
            modal=True,
        )
        self.boss_window = Window(
            content=self.boss_control,
            wrap_lines=False,
            always_hide_cursor=True,
            style="class:boss",
        )
        root = DynamicContainer(lambda: self.boss_window if self.boss_mode else self.normal_root)
        self.application: Application[int] = Application(
            layout=Layout(root, focused_element=self.active_view.input_buffer),
            key_bindings=bindings,
            full_screen=True,
            mouse_support=True,
            style=Style.from_dict(
                {
                    "world.active": "bold reverse",
                    "world.inactive": "",
                    "world.agent": "fg:#ffaf00",
                    "world.unread": "bold fg:#5fd7ff",
                    "input.prompt": "bold fg:#87afff",
                    "input.recent": "fg:#87afaf",
                    "status": "reverse",
                    "status.more": "bold fg:#ffffff bg:#af0000",
                    "status.lowbw": "bold fg:#000000 bg:#d7af00",
                    "selection": "reverse",
                    "border.output": "fg:#5f87af",
                    "border.input": "fg:#87afff",
                    "boss": "fg:#a8a8a8 bg:#1c1c1c",
                    "boss.chart": "fg:#ffffff bg:#1c1c1c",
                }
            ),
            before_render=self._before_render,
            input=input,
            output=output,
        )
        self.plugins.initialize_boss_selection(boss_screen_mode, boss_screen)
        self.plugins.set_notice_handler(self.add_notice)
        self.plugins.set_boss_state_handler(self._boss_state_changed)

    @property
    def active_alias(self) -> str:
        return self.aliases[self.active_index]

    @property
    def active_view(self) -> WorldView:
        return self.views[self.active_alias]

    @property
    def boss_mode(self) -> bool:
        return self.plugins.boss_active

    def _create_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("c-q")
        @bindings.add("c-c")
        def quit_application(event: Any) -> None:
            event.app.exit(result=130 if event.key_sequence[-1].key == "c-c" else 0)

        @bindings.add("escape", "right")
        @bindings.add("c-right")
        @bindings.add("f6")
        def next_world(_event: Any) -> None:
            self.switch_relative(1)

        @bindings.add("escape", "left")
        @bindings.add("c-left")
        @bindings.add("f5")
        def previous_world(_event: Any) -> None:
            self.switch_relative(-1)

        @bindings.add("pageup")
        def page_up(event: Any) -> None:
            self._end_screen_clear_for(self.active_alias)
            if self.inspector_agent is not None:
                self.inspector_window.vertical_scroll = max(
                    0,
                    self.inspector_window.vertical_scroll
                    - self.active_view.display.pager.page_size,
                )
                event.app.invalidate()
                return
            self.active_view.clear_selection()
            display = self.active_view.display
            display.restore_scrollback()
            display.pager.scroll_rows(-display.pager.page_size)
            self._sync_animation_task(restart=True)
            event.app.invalidate()

        @bindings.add("pagedown")
        def page_down(event: Any) -> None:
            self._end_screen_clear_for(self.active_alias)
            if self.inspector_agent is not None:
                self.inspector_window.vertical_scroll += self.active_view.display.pager.page_size
                event.app.invalidate()
                return
            self.active_view.clear_selection()
            display = self.active_view.display
            if display.pager.mode is PagerMode.PAUSED:
                display.pager.advance()
            else:
                display.pager.scroll_rows(display.pager.page_size)
            self._sync_animation_task(restart=True)
            event.app.invalidate()

        @bindings.add("end")
        def jump_to_end(event: Any) -> None:
            self._end_screen_clear_for(self.active_alias)
            if self.inspector_agent is not None:
                self.inspector_window.vertical_scroll = 0
                event.app.invalidate()
                return
            self.active_view.clear_selection()
            self.active_view.display.pager.jump_to_end()
            self._sync_animation_task(restart=True)
            event.app.invalidate()

        @bindings.add("c-r")
        def reconnect(_event: Any) -> None:
            self._spawn(self.reconnect_world(self.active_alias))

        @bindings.add("c-l")
        def clear_screen(_event: Any) -> None:
            self.inspector_agent = None
            self.start_screen_clear(self.active_alias)

        @bindings.add("f8")
        def toggle_agent_inspector(_event: Any) -> None:
            if self.inspector_agent is not None:
                self.inspector_agent = None
            elif self.agents is not None:
                controller = self.agents.for_world(self.active_alias)
                if controller is not None:
                    self.inspector_agent = controller.name
            self._sync_animation_task(restart=True)
            self.application.invalidate()

        if self.plugins is not None:
            for name, (binding, _handler) in self.plugins.registry.key_bindings.items():

                def invoke_plugin_key(_event: Any, binding_name: str = name) -> None:
                    assert self.plugins is not None
                    self._spawn(self.plugins.invoke_key(binding_name, self.active_alias))

                bindings.add(*binding.keys)(invoke_plugin_key)

        return bindings

    def _create_boss_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter", eager=True)
        def dismiss(_event: Any) -> None:
            self.dismiss_boss()

        @bindings.add(Keys.Any, eager=True)
        def ignore(_event: Any) -> None:
            pass

        return bindings

    def _active_output_window(self) -> Window:
        if self.inspector_agent is not None:
            return self.inspector_window
        return self.active_view.output_window

    def _active_output_panel(self) -> Any:
        return self.output_panels[self.active_alias]

    def screen_clear_text(self) -> StyleAndTextTuples:
        if self.plugins is None or self._screen_clear_plugin is None:
            return []
        name = self._screen_clear_plugin
        frame = self.plugins.render_screen_clear(
            name,
            ScreenClearContext(
                lines=self._screen_clear_lines,
                width=self._screen_clear_width,
                progress=self._screen_clear_progress,
                world=self._screen_clear_world or "",
                seed=self._screen_clear_seed,
                styled_lines=self._screen_clear_styled_lines,
                elapsed_seconds=self._screen_clear_elapsed_seconds,
            ),
        )
        if name not in self.plugins.registry.screen_clear_effects:
            self._stop_screen_clear()
        return frame

    def _screen_clear_plugins(self) -> tuple[str, ...]:
        if self.plugins is None:
            return ()
        return tuple(self.plugins.registry.screen_clear_effects)

    def _select_screen_clear_plugin(self) -> str | None:
        available = self._screen_clear_plugins()
        if not available:
            return None
        if self.screen_clear_mode == "locked":
            selected = (self.screen_clear_effect or "").casefold()
            return next((name for name in available if name.casefold() == selected), None)
        if self.screen_clear_mode == "random":
            return secrets.choice(available)
        if self._screen_clear_last_plugin in available:
            index = available.index(self._screen_clear_last_plugin)
            selected = available[(index + 1) % len(available)]
        else:
            selected = available[0]
        self._screen_clear_last_plugin = selected
        return selected

    def _stop_screen_clear(self) -> None:
        task = self._screen_clear_task
        self._screen_clear_task = None
        if task is not None and not task.done():
            task.cancel()
        self._screen_clear_lines = ()
        self._screen_clear_styled_lines = ()
        self._screen_clear_world = None
        self._screen_clear_plugin = None
        self._screen_clear_elapsed_seconds = 0.0
        self._sync_animation_task(restart=True)
        self.application.invalidate()

    def _end_screen_clear_for(self, alias: str) -> None:
        # Scrolling, paging, or jumping to the end of a world's output
        # while its screen-clear animation is still running would silently
        # change pager state underneath an overlay that's still covering
        # it -- the change wouldn't even be visible until the animation
        # finishes on its own. Ending the animation immediately makes the
        # requested action visible right away instead.
        if self._screen_clear_world == alias:
            self._stop_screen_clear()

    def start_screen_clear(self, alias: str) -> None:
        view = self.views[alias]
        view.clear_selection()
        elapsed_seconds = self._animation_elapsed_seconds()
        # Padded to the pane's true height (rather than however many rows
        # happen to be buffered) so the animation's floor always lands on
        # the pane's actual bottom edge, for example right after /recall
        # truncated the buffer down to just a few lines.
        styled_rows = view.display.padded_visible_rows(
            elapsed_seconds=elapsed_seconds,
            animations_enabled=self.animations_enabled,
        )
        rows = tuple(_row_text(row) for row in styled_rows)
        view.display.clear_screen()
        self._sync_animation_task(restart=True)
        if self._screen_clear_task is not None:
            self._screen_clear_task.cancel()
            self._screen_clear_task = None
        has_content = any(line.strip() for line in rows)
        selected = self._select_screen_clear_plugin() if has_content else None
        effect_unavailable = (
            has_content
            and self.screen_clear_mode == "locked"
            and self.screen_clear_effect is not None
            and selected is None
        )
        if selected is None:
            self._screen_clear_lines = ()
            self._screen_clear_styled_lines = ()
            self._screen_clear_world = None
            self._screen_clear_plugin = None
            if effect_unavailable:
                self.add_notice(
                    alias,
                    f"Screen-clear effect is unavailable: {self.screen_clear_effect}",
                )
            self.application.invalidate()
            return
        self._screen_clear_lines = rows
        self._screen_clear_styled_lines = styled_rows
        self._screen_clear_width = view.display.width
        self._screen_clear_world = alias
        self._screen_clear_plugin = selected
        self._screen_clear_progress = 0.0
        self._screen_clear_elapsed_seconds = 0.0
        self._screen_clear_seed += 1
        assert self.plugins is not None
        effect = self.plugins.registry.screen_clear_effects[selected]
        self._screen_clear_duration = effect.duration_seconds
        self._screen_clear_frames_per_second = effect.frames_per_second
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._screen_clear_lines = ()
            self._screen_clear_styled_lines = ()
            self._screen_clear_world = None
            self._screen_clear_plugin = None
        else:
            task = loop.create_task(self._animate_screen_clear(), name="tfr-screen-clear")
            self._screen_clear_task = task
            self._background_tasks.add(task)
            task.add_done_callback(self._screen_clear_task_done)
        self.application.invalidate()

    async def _animate_screen_clear(self) -> None:
        started = time.monotonic()
        while True:
            if (
                self.plugins is None
                or self._screen_clear_plugin not in self.plugins.registry.screen_clear_effects
            ):
                return
            self._screen_clear_elapsed_seconds = time.monotonic() - started
            self._screen_clear_progress = min(
                1.0,
                self._screen_clear_elapsed_seconds / self._screen_clear_duration,
            )
            self.application.invalidate()
            assert self.plugins is not None
            assert self._screen_clear_plugin is not None
            if self._screen_clear_progress >= 1.0 and self.plugins.screen_clear_is_complete(
                self._screen_clear_plugin,
                world=self._screen_clear_world or "",
            ):
                return
            if self._screen_clear_elapsed_seconds >= 300:
                return
            await asyncio.sleep(1 / self._screen_clear_frames_per_second)

    def _screen_clear_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_task_done(task)
        if self._screen_clear_task is task:
            self._screen_clear_task = None
            self._screen_clear_lines = ()
            self._screen_clear_styled_lines = ()
            self._screen_clear_world = None
            self._screen_clear_plugin = None
            self._screen_clear_elapsed_seconds = 0.0
            self._sync_animation_task(restart=True)
            self.application.invalidate()

    def _bordered_panel(
        self,
        view: WorldView,
        *,
        panel: str,
        content: Any,
        inner_height: int | None,
    ) -> HSplit:
        height = inner_height + 2 if inner_height is not None else None
        return HSplit(
            [
                Window(
                    content=self._border_control(
                        view,
                        panel,
                        BorderEdge.TOP,
                        inner_height,
                    ),
                    height=1,
                ),
                VSplit(
                    [
                        Window(
                            content=self._border_control(
                                view,
                                panel,
                                BorderEdge.LEFT,
                                inner_height,
                            ),
                            width=1,
                            dont_extend_width=True,
                        ),
                        content,
                        Window(
                            content=self._border_control(
                                view,
                                panel,
                                BorderEdge.RIGHT,
                                inner_height,
                            ),
                            width=1,
                            dont_extend_width=True,
                        ),
                    ],
                    height=inner_height,
                ),
                Window(
                    content=self._border_control(
                        view,
                        panel,
                        BorderEdge.BOTTOM,
                        inner_height,
                    ),
                    height=1,
                ),
            ],
            height=height,
        )

    def _border_control(
        self,
        view: WorldView,
        panel: str,
        edge: BorderEdge,
        inner_height: int | None,
    ) -> FormattedTextControl:
        return FormattedTextControl(
            lambda: self._border_text(
                view,
                panel,
                edge,
                inner_height or view.display.pager.height,
            )
        )

    def _border_text(
        self,
        view: WorldView,
        panel: str,
        edge: BorderEdge,
        inner_height: int,
    ) -> StyleAndTextTuples:
        width = max(1, view.display.width + 2)
        length = width if edge in {BorderEdge.TOP, BorderEdge.BOTTOM} else inner_height
        elapsed = self._border_frame_elapsed
        output: StyleAndTextTuples = []

        def mouse_handler(event: MouseEvent) -> object:
            return view.handle_border_mouse(event, edge, panel=panel)

        for index in range(length):
            context, fragment = border_cell(
                panel=panel,
                world=view.session.world,
                edge=edge,
                edge_index=index,
                width=width,
                inner_height=inner_height,
                focused=view.session.world == self.active_alias,
                elapsed_seconds=elapsed,
            )
            if self.animations_enabled and self.plugins is not None:
                fragment = self.plugins.transform_border(context, fragment)
            output.append((fragment.style, fragment.character, mouse_handler))
            if edge in {BorderEdge.LEFT, BorderEdge.RIGHT} and index + 1 < length:
                output.append(("", "\n"))
        return output

    def _before_render(self, app: Application[Any]) -> None:
        self._border_frame_elapsed = self._animation_elapsed_seconds()
        size = app.output.get_size()
        output_height = max(1, size.rows - 7 - self.recent_input_lines)
        output_width = max(1, size.columns - 2)
        layout_changed = False
        for view in self.views.values():
            if view.display.width != output_width or view.display.pager.height != output_height:
                layout_changed = True
            if view.display.width != output_width:
                view.clear_selection()
            view.display.resize(width=output_width, height=output_height)
        if layout_changed:
            self._sync_animation_task(restart=True)

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)

    def _background_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        if task.exception() is not None:
            self.add_notice(self.active_alias, "Client operation failed")

    def switch_relative(self, amount: int) -> None:
        self.switch_world(self.aliases[(self.active_index + amount) % len(self.aliases)])

    def switch_world(self, alias: str) -> None:
        if alias not in self.views:
            self.add_notice(self.active_alias, f"Unknown world: {alias}")
            return
        self.active_index = self.aliases.index(alias)
        self.inspector_agent = None
        self.active_view.unread_events = 0
        self.application.layout.focus(self.active_view.input_buffer)
        self._sync_animation_task(restart=True)
        self.application.invalidate()

    async def activate_boss(self) -> None:
        await self.plugins.activate_selected_boss(self.active_alias)

    def _boss_state_changed(self, active: bool) -> None:
        self._sync_animation_task()
        self._sync_boss_refresh_task()
        self.application.layout.focus(
            self.boss_control if active else self.active_view.input_buffer
        )
        self.application.invalidate()

    def _sync_boss_refresh_task(self) -> None:
        interval = self.plugins.active_boss_refresh_interval
        should_run = self.boss_mode and interval is not None
        restart = should_run and self._boss_refresh_interval != interval
        if restart and self._boss_refresh_task is not None:
            self._boss_refresh_task.cancel()
            self._boss_refresh_task = None
        if should_run and (self._boss_refresh_task is None or self._boss_refresh_task.done()):
            self._boss_refresh_interval = interval
            self._boss_refresh_task = asyncio.create_task(
                self._refresh_boss_view(),
                name="tfr-boss-refresh",
            )
        elif not should_run and self._boss_refresh_task is not None:
            self._boss_refresh_task.cancel()
            self._boss_refresh_task = None
            self._boss_refresh_interval = None

    async def _refresh_boss_view(self) -> None:
        try:
            while self.boss_mode:
                interval = self.plugins.active_boss_refresh_interval
                if interval is None:
                    return
                await asyncio.sleep(interval)
                self.application.invalidate()
        finally:
            if asyncio.current_task() is self._boss_refresh_task:
                self._boss_refresh_task = None
                self._boss_refresh_interval = None

    def dismiss_boss(self) -> None:
        self.plugins.dismiss_boss()

    def boss_text(self) -> StyleAndTextTuples:
        size = self.application.output.get_size()
        return self.plugins.render_boss(width=size.columns, height=size.rows)

    def add_notice(self, alias: str, text: str) -> None:
        self.views[alias].clear_selection()
        self.views[alias].display.append(f"\x1b[33m-- {text} --\x1b[0m", recallable=False)
        self.application.invalidate()

    def _copy_selection(self, text: str) -> None:
        self.application.output.write_raw(_osc52_sequence(text))
        self.application.output.flush()
        if sys.platform == "darwin":
            self._spawn(self._copy_with_pbcopy(text))

    @staticmethod
    async def _copy_with_pbcopy(text: str) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                "pbcopy",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return
        await process.communicate(text.encode("utf-8"))

    def _open_url(self, url: str) -> None:
        self._spawn(self._open_url_in_browser(url))

    @staticmethod
    async def _open_url_in_browser(url: str) -> None:
        await asyncio.to_thread(webbrowser.open_new_tab, url)

    def handle_event(self, event: Event) -> None:
        self.plugins.observe_ui_event(event)
        view = self.views.get(event.world)
        if view is None:
            return
        should_count = False
        if event.direction is Direction.INBOUND:
            view.last_inbound_at = time.monotonic()
            event_text = event.display_text
            if event.provenance is not None and event.provenance.prefix_span is not None:
                if view.session.show_nospoof_prefix:
                    event_text = event.canonical_text
                else:
                    message_text = event.metadata.get("message_text")
                    if isinstance(message_text, str):
                        event_text = message_text
            display_event = (
                event
                if event_text == event.display_text
                else replace(event, display_text=event_text)
            )
            display_text = (
                self.plugins.transform_display(display_event)
                if self.plugins is not None
                else event_text
            )
            if display_text is not None:
                decorations = (
                    self.plugins.decorate_display(display_event, display_text)
                    if self.plugins is not None
                    else ()
                )
                elapsed_seconds = self._animation_elapsed_seconds()
                event_age_seconds = max(0.0, time.time() - event.timestamp.timestamp())
                decorations = tuple(
                    replace(
                        decoration,
                        phase_offset_seconds=(
                            event_age_seconds % decoration.repeat_seconds
                            if decoration.loop
                            else event_age_seconds
                        )
                        - elapsed_seconds,
                    )
                    for decoration in decorations
                )
                view.clear_selection()
                view.display.append(display_text, decorations=decorations)
                should_count = True
        elif (
            event.direction is Direction.OUTBOUND
            and event.kind is EventKind.COMMAND
            and event.actor is not None
            and event.actor.type is ActorType.HUMAN
            and event.display_text is not None
        ):
            view.recent_commands.append(event.display_text)
        elif event.kind is EventKind.PLUGIN and event.display_text is not None:
            view.clear_selection()
            view.display.append(f"\x1b[31m-- {event.display_text} --\x1b[0m")
            should_count = True
        elif event.kind is EventKind.CONNECTION:
            state = event.metadata.get("state")
            if state == SessionState.CONNECTED.value:
                view.clear_selection()
                view.display.append("\x1b[32m-- Connected --\x1b[0m")
                should_count = True
            elif state == SessionState.DISCONNECTED.value:
                view.clear_selection()
                reason = event.metadata.get("error")
                suffix = f": {reason}" if reason else ""
                view.display.append(f"\x1b[31m-- Disconnected{suffix} --\x1b[0m")
                should_count = True
        if should_count and event.world != self.active_alias:
            view.unread_events += 1
        self._sync_animation_task(restart=event.world == self.active_alias)
        self.application.invalidate()

    def world_bar(self) -> StyleAndTextTuples:
        output: StyleAndTextTuples = []
        for alias in self.aliases:
            view = self.views[alias]

            def select_world(mouse_event: MouseEvent, world: str = alias) -> None:
                if (
                    mouse_event.event_type is MouseEventType.MOUSE_UP
                    and mouse_event.button is MouseButton.LEFT
                ):
                    self.switch_world(world)

            classes = [
                "class:world.active" if alias == self.active_alias else "class:world.inactive"
            ]
            if view.is_agent:
                classes.append("class:world.agent")
            if view.unread_events:
                classes.append("class:world.unread")
            marker = "A" if view.is_agent else "H"
            activity = (
                f" ({_format_elapsed(time.monotonic() - view.last_inbound_at)})"
                if view.last_inbound_at is not None
                else ""
            )
            unread = f" +{view.unread_events}" if view.unread_events else ""
            output.append(
                (" ".join(classes), f" [{marker}] {alias}{activity}{unread} ", select_world)
            )
        return output

    def status_bar(self) -> StyleAndTextTuples:
        view = self.active_view
        pager = view.display.pager
        start, end = pager.visible_range
        status: StyleAndTextTuples = [
            (
                "class:status",
                f" {'replay' if self.replay_mode else view.session.state.value}  "
                f"rows {start + 1 if end else 0}-{end}/{pager.total_rows} ",
            )
        ]
        if pager.more_rows:
            status.append(("class:status.more", f" More {pager.more_rows} "))
        if self.low_bandwidth:
            status.append(("class:status.lowbw", " LOWBW "))
        if not self.animations_enabled:
            status.append(("class:status.lowbw", " ANIM OFF "))
        if self.plugins is not None:
            for segment in self.plugins.render_status(self.active_alias):
                status.append(("class:status", f" {segment} "))
        if self.inspector_agent is not None:
            status.append(("class:status.more", f" Agent inspector: {self.inspector_agent} "))
        return status

    def agent_inspector_text(self) -> StyleAndTextTuples:
        if self.agents is None or self.inspector_agent is None:
            return []
        controller = self.agents.controllers.get(self.inspector_agent)
        if controller is None:
            return [("", "Agent not found")]
        inspection = controller.inspection
        lines = [
            f"Agent: {controller.name}",
            f"World: {controller.session.world}",
            f"State: {inspection.state}",
            f"Provider: {inspection.provider or controller.provider.name}",
            f"Endpoint: {inspection.endpoint or controller.provider.endpoint}",
            f"Model: {inspection.model or controller.config.model}",
            f"Request: {inspection.request_id or '-'}",
            "Selected events: "
            + (", ".join(str(value) for value in inspection.selected_event_ids) or "-"),
            f"Validation: {inspection.validation or '-'}",
            f"Command: {inspection.command or '-'}",
            "",
            "System message:",
            inspection.system_message or "-",
            "",
            "User message:",
            inspection.user_message or "-",
            "",
            "Response:",
            inspection.response or "-",
            "",
            "Reasoning summary:",
            inspection.reasoning_summary or "-",
            "",
            "Action:",
            str(dict(inspection.action)) if inspection.action is not None else "-",
        ]
        return [("", "\n".join(lines))]

    async def submit_text(self, alias: str, text: str) -> None:
        if text.startswith("!!"):
            text = text[1:]
        elif text.startswith("!"):
            command = text[1:].lstrip()
            if not command:
                self.add_notice(alias, "Usage: ! command")
            else:
                await self.application.run_system_command(command, wait_for_enter=True)
            return
        if text.startswith("//"):
            text = text[1:]
        elif text.startswith("/"):
            await self._handle_client_command(alias, text)
            return

        view = self.views[alias]
        if view.session.state not in {
            SessionState.CONNECTING,
            SessionState.CONNECTED,
            SessionState.RECONNECT_WAIT,
        }:
            self.add_notice(alias, "Not connected; use /connect")
            return
        request = CommandRequest(
            session_id=view.session.session_id,
            world=alias,
            actor=Actor(ActorType.HUMAN, "operator"),
            text=text,
        )
        try:
            await self.command_bus.submit(request)
        except UnknownSessionError:
            self.add_notice(alias, "Connection is not accepting commands")

    async def _handle_client_command(self, alias: str, text: str) -> None:
        try:
            arguments = shlex.split(text[1:])
        except ValueError as exc:
            self.add_notice(alias, f"Invalid client command: {exc}")
            return
        if not arguments:
            return
        command, *parameters = arguments
        command = command.casefold()
        if command == "help":
            if parameters:
                self.add_notice(alias, "Usage: /help")
            else:
                self.add_notice(alias, self.help_text())
        elif command == "sh":
            if parameters:
                self.add_notice(alias, "Usage: /sh")
            else:
                shell = os.environ.get("SHELL")
                if not shell and os.name == "nt":
                    shell = os.environ.get("COMSPEC")
                await self.application.run_system_command(
                    shlex.quote(shell or "/bin/sh"),
                    wait_for_enter=False,
                )
        elif command in {"reload", "restart"}:
            if parameters:
                self.add_notice(alias, f"Usage: /{command}")
            elif not self.restart_supported:
                self.add_notice(alias, "Restart is available only for a gateway-attached UI")
            else:
                self.restart_requested = True
                get_app().exit(result=0)
        elif command == "gateway":
            if parameters != ["reconnect"]:
                self.add_notice(alias, "Usage: /gateway reconnect")
            elif self.gateway_reconnect is None:
                self.add_notice(alias, "Gateway reconnect is available only in an attached UI")
            else:
                self.add_notice(alias, "Reconnecting to Gateway...")
                try:
                    result = await self.gateway_reconnect()
                except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                    self.add_notice(alias, f"Gateway reconnect failed: {exc}")
                else:
                    self.add_notice(alias, result)
        elif command == "update":
            await self._handle_update_command(alias, parameters)
        elif command in {"quit", "exit"}:
            get_app().exit(result=0)
        elif command == "world":
            if parameters:
                self.switch_world(parameters[0])
            else:
                self.add_notice(alias, "Worlds: " + ", ".join(self.aliases))
        elif command in {"next", "n"}:
            self.switch_relative(1)
        elif command in {"previous", "prev", "p"}:
            self.switch_relative(-1)
        elif command == "connect":
            await self.connect_world(alias)
        elif command == "disconnect":
            await self.views[alias].session.stop()
        elif command == "reconnect":
            await self.reconnect_world(alias)
        elif command == "clear":
            self._handle_clear_command(alias, parameters)
        elif command == "recall":
            self._handle_recall_command(alias, parameters)
        elif command == "end":
            self._end_screen_clear_for(alias)
            self.views[alias].clear_selection()
            self.views[alias].display.pager.jump_to_end()
            self._sync_animation_task(restart=True)
            self.application.invalidate()
        elif command == "agent":
            await self._handle_agent_command(alias, parameters)
        elif command == "nospoof":
            self._handle_nospoof_command(alias, parameters)
        elif command == "lowbw":
            self._handle_low_bandwidth_command(alias, parameters)
        elif command == "animations":
            self._handle_animations_command(alias, parameters)
        elif self.plugins is not None and await self.plugins.execute_command(
            command, tuple(parameters), alias
        ):
            pass
        else:
            self.add_notice(alias, f"Unknown client command: /{command}")

    def help_text(self) -> str:
        lines = [
            "TFR commands",
            "  /help - show this help",
            "  /sh - temporarily open an interactive local shell",
            "  ! command - run one local shell command; !!TEXT sends a literal !",
            "  /world ALIAS - switch worlds; /next (/n) and /previous (/p) also switch",
            "  /connect, /disconnect, /reconnect - manage the active connection",
            "  /clear [status|cycle|random|lock EFFECT] - clear output or select its effect",
            "  /recall X - show the last X retained lines for the active world",
            "  /end - return to live output",
            "  /nospoof show|hide|status - control NOSPOOF prefix visibility",
            "  /lowbw [on|off|status] - suppress continuous UI animation",
            "  /animations [on|off|status] - enable continuous UI effects",
            "  /update status|check - inspect stable UI and Gateway releases",
            "  /agent status|inspect|pause|resume|trigger|close - manage agents",
            "  /quit - exit TFR; //TEXT sends a literal leading slash",
            "",
            "World markers",
            "  [H] human-operated world; [A] agent world",
            "  (Xs/Xm/Xh/Xd) time since that world last received inbound input",
            "",
            "Keybindings",
            "  Enter send; F5/Ctrl-Left/Option-Left previous world",
            "  F6/Ctrl-Right next; left-click selects a world; drag copies output",
            "  Click an underlined http(s) link to open it in your browser",
            "  PageUp/PageDown scroll or page; End returns to live output",
            "  Ctrl-L clear screen; Ctrl-R reconnect; F8 agent inspector",
            "  Ctrl-Q quit; Ctrl-C interrupt",
            "",
            "Loaded plugin commands",
        ]
        if self.restart_supported:
            lines.insert(
                4,
                "  /reload, /restart - reload this UI without disconnecting the gateway",
            )
        if self.gateway_reconnect is not None:
            lines.insert(5, "  /gateway reconnect - reconnect this UI to the gateway")
        if not self.plugins.registry.commands:
            lines.append("  none")
        else:
            for command, (plugin, _handler) in sorted(self.plugins.registry.commands.items()):
                description = self.plugins.registry.command_help.get(command, "plugin command")
                lines.append(f"  /{command} ({plugin}) - {description}")
        return "\n".join(lines)

    async def _handle_update_command(self, alias: str, parameters: list[str]) -> None:
        if parameters not in (["status"], ["check"]):
            self.add_notice(alias, "Usage: /update status|check")
            return
        if self.update_checker is None or not self.update_checker.config.enabled:
            self.add_notice(alias, "Stable update checks are disabled")
            return
        if parameters == ["check"]:
            self.add_notice(alias, "Checking for stable TFR updates...")
            result = await self.update_checker.check()
        else:
            result = self.update_checker.result
        self._show_update_status(result, available_only=False, alias=alias)

    def _show_update_status(
        self,
        result: UpdateResult,
        *,
        available_only: bool,
        alias: str | None = None,
    ) -> None:
        target = alias or self.active_alias
        builds = [("UI", self.update_checker.build)] if self.update_checker is not None else []
        if self.gateway_build is not None:
            builds.append(("Gateway", self.gateway_build))
        for label, build in builds:
            if not available_only or result.available_for(build):
                self.add_notice(target, format_update_status(label, build, result))

    def _handle_nospoof_command(self, alias: str, parameters: list[str]) -> None:
        session = self.views[alias].session
        operation = parameters[0].casefold() if len(parameters) == 1 else ""
        if operation == "show":
            session.show_nospoof_prefix = True
        elif operation == "hide":
            session.show_nospoof_prefix = False
        elif operation not in {"", "status"} or len(parameters) > 1:
            self.add_notice(alias, "Usage: /nospoof show|hide|status")
            return
        visibility = "shown" if session.show_nospoof_prefix else "hidden"
        self.add_notice(alias, f"NOSPOOF prefixes are {visibility}")

    def _handle_clear_command(self, alias: str, parameters: list[str]) -> None:
        available = self._screen_clear_plugins()
        operation = parameters[0].casefold() if parameters else ""
        if not parameters:
            self.start_screen_clear(alias)
            return
        if operation == "status" and len(parameters) == 1:
            selected = (
                f"locked to {self.screen_clear_effect}"
                if self.screen_clear_mode == "locked"
                else self.screen_clear_mode
            )
            self.add_notice(
                alias,
                f"Screen-clear mode is {selected}; effects: {', '.join(available) or 'none'}",
            )
            return
        if operation in {"cycle", "random"} and len(parameters) == 1:
            self.screen_clear_mode = operation
            self.screen_clear_effect = None
            self.add_notice(alias, f"Screen-clear mode is {operation}")
            return
        if operation == "lock" and len(parameters) == 2:
            requested = parameters[1].casefold()
            effect = next((name for name in available if name.casefold() == requested), None)
            if effect is None:
                self.add_notice(alias, f"Unknown screen-clear effect: {parameters[1]}")
                return
            self.screen_clear_mode = "locked"
            self.screen_clear_effect = effect
            self.add_notice(alias, f"Screen-clear effect locked to {effect}")
            return
        self.add_notice(alias, "Usage: /clear [status|cycle|random|lock EFFECT]")

    def _handle_recall_command(self, alias: str, parameters: list[str]) -> None:
        if len(parameters) != 1:
            self.add_notice(alias, "Usage: /recall X")
            return
        try:
            count = int(parameters[0])
        except ValueError:
            self.add_notice(alias, "Usage: /recall X")
            return
        if count <= 0:
            self.add_notice(alias, "Recall count must be a positive integer")
            return
        view = self.views[alias]
        recalled = view.display.recent_entries(count)
        view.clear_selection()
        view.display.append(f"\x1b[33m-- Recall {count}\x1b[0m", recallable=False)
        elapsed_seconds = self._animation_elapsed_seconds()
        for text, decorations in recalled:
            replayed = tuple(
                replace(decoration, phase_offset_seconds=-elapsed_seconds)
                for decoration in decorations
            )
            view.display.append(text, decorations=replayed, recallable=False)
        view.display.pager.jump_to_end()
        self._sync_animation_task(restart=True)
        self.application.invalidate()

    def _handle_low_bandwidth_command(self, alias: str, parameters: list[str]) -> None:
        operation = parameters[0].casefold() if len(parameters) == 1 else ""
        if not parameters:
            self._set_low_bandwidth(not self.low_bandwidth)
        elif operation == "on":
            self._set_low_bandwidth(True)
        elif operation == "off":
            self._set_low_bandwidth(False)
        elif operation != "status" or len(parameters) > 1:
            self.add_notice(alias, "Usage: /lowbw [on|off|status]")
            return
        self._sync_animation_task(restart=True)
        state = "on" if self.low_bandwidth else "off"
        self.add_notice(alias, f"Low-bandwidth mode is {state}")

    def _set_low_bandwidth(self, enabled: bool) -> None:
        if enabled == self.low_bandwidth:
            return
        now = time.monotonic()
        if enabled:
            self._animation_paused_at = now
        else:
            assert self._animation_paused_at is not None
            self._animation_epoch += now - self._animation_paused_at
            self._animation_paused_at = None
        self.low_bandwidth = enabled

    def _handle_animations_command(self, alias: str, parameters: list[str]) -> None:
        operation = parameters[0].casefold() if len(parameters) == 1 else ""
        if not parameters:
            self.animations_enabled = not self.animations_enabled
        elif operation == "on":
            self.animations_enabled = True
        elif operation == "off":
            self.animations_enabled = False
        elif operation != "status" or len(parameters) > 1:
            self.add_notice(alias, "Usage: /animations [on|off|status]")
            return
        self._sync_animation_task()
        state = "on" if self.animations_enabled else "off"
        self.add_notice(alias, f"Continuous UI animations are {state}")

    def _animation_elapsed_seconds(self) -> float:
        now = self._animation_paused_at
        if now is None:
            now = time.monotonic()
        return now - self._animation_epoch

    def _sync_animation_task(self, *, restart: bool = False) -> None:
        has_border_effects = (
            self.plugins.has_animated_border_effects if self.plugins is not None else False
        )
        has_text_effects = self._text_frame_delay(self._animation_elapsed_seconds()) is not None
        should_run = (
            self._animations_started
            and self.animations_enabled
            and not self.low_bandwidth
            and not self.boss_mode
        )
        should_run = should_run and (has_border_effects or has_text_effects)
        if (
            should_run
            and restart
            and self._animation_task is not None
            and not self._animation_task.done()
            and self._animation_task is not asyncio.current_task()
        ):
            self._animation_task.cancel()
            self._animation_task = None
        if should_run and (self._animation_task is None or self._animation_task.done()):
            self._animation_task = asyncio.create_task(
                self._animate_ui(),
                name="tfr-ui-animation",
            )
        elif not should_run and self._animation_task is not None:
            self._animation_task.cancel()
            self._animation_task = None
        self._sync_activity_ticker()

    def _sync_activity_ticker(self) -> None:
        # A separate, much slower ticker so the world bar's per-world
        # "last activity" indicator keeps counting up even when no other
        # animation or event is causing a redraw. Deliberately independent
        # of animations_enabled (it's informational text, not a visual
        # effect), but still respects low_bandwidth/boss_mode like the
        # rest of the continuous-redraw machinery.
        should_run = self._animations_started and not self.low_bandwidth and not self.boss_mode
        if should_run and (self._activity_ticker_task is None or self._activity_ticker_task.done()):
            task = asyncio.create_task(self._animate_last_activity(), name="tfr-activity-ticker")
            self._activity_ticker_task = task
            self._background_tasks.add(task)
            task.add_done_callback(self._background_task_done)
        elif not should_run and self._activity_ticker_task is not None:
            self._activity_ticker_task.cancel()
            self._activity_ticker_task = None

    async def _animate_last_activity(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.application.invalidate()

    def _text_frame_delay(self, elapsed_seconds: float) -> float | None:
        if self.inspector_agent is not None or self._screen_clear_world == self.active_alias:
            return None
        return self.active_view.display.animation_frame_delay(elapsed_seconds)

    async def _animate_ui(self) -> None:
        while True:
            elapsed_seconds = self._animation_elapsed_seconds()
            border_delay = (
                self.plugins.border_frame_delay(elapsed_seconds)
                if self.plugins is not None
                else None
            )
            text_delay = self._text_frame_delay(elapsed_seconds)
            delays = [delay for delay in (border_delay, text_delay) if delay is not None]
            delay = min(delays, default=None)
            if delay is None or not self.animations_enabled or self.low_bandwidth or self.boss_mode:
                return
            await asyncio.sleep(max(1 / 30, delay))
            self.application.invalidate()

    async def _handle_agent_command(self, alias: str, parameters: list[str]) -> None:
        if self.agents is None:
            self.add_notice(alias, "No agents are configured")
            return
        operation = parameters[0].casefold() if parameters else "status"
        default = self.agents.for_world(alias)
        name = parameters[1] if len(parameters) > 1 else (default.name if default else None)
        if operation == "close":
            self.inspector_agent = None
        elif operation == "status":
            names = ", ".join(
                f"{controller.name}:{controller.inspection.state}"
                for controller in self.agents.controllers.values()
            )
            self.add_notice(alias, f"Agents: {names or 'none'}")
        elif name is None or name not in self.agents.controllers:
            self.add_notice(alias, "Specify a configured agent name")
        elif operation == "inspect":
            self.inspector_agent = name
        elif operation == "pause":
            await self.agents.pause(name)
        elif operation == "resume":
            result = self.agents.resume(name)
            if inspect.isawaitable(result):
                await result
        elif operation == "trigger":
            result = self.agents.trigger(name)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                self.add_notice(alias, f"Agent {name} cannot be triggered")
        else:
            self.add_notice(alias, f"Unknown agent operation: {operation}")
        self._sync_animation_task()
        self.application.invalidate()

    async def connect_world(self, alias: str) -> None:
        session = self.views[alias].session
        if session.state is not SessionState.STOPPED:
            self.add_notice(alias, f"Already {session.state.value}")
            return
        await session.start()

    async def reconnect_world(self, alias: str) -> None:
        session = self.views[alias].session
        await session.stop()
        await session.start()

    async def _pump_events(self) -> None:
        assert self._event_queue is not None
        while True:
            self.handle_event(await self._event_queue.get())

    async def run(self) -> int:
        self._event_queue = self.event_bus.subscribe()
        pump = asyncio.create_task(self._pump_events(), name="tfr-ui-events")
        loop = asyncio.get_running_loop()
        installed_signals: list[signal.Signals] = []
        try:
            self._before_render(self.application)
            for event in self.initial_events:
                self.handle_event(event)
            if self.initial_scroll_to_end:
                for view in self.views.values():
                    view.display.pager.jump_to_end()
            if self.service_runtime is not None:
                await self.service_runtime.start()
            else:
                if self.plugins is not None:
                    await self.plugins.lifecycle(PluginLifecycleEvent(kind="application_start"))
                if self.agents is not None:
                    self.agents.start()
                await self.manager.start_autoconnect()
            if self.update_checker is not None and self.update_checker.config.enabled:
                self._spawn(
                    self.update_checker.run_periodically(
                        lambda result: self._show_update_status(result, available_only=True)
                    )
                )
            self._animations_started = True
            self._sync_animation_task()
            handled_signals = [signal.SIGTERM]
            if hasattr(signal, "SIGHUP"):
                handled_signals.append(signal.SIGHUP)
            for handled_signal in handled_signals:
                try:
                    loop.add_signal_handler(
                        handled_signal,
                        lambda code=128 + handled_signal.value: (
                            self.application.exit(result=code)
                            if self.application.is_running
                            else None
                        ),
                    )
                    installed_signals.append(handled_signal)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
            return await self.application.run_async()
        finally:
            self._animations_started = False
            animation_task = self._animation_task
            self._animation_task = None
            if animation_task is not None:
                animation_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await animation_task
            boss_refresh_task = self._boss_refresh_task
            self._boss_refresh_task = None
            self._boss_refresh_interval = None
            if boss_refresh_task is not None:
                boss_refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await boss_refresh_task
            for handled_signal in installed_signals:
                loop.remove_signal_handler(handled_signal)
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            self.event_bus.unsubscribe(self._event_queue)
            if self.service_runtime is not None:
                await self.service_runtime.stop()
            else:
                if self.agents is not None:
                    await self.agents.stop()
                await self.manager.stop_all()
                if self.plugins is not None:
                    await self.plugins.drain()
                    await self.plugins.lifecycle(PluginLifecycleEvent(kind="application_stop"))
                    await self.plugins.drain()
            for task in self._background_tasks:
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)


async def run_client(bundle: ConfigurationBundle) -> int:
    from tfr.gateway import GatewayRuntime, scrollback_for

    runtime = await GatewayRuntime.from_configuration(bundle, plugin_scope="all")
    try:
        tui = TfrTui(
            sessions=runtime.sessions,
            manager=runtime.manager,
            event_bus=runtime.event_bus,
            command_bus=runtime.command_bus,
            scrollback_lines={
                alias: scrollback_for(bundle, alias) for alias in bundle.worlds.worlds
            },
            agent_worlds={agent.world for agent in bundle.agents.agents.values()},
            pager_enabled=bundle.main.ui.pager.enabled,
            pager_overlap=bundle.main.ui.pager.overlap_lines,
            recent_input_lines=bundle.main.ui.recent_input_lines,
            animations_enabled=bundle.main.ui.animations_enabled,
            low_bandwidth=bundle.main.ui.low_bandwidth,
            output_color=bundle.main.ui.output_color,
            screen_clear_mode=bundle.main.ui.screen_clear.mode,
            screen_clear_effect=bundle.main.ui.screen_clear.effect,
            boss_screen_mode=bundle.main.ui.boss.mode,
            boss_screen=bundle.main.ui.boss.screen,
            plugins=runtime.plugins,
            agents=runtime.agents,
            service_runtime=runtime,
            update_checker=UpdateChecker(bundle.main.updates),
        )
    except BaseException:
        await runtime.stop()
        raise
    return await tui.run()
