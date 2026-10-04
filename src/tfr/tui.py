from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
import webbrowser
from collections import deque
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from PIL import Image
from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import BufferControl, DynamicContainer, FormattedTextControl, HSplit
from prompt_toolkit.layout.containers import Float, FloatContainer, VSplit, Window
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from tfr.agents import AgentRuntime
from tfr.ansi import ansi_visible_text, safe_ansi_formatted_text, terminal_plain_text
from tfr.borders import BorderEdge, border_cell
from tfr.clear_effects import ScreenClearContext
from tfr.combo import Combo, ComboTracker
from tfr.config import ConfigurationBundle, SpellcheckConfig, ThemeConfig, TypingGlowConfig
from tfr.core import CommandBus, EventBus, UnknownSessionError
from tfr.events import (
    Actor,
    ActorType,
    CommandRequest,
    Confidence,
    Direction,
    Event,
    EventKind,
    Provenance,
    SpoofStatus,
)
from tfr.image_art import (
    DEFAULT_IMAGE_WIDTH,
    MAXIMUM_IMAGE_WIDTH,
    ImageGlyphMode,
    RenderedImage,
    load_clipboard_image,
    load_image_file,
    render_image,
)
from tfr.managed_updates import activate_managed_update, stage_managed_update
from tfr.pager import (
    DisplayBuffer,
    FormattedRow,
    PagerMode,
    StaticStyleSpan,
    TransientStyleSpan,
    rows_to_formatted_text,
)
from tfr.plugin_sources import (
    PluginUpdateChecker,
    PluginUpdateResult,
    format_plugin_update_status,
)
from tfr.plugins import PluginLifecycleEvent, PluginManager, PluginWorldInfo
from tfr.presentation import ActiveEffectProgram
from tfr.sessions import SessionManager, SessionState, WorldSession
from tfr.spellcheck import Correction, LocalSpellChecker, speech_payload
from tfr.text_effects import TextDecoration, TextEffectKind, derive_bright_color, interpolate_color
from tfr.themes import ResolvedTheme, resolve_theme
from tfr.updates import (
    BuildIdentity,
    UpdateChecker,
    UpdateResult,
    format_update_status,
)
from tfr.world_text import escape_world_text

_MULTILINE_PASTE_DELAY_SECONDS = 0.5
_MULTILINE_PASTE_MAXIMUM_BYTES = 1_048_576
_MULTILINE_PASTE_MAXIMUM_LINES = 10_000
_MULTILINE_PASTE_MAXIMUM_COMMAND_BYTES = 7_000
_CORE_CLIENT_COMMANDS = frozenset(
    {
        "agent",
        "animations",
        "clear",
        "connect",
        "disconnect",
        "end",
        "exit",
        "gateway",
        "help",
        "image",
        "lowbw",
        "mouse",
        "n",
        "next",
        "nospoof",
        "p",
        "plugins",
        "prev",
        "previous",
        "quit",
        "recall",
        "reconnect",
        "reload",
        "restart",
        "sh",
        "spellcheck",
        "update",
        "world",
    }
)
_MAX_OBSERVED_SPEAKERS = 1_000
_MAX_OBSERVED_SPEAKER_LENGTH = 64
_MAX_PENDING_SPELLCHECK_ECHOES = 32
_SPELLCHECK_ECHO_TIMEOUT_SECONDS = 10.0


class ServiceRuntime(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def request_update(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class ImagePreview:
    alias: str
    image: Image.Image
    rendered: RenderedImage
    commands: tuple[str, ...]
    unicode_allowed: bool
    requested_width: int
    requested_mode: ImageGlyphMode
    requested_with_color: bool
    connection_generation: int
    server: str
    encoding: str
    rendering: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PendingSpellcheckEcho:
    text: str
    corrections: tuple[Correction, ...]
    connection_generation: int
    queued_at: float


class RecentCommand(str):
    transient_style_spans: tuple[TransientStyleSpan, ...]

    def __new__(
        cls,
        text: str,
        transient_style_spans: tuple[TransientStyleSpan, ...] = (),
    ) -> RecentCommand:
        value = super().__new__(cls, text)
        value.transient_style_spans = transient_style_spans
        return value


@dataclass(frozen=True, slots=True)
class ComboNotice:
    text: str
    color: str
    started_at: float


@dataclass(frozen=True, slots=True)
class FireworkParticle:
    world: str
    started_at: float
    origin_x: float
    origin_y: float
    velocity_x: float
    velocity_y: float
    color: str
    glyph: str


class TypingGlowTracker:
    def __init__(self) -> None:
        self.text = ""
        self.timestamps: list[float | None] = []

    def sync(self, text: str) -> None:
        if text == self.text:
            return
        prefix = 0
        maximum_prefix = min(len(self.text), len(text))
        while prefix < maximum_prefix and self.text[prefix] == text[prefix]:
            prefix += 1
        suffix = 0
        maximum_suffix = min(len(self.text) - prefix, len(text) - prefix)
        while suffix < maximum_suffix and self.text[-1 - suffix] == text[-1 - suffix]:
            suffix += 1
        tail = self.timestamps[len(self.timestamps) - suffix :] if suffix else []
        self.timestamps = [
            *self.timestamps[:prefix],
            *([None] * (len(text) - prefix - suffix)),
            *tail,
        ]
        self.text = text

    def inserted(self, text: str, start: int, length: int, now: float) -> None:
        self.sync(text)
        end = min(len(self.timestamps), start + length)
        for index in range(max(0, start), end):
            self.timestamps[index] = now

    def frame_delay(self, now: float, duration_seconds: float) -> float | None:
        remaining = [
            timestamp + duration_seconds - now
            for timestamp in self.timestamps
            if timestamp is not None and timestamp + duration_seconds > now
        ]
        return min(1 / 30, min(remaining)) if remaining else None


class TypingGlowBuffer(Buffer):
    def __init__(
        self,
        *,
        tracker: TypingGlowTracker,
        enabled: Callable[[], bool],
        inserted_handler: Callable[[], None],
        **kwargs: Any,
    ) -> None:
        self._typing_glow_tracker = tracker
        self._typing_glow_enabled = enabled
        self._typing_glow_inserted_handler = inserted_handler
        super().__init__(**kwargs)

    def insert_text(
        self,
        data: str,
        overwrite: bool = False,
        move_cursor: bool = True,
        fire_event: bool = True,
    ) -> None:
        start = self.cursor_position
        super().insert_text(
            data,
            overwrite=overwrite,
            move_cursor=move_cursor,
            fire_event=fire_event,
        )
        if fire_event and data and self._typing_glow_enabled():
            self._typing_glow_tracker.inserted(self.text, start, len(data), time.monotonic())
            self._typing_glow_inserted_handler()


class TypingGlowProcessor(Processor):
    def __init__(
        self,
        *,
        tracker: TypingGlowTracker,
        enabled: Callable[[], bool],
        duration_seconds: float,
        start_color: str,
        end_color: str,
        bold: bool,
    ) -> None:
        self.tracker = tracker
        self.enabled = enabled
        self.duration_seconds = duration_seconds
        self.start_color = start_color
        self.end_color = end_color
        self.bold = bold

    def apply_transformation(self, transformation_input: TransformationInput) -> Transformation:
        fragments = transformation_input.fragments
        text = "".join(fragment[1] for fragment in fragments)
        self.tracker.sync(text)
        if not self.enabled():
            return Transformation(fragments)
        now = time.monotonic()
        output: StyleAndTextTuples = []
        offset = 0
        for fragment in fragments:
            style, value, *handler = fragment
            for character in value:
                timestamp = (
                    self.tracker.timestamps[offset]
                    if offset < len(self.tracker.timestamps)
                    else None
                )
                rendered_style = style
                if timestamp is not None:
                    elapsed = max(0.0, now - timestamp)
                    if elapsed < self.duration_seconds:
                        progress = elapsed / self.duration_seconds
                        color = interpolate_color(self.start_color, self.end_color, progress)
                        emphasis = " bold" if self.bold else ""
                        rendered_style = f"{style} fg:{color}{emphasis}".strip()
                output.append((rendered_style, character, *handler))
                offset += 1
        return Transformation(output)


def _row_text(row: FormattedRow) -> str:
    return "".join(text for _style, text in row)


def _transient_plain_text(
    text: str,
    *,
    base_style: str,
    spans: tuple[TransientStyleSpan, ...],
    now: float,
    animated: bool,
) -> StyleAndTextTuples:
    output: StyleAndTextTuples = []
    for index, character in enumerate(text):
        style = base_style
        for span in spans:
            if span.start <= index < span.end:
                effect = span.style_at(now, animated=animated)
                if effect:
                    style = f"{style} {effect}".strip()
        if output and len(output[-1]) == 2 and output[-1][0] == style:
            previous_style, previous_text = output[-1]
            output[-1] = (previous_style, previous_text + character)
        else:
            output.append((style, character))
    return output


def _osc52_sequence(text: str) -> str:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\x1b]52;c;{payload}\x07"


def _process_parent(pid: int) -> tuple[int, str] | None:
    proc = Path("/proc") / str(pid)
    try:
        name = (proc / "comm").read_text(encoding="utf-8").strip()
        status = (proc / "status").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        try:
            result = subprocess.run(
                ("ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)),
                check=True,
                capture_output=True,
                text=True,
                timeout=1,
            )
            parent, name = result.stdout.strip().split(maxsplit=1)
            return int(parent), Path(name).name
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    parent_line = next((line for line in status.splitlines() if line.startswith("PPid:")), None)
    if parent_line is None:
        return None
    try:
        return int(parent_line.split()[1]), name
    except (IndexError, ValueError):
        return None


def _running_under_mosh(pid: int | None = None) -> bool:
    current = pid or os.getpid()
    seen: set[int] = set()
    while current > 1 and current not in seen and len(seen) < 64:
        seen.add(current)
        process = _process_parent(current)
        if process is None:
            return False
        parent, name = process
        if name == "mosh-server":
            return True
        current = parent
    return False


def _emit_commands(
    lines: Sequence[str],
    *,
    server: str,
    encoding: str,
    source: str,
) -> tuple[str, ...]:
    if server not in {"bare", "tinymush", "tinymux"}:
        raise ValueError(f"{source} requires a bare, tinymush, or tinymux world")
    text = "\n".join(lines)
    if "\x00" in text:
        raise ValueError(f"{source} contains a NUL character")
    if len(text.encode("utf-8")) > _MULTILINE_PASTE_MAXIMUM_BYTES:
        raise ValueError(f"{source} exceeds the {_MULTILINE_PASTE_MAXIMUM_BYTES} byte limit")
    if len(lines) > _MULTILINE_PASTE_MAXIMUM_LINES:
        raise ValueError(f"{source} exceeds the {_MULTILINE_PASTE_MAXIMUM_LINES} line limit")
    commands = tuple(f"@emit {escape_world_text(line, server)}" for line in lines)
    for line_number, command in enumerate(commands, start=1):
        try:
            command_size = len(command.encode(encoding, errors="strict"))
        except (LookupError, UnicodeEncodeError) as exc:
            raise ValueError(
                f"{source} line {line_number} cannot be encoded as {encoding}"
            ) from exc
        if command_size > _MULTILINE_PASTE_MAXIMUM_COMMAND_BYTES:
            raise ValueError(
                f"{source} line {line_number} exceeds the "
                f"{_MULTILINE_PASTE_MAXIMUM_COMMAND_BYTES} byte command limit"
            )
    return commands


def _multiline_paste_commands(text: str, *, server: str, encoding: str) -> tuple[str, ...]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return _emit_commands(
        lines,
        server=server,
        encoding=encoding,
        source="multiline paste",
    )


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


def _format_transition_timestamp(timestamp: datetime) -> str:
    local = timestamp.astimezone()
    local_zone = local.tzname() or "local"
    utc = timestamp.astimezone(UTC)
    return (
        f"local {local:%Y-%m-%d %H:%M:%S} {local_zone}; "
        f"UTC {utc:%Y-%m-%d %H:%M:%S} UTC"
    )


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
        typing_inserted_handler: Callable[[], None],
        animation_state: Callable[[], tuple[float, bool]],
        typing_glow: TypingGlowConfig,
        typing_glow_start_color: str,
        typing_glow_end_color: str,
        typing_glow_bold: bool,
        open_url_handler: Callable[[str], None],
    ) -> None:
        self.session = session
        self.display = display
        self.is_agent = is_agent
        self.unread_events = 0
        self.last_inbound_at: float | None = None
        self.recent_input_lines = recent_input_lines
        self.recent_commands: deque[RecentCommand] = deque(maxlen=recent_input_lines)
        self._copy_handler = copy_handler
        self._invalidate_handler = invalidate_handler
        self._animation_state = animation_state
        self._open_url_handler = open_url_handler
        self._selection_anchor: tuple[int, int] | None = None
        self._selection_head: tuple[int, int] | None = None
        self._selection_dragged = False
        self._selection_active = False
        self._selection_dragging = False
        self.typing_glow_tracker = TypingGlowTracker()
        self.typing_glow_processor = TypingGlowProcessor(
            tracker=self.typing_glow_tracker,
            enabled=lambda: typing_glow.enabled and self._animation_state()[1],
            duration_seconds=typing_glow.duration_seconds,
            start_color=typing_glow_start_color,
            end_color=typing_glow_end_color,
            bold=typing_glow_bold,
        )
        self.input_buffer = TypingGlowBuffer(
            tracker=self.typing_glow_tracker,
            enabled=lambda: typing_glow.enabled and self._animation_state()[1],
            inserted_handler=typing_inserted_handler,
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
                Window(
                    content=BufferControl(
                        buffer=self.input_buffer,
                        input_processors=[self.typing_glow_processor],
                    ),
                    height=1,
                ),
            ],
            height=1,
        )

    def recent_input_text(self) -> StyleAndTextTuples:
        commands = [RecentCommand("")] * (self.recent_input_lines - len(self.recent_commands))
        output: StyleAndTextTuples = []
        entries = [*commands, *self.recent_commands]
        now = time.monotonic()
        _elapsed, animated = self._animation_state()
        for line_number, entry in enumerate(entries):
            if line_number:
                output.append(("class:input.recent", "\n"))
            text = str(entry).replace("\r", "").replace("\n", " ")
            output.extend(
                _transient_plain_text(
                    text,
                    base_style="class:input.recent",
                    spans=getattr(entry, "transient_style_spans", ()),
                    now=now,
                    animated=animated,
                )
            )
        return output

    def transient_style_frame_delay(self, now: float, *, animated: bool) -> float | None:
        delays = [
            span.frame_delay(now, animated=animated)
            for command in self.recent_commands
            for span in getattr(command, "transient_style_spans", ())
        ]
        active = [delay for delay in delays if delay is not None]
        return min(active, default=None)

    def output_text(self) -> StyleAndTextTuples:
        elapsed_seconds, animations_enabled = self._animation_state()
        rows = self.display.padded_visible_rows(
            elapsed_seconds=elapsed_seconds,
            animations_enabled=animations_enabled,
            now_seconds=time.monotonic(),
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
        mouse_mode: str = "tfr",
        plugins: PluginManager | None = None,
        agents: AgentRuntime | None = None,
        service_runtime: ServiceRuntime | None = None,
        gateway_reconnect: Callable[[], Coroutine[Any, Any, str]] | None = None,
        restart_supported: bool = False,
        update_checker: UpdateChecker | None = None,
        plugin_update_checker: PluginUpdateChecker | None = None,
        gateway_build: BuildIdentity | None = None,
        animations_enabled: bool = True,
        low_bandwidth: bool = False,
        output_color: str | None = None,
        theme: ThemeConfig | None = None,
        screen_clear_mode: str = "cycle",
        screen_clear_effect: str | None = None,
        boss_screen_mode: str = "cycle",
        boss_screen: str | None = None,
        initial_events: Sequence[Event] = (),
        initial_scroll_to_end: bool = True,
        replay_mode: bool = False,
        spellcheck: SpellcheckConfig | None = None,
        typing_glow: TypingGlowConfig | None = None,
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
        if mouse_mode not in {"auto", "terminal", "tfr"}:
            raise ValueError("mouse mode must be auto, terminal, or tfr")
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
        self.plugin_update_checker = plugin_update_checker
        self.gateway_build = gateway_build
        self.restart_requested = False
        self.update_restart_requested = False
        self.animations_enabled = animations_enabled
        self.low_bandwidth = low_bandwidth
        self.theme: ResolvedTheme = resolve_theme(theme, output_color=output_color)
        self.typing_glow = typing_glow or TypingGlowConfig()
        resolved_output_color = self.theme.output_color if theme is not None else output_color
        self._animation_epoch = time.monotonic()
        self._animation_paused_at = self._animation_epoch if low_bandwidth else None
        self._border_frame_elapsed = 0.0
        self._combo_tracker = ComboTracker()
        self._accept_combo_events = True
        self._combo_notices: dict[str, ComboNotice] = {}
        self._firework_particles: list[FireworkParticle] = []
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
        self._startup_notices: list[tuple[str, str]] = []
        self.replay_mode = replay_mode
        spellcheck_config = spellcheck or SpellcheckConfig()
        self.spellcheck_enabled = spellcheck_config.enabled
        self._spellcheck_protected_words = frozenset(spellcheck_config.protected_words)
        self._spellcheck_world_words = {
            world: frozenset(words) for world, words in spellcheck_config.worlds.items()
        }
        self._spellchecker = LocalSpellChecker()
        self._spellcheck_pending: dict[UUID, tuple[str, tuple[Correction, ...]]] = {}
        self._spellcheck_echoes: dict[str, deque[PendingSpellcheckEcho]] = {
            session.world: deque(maxlen=_MAX_PENDING_SPELLCHECK_ECHOES) for session in sessions
        }
        self._spellcheck_undo: dict[str, str] = {}
        self._observed_speakers: dict[str, set[str]] = {
            session.world: set() for session in sessions
        }
        self.recent_input_lines = recent_input_lines
        self.mouse_mode = mouse_mode
        self._mosh_detected = _running_under_mosh() if mouse_mode == "auto" else False
        self.aliases = [session.world for session in sessions]
        self.world_switch_aliases: dict[str, str] = {}
        for session in sessions:
            for shortcut in session.config.aliases:
                previous = self.world_switch_aliases.get(shortcut)
                if previous is not None:
                    raise ValueError(
                        f"duplicate world-switch alias {shortcut!r} for worlds "
                        f"{previous!r} and {session.world!r}"
                    )
                self.world_switch_aliases[shortcut] = session.world
        self.active_index = 0
        self.inspector_agent: str | None = None
        self._event_queue: asyncio.Queue[Event] | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._active_multiline_pastes: set[str] = set()
        self._image_operation_active = False
        self._image_work_lock = asyncio.Lock()
        self.image_preview: ImagePreview | None = None
        self.views: dict[str, WorldView] = {}

        for session in sessions:
            alias = session.world

            def accept(buffer: Buffer, world: str = alias) -> bool:
                text = buffer.text
                if text:
                    self._spawn(self.submit_text(world, text))
                else:
                    view = self.views[world]
                    view.recent_commands.append(RecentCommand(""))
                    buffer.history_forward(count=1_000_000)
                    buffer.document = Document()
                    self.application.invalidate()
                    return True
                return False

            self.views[alias] = WorldView(
                session=session,
                display=DisplayBuffer(
                    max_rows=scrollback_lines[alias],
                    pager_enabled=pager_enabled,
                    pager_overlap=pager_overlap,
                    default_style=(
                        f"fg:{resolved_output_color}" if resolved_output_color is not None else ""
                    ),
                ),
                is_agent=alias in agent_worlds,
                recent_input_lines=recent_input_lines,
                accept_handler=accept,
                copy_handler=self._copy_selection,
                invalidate_handler=lambda: self.application.invalidate(),
                typing_inserted_handler=self._typing_glow_inserted,
                animation_state=lambda: (
                    self._border_frame_elapsed,
                    self.animations_enabled and not self.low_bandwidth,
                ),
                typing_glow=self.typing_glow,
                typing_glow_start_color=self.typing_glow.highlight_color,
                typing_glow_end_color=self.theme.palette.text,
                typing_glow_bold=self.typing_glow.bold,
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
            ],
            style="class:application",
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
        self.image_preview_control = FormattedTextControl(
            self.image_preview_text,
            focusable=True,
            key_bindings=self._create_image_preview_bindings(),
            show_cursor=False,
            modal=True,
        )
        self.image_preview_window = Window(
            content=self.image_preview_control,
            wrap_lines=False,
            always_hide_cursor=True,
            style="class:application",
        )
        normal_content = DynamicContainer(
            lambda: (
                self.image_preview_window
                if self.image_preview is not None
                else self.boss_window if self.boss_mode else self.normal_root
            )
        )
        self._firework_floats = [
            Float(
                content=Window(
                    content=FormattedTextControl(
                        lambda index=index: self._firework_particle_text(index)
                    ),
                    width=1,
                    height=1,
                    always_hide_cursor=True,
                ),
                top=0,
                left=0,
                width=1,
                height=1,
                transparent=True,
                z_index=20,
            )
            for index in range(36)
        ]
        root = FloatContainer(content=normal_content, floats=self._firework_floats)
        self.application: Application[int] = Application(
            layout=Layout(root, focused_element=self.active_view.input_buffer),
            key_bindings=bindings,
            full_screen=True,
            mouse_support=Condition(lambda: self.effective_mouse_mode == "tfr"),
            style=Style.from_dict(dict(self.theme.styles)),
            before_render=self._before_render,
            input=input,
            output=output,
        )
        self.plugins.initialize_boss_selection(boss_screen_mode, boss_screen)
        self.plugins.set_notice_handler(self.add_notice)
        self.plugins.set_boss_state_handler(self._boss_state_changed)
        shadowed = self._shadowed_world_aliases()
        if shadowed:
            details = ", ".join(f"/{name} ({reason})" for name, reason in shadowed.items())
            for world in self.aliases:
                self.queue_startup_notice(
                    world,
                    f"World-switch aliases shadowed by commands: {details}",
                )

    @property
    def active_alias(self) -> str:
        return self.aliases[self.active_index]

    @property
    def active_view(self) -> WorldView:
        return self.views[self.active_alias]

    @property
    def boss_mode(self) -> bool:
        return self.plugins.boss_active

    @property
    def effective_mouse_mode(self) -> str:
        if self.mouse_mode == "auto":
            return "terminal" if self._mosh_detected else "tfr"
        return self.mouse_mode

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
            if self.image_preview is None:
                self.switch_relative(1)

        @bindings.add("escape", "left")
        @bindings.add("c-left")
        @bindings.add("f5")
        def previous_world(_event: Any) -> None:
            if self.image_preview is None:
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

        tab_pages_output = Condition(
            lambda: (
                self.inspector_agent is None
                and self.image_preview is None
                and not self.active_view.input_buffer.text
                and self.active_view.display.pager.more_rows > 0
            )
        )

        @bindings.add("tab", filter=tab_pages_output)
        def tab_page_down(event: Any) -> None:
            page_down(event)

        @bindings.add("end")
        def jump_to_end(event: Any) -> None:
            if self.inspector_agent is not None:
                self.inspector_window.vertical_scroll = 0
                event.app.invalidate()
                return
            self._jump_to_end(self.active_alias)

        @bindings.add("c-r")
        def reconnect(_event: Any) -> None:
            if self.active_alias in self._active_multiline_pastes:
                self.add_notice(
                    self.active_alias,
                    "Wait for the paced transfer before reconnecting",
                )
            else:
                self._spawn(self.reconnect_world(self.active_alias))

        @bindings.add(Keys.BracketedPaste)
        def bracketed_paste(event: Any) -> None:
            text = event.data.replace("\r\n", "\n").replace("\r", "\n")
            lines = text.split("\n")
            if lines and not lines[-1]:
                lines.pop()
            if len(lines) <= 1:
                event.current_buffer.insert_text(lines[0] if lines else "")
                return
            self._spawn(self.submit_multiline_paste(self.active_alias, text))

        @bindings.add("c-l")
        def clear_screen(_event: Any) -> None:
            self.clear_screen(self.active_alias)

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

    def _create_image_preview_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("escape")
        def cancel(event: Any) -> None:
            self.image_preview = None
            event.app.layout.focus(self.active_view.input_buffer)
            event.app.invalidate()

        @bindings.add("enter")
        def confirm(event: Any) -> None:
            preview = self.image_preview
            if preview is None or preview.error is not None or preview.rendering:
                return
            view = self.views[preview.alias]
            if (
                view.session.state is not SessionState.CONNECTED
                or view.session.connection_generation != preview.connection_generation
                or view.session.config.server != preview.server
                or view.session.encoding != preview.encoding
            ):
                preview.error = "World connection changed; cancel and create a new image preview"
                event.app.invalidate()
                return
            if preview.alias in self._active_multiline_pastes:
                preview.error = f"A paced transfer is already active for {preview.alias}"
                event.app.invalidate()
                return
            self.image_preview = None
            self._active_multiline_pastes.add(preview.alias)
            event.app.layout.focus(self.active_view.input_buffer)
            self._spawn(
                self._submit_paced_commands(
                    preview.alias,
                    preview.commands,
                    source="image",
                    metadata={
                        "image_width": preview.rendered.width,
                        "image_height": preview.rendered.height,
                        "image_mode": preview.rendered.mode,
                    },
                    reserved=True,
                    expected_generation=preview.connection_generation,
                    expected_server=preview.server,
                    expected_encoding=preview.encoding,
                )
            )
            event.app.invalidate()

        @bindings.add("+")
        @bindings.add("=")
        def wider(_event: Any) -> None:
            self._resize_image_preview(1)

        @bindings.add("-")
        def narrower(_event: Any) -> None:
            self._resize_image_preview(-1)

        @bindings.add("a")
        def ascii_mode(_event: Any) -> None:
            self._set_image_preview_mode("ascii")

        @bindings.add("u")
        def unicode_mode(_event: Any) -> None:
            preview = self.image_preview
            if preview is not None and preview.unicode_allowed:
                self._set_image_preview_mode("braille")

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

    def image_preview_text(self) -> StyleAndTextTuples:
        preview = self.image_preview
        if preview is None:
            return []
        mode = (
            "Unicode Braille"
            if preview.rendered.mode == "braille"
            else "ASCII color" if preview.requested_with_color else "ASCII grayscale"
        )
        controls = (
            f"Image preview for {preview.alias}: {preview.rendered.width}x"
            f"{preview.rendered.height}, {mode}\n"
            "+/- width  A ASCII"
            + ("  U Unicode" if preview.unicode_allowed else "")
            + "  Enter send  Esc cancel\n\n"
        )
        output: StyleAndTextTuples = [("class:notice", controls)]
        if preview.error is not None:
            output.append(("class:notice", preview.error))
            return output
        if preview.rendering:
            output.append(("class:notice", "Updating preview...\n\n"))
        for index, line in enumerate(preview.rendered.lines):
            output.extend(safe_ansi_formatted_text(line))
            if index + 1 < len(preview.rendered.lines):
                output.append(("", "\n"))
        return output

    def _resize_image_preview(self, amount: int) -> None:
        preview = self.image_preview
        if preview is None:
            return
        width = min(MAXIMUM_IMAGE_WIDTH, max(1, preview.requested_width + amount))
        if width != preview.requested_width:
            preview.requested_width = width
            self._schedule_image_preview_render()

    def _set_image_preview_mode(self, mode: ImageGlyphMode) -> None:
        preview = self.image_preview
        if preview is None or preview.requested_mode == mode:
            return
        preview.requested_mode = mode
        self._schedule_image_preview_render()

    def _schedule_image_preview_render(self) -> None:
        preview = self.image_preview
        if preview is None or preview.rendering:
            return
        preview.rendering = True
        self._spawn(self._rerender_image_preview(preview))

    async def _rerender_image_preview(self, preview: ImagePreview) -> None:
        while self.image_preview is preview:
            selected_width = preview.requested_width
            selected_mode = preview.requested_mode
            try:
                async with self._image_work_lock:
                    rendered = await asyncio.to_thread(
                        render_image,
                        preview.image,
                        width=selected_width,
                        mode=selected_mode,
                        with_color=preview.requested_with_color,
                    )
                commands = self._image_commands(preview.alias, rendered)
            except ValueError as exc:
                preview.error = str(exc)
            else:
                preview.rendered = rendered
                preview.commands = commands
                preview.error = None
            if (
                selected_width == preview.requested_width
                and selected_mode == preview.requested_mode
            ):
                break
        preview.rendering = False
        self.application.invalidate()

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

    def _jump_to_end(self, alias: str) -> None:
        self._end_screen_clear_for(alias)
        self.views[alias].clear_selection()
        self.views[alias].display.pager.jump_to_end()
        self._sync_animation_task(restart=True)
        self.application.invalidate()

    def clear_screen(self, alias: str) -> None:
        view = self.views[alias]
        self.inspector_agent = None
        view.recent_commands.clear()
        view.input_buffer.history_forward(count=1_000_000)
        view.input_buffer.document = Document()
        self.start_screen_clear(alias)

    def start_screen_clear(self, alias: str) -> None:
        view = self.views[alias]
        view.clear_selection()
        if self.low_bandwidth:
            view.display.clear_screen()
            self._stop_screen_clear()
            return
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
        more = ""
        activity = ""
        more_start = 3
        activity_start = 13
        if panel == "output" and edge is BorderEdge.BOTTOM:
            if view.display.pager.more_rows and activity_start < length:
                more = f" More {min(9_999, view.display.pager.more_rows):>4}"
            activity_text = self._activity_border_text(max(0, length - activity_start - 2))
            if activity_text:
                activity = f" {activity_text}"
        notice = self._combo_notices.get(view.session.world)
        notice_start = -1
        notice_style = ""
        if (
            panel == "output"
            and edge is BorderEdge.BOTTOM
            and notice is not None
            and self.animations_enabled
            and not self.low_bandwidth
        ):
            age = time.monotonic() - notice.started_at
            if age >= 3.6:
                self._combo_notices.pop(view.session.world, None)
                notice = None
            elif len(notice.text) <= length - 2:
                notice_start = max(1, (length - len(notice.text)) // 2)
                reversed_phase = age >= 3.0 and int((age - 3.0) / 0.1) % 2 == 0
                notice_style = f"fg:{notice.color}" + (" reverse" if reversed_phase else "")

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
            if more_start <= index < more_start + len(more):
                fragment = replace(
                    fragment,
                    character=more[index - more_start],
                    style="class:border.more",
                )
            elif activity_start <= index < activity_start + len(activity):
                fragment = replace(
                    fragment,
                    character=activity[index - activity_start],
                    style="class:border.activity",
                )
            if notice is not None and notice_start <= index < notice_start + len(notice.text):
                fragment = replace(
                    fragment,
                    character=notice.text[index - notice_start],
                    style=notice_style,
                )
            output.append((fragment.style, fragment.character, mouse_handler))
            if edge in {BorderEdge.LEFT, BorderEdge.RIGHT} and index + 1 < length:
                output.append(("", "\n"))
        return output

    def _activity_border_text(self, maximum: int) -> str:
        unread = [
            f"{''.join(character if get_cwidth(character) == 1 else '?' for character in alias)} "
            f"+{self.views[alias].unread_events}"
            for alias in self.aliases
            if self.views[alias].unread_events
        ]
        if not unread:
            return ""
        prefix = "Activity in world(s): "
        ellipsis = "... "
        if len(prefix) + len(ellipsis) > maximum:
            return ""
        complete = prefix + ", ".join(unread) + " "
        if len(complete) <= maximum:
            return complete
        included: list[str] = []
        for entry in unread:
            candidate = prefix + ", ".join((*included, entry)) + ", ... "
            if len(candidate) > maximum:
                break
            included.append(entry)
        return prefix + ", ".join(included) + (", ... " if included else "... ")

    def _before_render(self, app: Application[Any]) -> None:
        self._border_frame_elapsed = self._animation_elapsed_seconds()
        size = app.output.get_size()
        output_height = max(1, size.rows - 7 - self.recent_input_lines)
        output_width = max(1, size.columns - 2)
        self._position_firework_particles(output_width, output_height)
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

    def _typing_glow_inserted(self) -> None:
        self._sync_animation_task(restart=True)
        self.application.invalidate()

    def _background_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        if exception := task.exception():
            self.add_notice(
                self.active_alias,
                f"Client operation failed: {type(exception).__name__}: {exception}",
            )

    def switch_relative(self, amount: int) -> None:
        self.switch_world(self.aliases[(self.active_index + amount) % len(self.aliases)])

    def switch_world(self, alias: str) -> None:
        if self.image_preview is not None:
            return
        if alias not in self.views:
            self.add_notice(self.active_alias, f"Unknown world: {alias}")
            return
        if alias != self.active_alias and self._screen_clear_world is not None:
            self._stop_screen_clear()
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
        self.views[alias].display.append(
            self.theme.ansi_text("warning", f"-- {text} --"), recallable=False
        )
        self.application.invalidate()

    def queue_startup_notice(self, alias: str, text: str) -> None:
        self._startup_notices.append((alias, text))

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

    def _spellcheck_echo_start(
        self,
        event: Event,
        pending: PendingSpellcheckEcho,
        message: str,
    ) -> int | None:
        payload = speech_payload(pending.text)
        if payload is None:
            return None
        payload_start, candidate = payload
        candidate_start = message.find(candidate)
        if candidate_start < 0 or candidate_start != message.rfind(candidate):
            return None

        prefix = pending.text[:payload_start].casefold()
        echo_prefix = message[:candidate_start].casefold()
        echo_suffix = message[candidate_start + len(candidate) :].rstrip("\r\n")
        bare_raw = event.kind is EventKind.RAW_OUTPUT and event.parser_name in {"bare", "generic"}
        if bare_raw and message.rstrip("\r\n") == pending.text:
            return payload_start
        character = self.views[event.world].session.character_name
        character_name = character.casefold() if character is not None else None
        if prefix.startswith('"') or prefix.startswith("say "):
            if event.kind is not EventKind.SAY and not bare_raw:
                return None
            expected = {'you say, "', "you say, “"}
            if character_name is not None:
                expected.update(
                    {
                        f'{character_name} says, "',
                        f"{character_name} says, “",
                    }
                )
            if echo_prefix not in expected or echo_suffix not in {'', '"', "”"}:
                return None
        else:
            if (
                event.kind not in {EventKind.POSE, EventKind.SPEECH}
                and not bare_raw
            ) or character_name is None:
                return None
            expected = {
                character_name,
                f"{character_name} ",
                f"{character_name}'s ",
                f"{character_name}’s ",
            }
            if echo_prefix not in expected or echo_suffix:
                return None
        return payload_start

    def _spellcheck_echo_spans(
        self,
        event: Event,
        display_text: str,
    ) -> tuple[TransientStyleSpan, ...]:
        if event.kind not in {EventKind.SAY, EventKind.POSE, EventKind.SPEECH} and not (
            event.kind is EventKind.RAW_OUTPUT and event.parser_name in {"bare", "generic"}
        ):
            return ()
        message_text = event.metadata.get("message_text")
        if not isinstance(message_text, str):
            return ()

        now = time.monotonic()
        pending_echoes = self._spellcheck_echoes[event.world]
        retained: deque[PendingSpellcheckEcho] = deque(
            maxlen=_MAX_PENDING_SPELLCHECK_ECHOES
        )
        plain_message = terminal_plain_text(message_text)
        plain_display = terminal_plain_text(display_text)
        matched: PendingSpellcheckEcho | None = None
        payload_start = 0
        display_start = -1
        for pending in pending_echoes:
            if (
                pending.connection_generation != event.connection_generation
                or now - pending.queued_at > _SPELLCHECK_ECHO_TIMEOUT_SECONDS
            ):
                continue
            candidate_start = self._spellcheck_echo_start(event, pending, plain_message)
            if matched is None and candidate_start is not None:
                candidate = pending.text[candidate_start:]
                candidate_display_start = plain_display.find(candidate)
                if candidate_display_start < 0 or candidate_display_start != plain_display.rfind(
                    candidate
                ):
                    retained.append(pending)
                    continue
                matched = pending
                payload_start = candidate_start
                display_start = candidate_display_start
                continue
            retained.append(pending)
        self._spellcheck_echoes[event.world] = retained
        if matched is None:
            return ()

        highlight_color = derive_bright_color(self.theme.palette.warning)
        return tuple(
            TransientStyleSpan(
                start=display_start + correction.start - payload_start,
                end=display_start + correction.end - payload_start,
                style="bold underline",
                start_color=highlight_color,
                end_color=self.theme.output_color,
                started_at=now,
            )
            for correction in matched.corrections
        )

    def _combo_decoration(self, combo: Combo, elapsed_seconds: float) -> TextDecoration | None:
        if combo.count == 4:
            effect, duration, pulses = TextEffectKind.COMBO_PULSE, 1.0, 2
        elif combo.count == 5:
            effect, duration, pulses = TextEffectKind.COMBO_FLASH_UPPER, 2.0, 1
        elif combo.count == 6:
            effect, duration, pulses = TextEffectKind.COMBO_CYLON, 4.0, 1
        elif combo.count == 7:
            effect, duration, pulses = TextEffectKind.COMBO_PULSE, 2.0, 2
        else:
            return None
        return TextDecoration(
            start=combo.body_start,
            end=combo.body_end,
            effect=effect,
            base_color=self.theme.output_color,
            accent_color="#ffffff",
            interval_seconds=duration,
            repeat_seconds=duration + 1,
            phase_offset_seconds=-elapsed_seconds,
            frames_per_second=20,
            loop=False,
            effect_width=2,
            sparkle_count=pulses,
        )

    def _start_fireworks(self, event: Event) -> None:
        value = event.event_id.int & 0xFFFFFFFF

        def random_value() -> float:
            nonlocal value
            value = (1_664_525 * value + 1_013_904_223) & 0xFFFFFFFF
            return value / 2**32

        colors = ("#ff0000", "#ff8000", "#0070dd", "#ffff00", "#a335ee")
        glyphs = ("*", "+", "·")
        now = time.monotonic()
        for _burst in range(1 + int(random_value() * 3)):
            delay = random_value() * 2
            origin_x = 0.12 + random_value() * 0.76
            origin_y = 0.12 + random_value() * 0.45
            color = colors[int(random_value() * len(colors))]
            for _particle in range(12):
                self._firework_particles.append(
                    FireworkParticle(
                        world=event.world,
                        started_at=now + delay,
                        origin_x=origin_x,
                        origin_y=origin_y,
                        velocity_x=(random_value() - 0.5) * 0.34,
                        velocity_y=-(0.22 + random_value() * 0.34),
                        color=color,
                        glyph=glyphs[int(random_value() * len(glyphs))],
                    )
                )
        self._firework_particles = self._firework_particles[-36:]

    def _firework_particle_text(self, index: int) -> StyleAndTextTuples:
        if not self.animations_enabled or self.low_bandwidth:
            return []
        if index >= len(self._firework_particles):
            return []
        particle = self._firework_particles[index]
        age = time.monotonic() - particle.started_at
        if particle.world != self.active_alias or not 0 <= age < 2.2:
            return []
        return [(f"fg:{particle.color} bold", particle.glyph)]

    def _position_firework_particles(self, width: int, height: int) -> None:
        now = time.monotonic()
        self._firework_particles = [
            particle
            for particle in self._firework_particles
            if now - particle.started_at < 2.2
        ]
        for index, float_container in enumerate(self._firework_floats):
            if index >= len(self._firework_particles):
                float_container.top = 0
                float_container.left = 0
                continue
            particle = self._firework_particles[index]
            age = max(0.0, now - particle.started_at)
            x = particle.origin_x + particle.velocity_x * age
            y = particle.origin_y + particle.velocity_y * age + 0.28 * age * age
            float_container.left = 1 + min(width - 1, max(0, int(x * width)))
            float_container.top = 2 + min(height - 1, max(0, int(y * height)))

    @staticmethod
    def _inferred_display_speaker(event: Event, event_text: str | None) -> Event:
        if (
            event_text is None
            or event.parser_name not in {"bare", "generic"}
            or event.provenance is not None
            or event.kind not in {EventKind.RAW_OUTPUT, EventKind.SAY}
        ):
            return event
        plain_text = terminal_plain_text(event_text).lstrip()
        token = plain_text.split(maxsplit=1)[0] if plain_text else ""
        if token.casefold().endswith(("'s", "’s")):
            token = token[:-2]
        if not token or len(token) > _MAX_OBSERVED_SPEAKER_LENGTH:
            return event
        return replace(
            event,
            kind=EventKind.POSE if event.kind is EventKind.RAW_OUTPUT else event.kind,
            provenance=Provenance(
                sender_name=token,
                adapter=event.parser_name,
                confidence=Confidence.INFERRED,
            ),
            confidence=Confidence.INFERRED,
        )

    def handle_event(self, event: Event) -> None:
        self.plugins.observe_ui_event(event)
        view = self.views.get(event.world)
        if view is None:
            return
        should_count = False
        if event.direction is Direction.INBOUND:
            view.last_inbound_at = time.monotonic()
            if event.provenance is not None and event.provenance.sender_name:
                observed = self._observed_speakers[event.world]
                sender_name = event.provenance.sender_name
                if (
                    len(observed) < _MAX_OBSERVED_SPEAKERS
                    and len(sender_name) <= _MAX_OBSERVED_SPEAKER_LENGTH
                ):
                    observed.add(sender_name)
            event_text = event.display_text
            if event.provenance is not None and event.provenance.prefix_span is not None:
                if view.session.show_nospoof_prefix:
                    event_text = event.canonical_text
                else:
                    message_text = event.metadata.get("message_text")
                    if isinstance(message_text, str):
                        event_text = next(
                            (
                                projected
                                for source in (event.display_text, event.canonical_text)
                                if source is not None
                                and (projected := ansi_visible_text(source, message_text))
                                is not None
                            ),
                            message_text,
                        )
            display_event = (
                event
                if event_text == event.display_text
                else replace(event, display_text=event_text)
            )
            spoof_span: tuple[int, int] | None = None
            if (
                event_text is not None
                and event.spoof is not None
                and event.spoof.status is SpoofStatus.SPOOFED
            ):
                plain_event_text = terminal_plain_text(event_text)
                message_text = event.metadata.get("message_text")
                plain_message = (
                    terminal_plain_text(message_text) if isinstance(message_text, str) else None
                )
                message_start = (
                    plain_event_text.find(plain_message) if plain_message is not None else -1
                )
                if message_start >= 0:
                    start, end = event.spoof.speaker_span
                    spoof_span = message_start + start, message_start + end
            display_text = (
                self.plugins.transform_display(display_event)
                if self.plugins is not None
                else event_text
            )
            if display_text is not None:
                effect_event = self._inferred_display_speaker(display_event, event_text)
                decorations = (
                    self.plugins.decorate_display(effect_event, display_text)
                    if self.plugins is not None
                    else ()
                )
                presentations = (
                    self.plugins.presentation_programs(
                        effect_event,
                        terminal_plain_text(display_text),
                    )
                    if self.plugins is not None
                    else ()
                )
                elapsed_seconds = self._animation_elapsed_seconds()
                combo = None
                if self._accept_combo_events and not (
                    event.kind is EventKind.RAW_OUTPUT and not (decorations or presentations)
                ):
                    combo = self._combo_tracker.observe(
                        effect_event, display_text, time.monotonic()
                    )
                combo_enabled = (
                    self.animations_enabled
                    and not self.low_bandwidth
                    and event.world == self.active_alias
                )
                combo_decoration = (
                    self._combo_decoration(combo, elapsed_seconds)
                    if combo is not None and combo_enabled
                    else None
                )
                if combo is not None and combo_enabled:
                    self._combo_notices[event.world] = ComboNotice(
                        combo.notice, combo.color, time.monotonic()
                    )
                    if combo.count == 7:
                        self._start_fireworks(event)
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
                if combo_decoration is not None:
                    decorations = (*decorations, combo_decoration)
                presentations = tuple(
                    ActiveEffectProgram(
                        program,
                        phase_offset_seconds=event_age_seconds - elapsed_seconds,
                    )
                    for program in presentations
                )
                view.clear_selection()
                transient_style_spans = self._spellcheck_echo_spans(event, display_text)
                if combo is not None and combo_enabled and combo.count == 3:
                    transient_style_spans = (
                        *transient_style_spans,
                        TransientStyleSpan(
                            start=combo.body_start,
                            end=combo.body_end,
                            style="bold",
                            start_color=self.theme.output_color,
                            end_color=self.theme.output_color,
                            started_at=time.monotonic(),
                            duration_seconds=0.25,
                            purpose="combo",
                        ),
                    )
                style_spans: list[StaticStyleSpan] = []
                if spoof_span is not None and event.spoof is not None:
                    plain_display = terminal_plain_text(display_text)
                    start, end = spoof_span
                    if plain_display[start:end].casefold() == event.spoof.speaker.casefold():
                        style_spans.append(
                            StaticStyleSpan(
                                start=start,
                                end=end,
                                style="reverse",
                            )
                        )
                view.display.append(
                    display_text,
                    decorations=decorations,
                    presentations=presentations,
                    style_spans=tuple(style_spans),
                    transient_style_spans=transient_style_spans,
                )
                should_count = True
        elif (
            event.direction is Direction.OUTBOUND
            and event.kind is EventKind.COMMAND
            and event.actor is not None
            and event.actor.type is ActorType.HUMAN
            and event.display_text is not None
        ):
            pending = (
                self._spellcheck_pending.pop(event.correlation_id, None)
                if event.correlation_id is not None
                else None
            )
            if pending is None:
                view.recent_commands.append(RecentCommand(event.display_text))
            else:
                text, corrections = pending
                now = time.monotonic()
                self._spellcheck_echoes[event.world].append(
                    PendingSpellcheckEcho(
                        text=text,
                        corrections=corrections,
                        connection_generation=event.connection_generation,
                        queued_at=now,
                    )
                )
                highlight_color = derive_bright_color(self.theme.palette.warning)
                view.recent_commands.append(
                    RecentCommand(
                        event.display_text,
                        tuple(
                            TransientStyleSpan(
                                start=correction.start,
                                end=correction.end,
                                style="bold underline",
                                start_color=highlight_color,
                                end_color=self.theme.palette.muted,
                                started_at=now,
                            )
                            for correction in corrections
                        ),
                    )
                )
        elif event.kind is EventKind.PLUGIN and event.display_text is not None:
            view.clear_selection()
            view.display.append(self.theme.ansi_text("error", f"-- {event.display_text} --"))
            should_count = True
        elif event.kind is EventKind.CONNECTION:
            state = event.metadata.get("state")
            timestamp = _format_transition_timestamp(event.timestamp)
            if state == SessionState.CONNECTED.value:
                view.clear_selection()
                view.display.append(
                    self.theme.ansi_text("success", f"-- Connected [{timestamp}] --")
                )
                should_count = True
            elif state == SessionState.DISCONNECTED.value:
                view.clear_selection()
                reason = event.metadata.get("error")
                suffix = f": {reason}" if reason else ""
                view.display.append(
                    self.theme.ansi_text(
                        "error", f"-- Disconnected{suffix} [{timestamp}] --"
                    )
                )
                should_count = True
            elif state == SessionState.RECONNECT_WAIT.value:
                view.clear_selection()
                delay = event.metadata.get("delay_seconds")
                suffix = f" in {delay:g}s" if isinstance(delay, (int, float)) else ""
                view.display.append(
                    self.theme.ansi_text(
                        "warning", f"-- Reconnecting{suffix} [{timestamp}] --"
                    )
                )
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
        if alias in self._active_multiline_pastes:
            self.add_notice(alias, "A paced transfer is active; input was not sent")
            return
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
        original = text
        corrections: tuple[Correction, ...] = ()
        if self.spellcheck_enabled:
            protected_words = set(self._spellcheck_protected_words)
            protected_words.update(self._spellcheck_world_words.get(alias, ()))
            protected_words.update(self.aliases)
            protected_words.update(self.world_switch_aliases)
            protected_words.update(self._observed_speakers[alias])
            result = self._spellchecker.correct(text, protected_words=protected_words)
            text = result.text
            corrections = result.corrections
        request_id = uuid4()
        request = CommandRequest(
            session_id=view.session.session_id,
            world=alias,
            actor=Actor(ActorType.HUMAN, "operator"),
            text=text,
            request_id=request_id,
        )
        if corrections:
            if len(self._spellcheck_pending) >= 128:
                self._spellcheck_pending.pop(next(iter(self._spellcheck_pending)))
            self._spellcheck_pending[request_id] = (text, corrections)
        try:
            await self.command_bus.submit(request)
        except UnknownSessionError:
            self._spellcheck_pending.pop(request_id, None)
            self.add_notice(alias, "Connection is not accepting commands")
        except BaseException:
            self._spellcheck_pending.pop(request_id, None)
            raise
        else:
            if corrections:
                self._spellcheck_undo[alias] = original
            self._jump_to_end(alias)

    async def submit_multiline_paste(self, alias: str, text: str) -> None:
        view = self.views[alias]
        if view.session.state is not SessionState.CONNECTED:
            self.add_notice(alias, "Not connected; multiline paste was not sent")
            return
        if alias in self._active_multiline_pastes:
            self.add_notice(alias, "A multiline paste is already active for this world")
            return
        try:
            commands = _multiline_paste_commands(
                text,
                server=view.session.config.server,
                encoding=view.session.encoding,
            )
        except ValueError as exc:
            self.add_notice(alias, str(exc))
            return
        if not commands:
            return

        await self._submit_paced_commands(alias, commands, source="multiline_paste")

    async def _submit_paced_commands(
        self,
        alias: str,
        commands: Sequence[str],
        *,
        source: str,
        metadata: dict[str, Any] | None = None,
        reserved: bool = False,
        expected_generation: int | None = None,
        expected_server: str | None = None,
        expected_encoding: str | None = None,
    ) -> None:
        view = self.views[alias]
        label = "image" if source == "image" else "multiline paste"
        generation = (
            expected_generation
            if expected_generation is not None
            else view.session.connection_generation
        )
        if (
            view.session.state is not SessionState.CONNECTED
            or view.session.connection_generation != generation
            or (expected_server is not None and view.session.config.server != expected_server)
            or (expected_encoding is not None and view.session.encoding != expected_encoding)
        ):
            self.add_notice(alias, f"Not connected; {label} was not sent")
            if reserved:
                self._active_multiline_pastes.discard(alias)
            return
        if alias in self._active_multiline_pastes and not reserved:
            self.add_notice(alias, f"A paced transfer is already active for {alias}")
            return

        if not reserved:
            self._active_multiline_pastes.add(alias)
        self._jump_to_end(alias)
        self.add_notice(alias, f"Sending {label} as {len(commands)} paced @emit lines")
        try:
            for line_number, command in enumerate(commands, start=1):
                if line_number > 1:
                    await asyncio.sleep(_MULTILINE_PASTE_DELAY_SECONDS)
                if (
                    view.session.state is not SessionState.CONNECTED
                    or view.session.connection_generation != generation
                ):
                    raise ConnectionError
                line_metadata = dict(metadata or {})
                line_metadata.update(
                    {
                        f"{source}_line": line_number,
                        f"{source}_total_lines": len(commands),
                    }
                )
                await self.command_bus.submit(
                    CommandRequest(
                        session_id=view.session.session_id,
                        world=alias,
                        actor=Actor(ActorType.HUMAN, "operator"),
                        text=command,
                        expected_connection_generation=generation,
                        metadata=line_metadata,
                    )
                )
        except (ConnectionError, OSError, UnknownSessionError, ValueError):
            self.add_notice(alias, f"Connection stopped accepting the {label}")
        finally:
            self._active_multiline_pastes.discard(alias)

    def _image_commands(self, alias: str, rendered: RenderedImage) -> tuple[str, ...]:
        view = self.views[alias]
        return _emit_commands(
            rendered.lines,
            server=view.session.config.server,
            encoding=view.session.encoding,
            source="image",
        )

    async def open_image_preview(self, alias: str, parameters: list[str]) -> None:
        if self.replay_mode:
            self.add_notice(alias, "Image sending is unavailable in replay mode")
            return
        if self._image_operation_active or self.image_preview is not None:
            self.add_notice(alias, "An image operation is already active")
            return
        try:
            width, requested_mode, with_color, path = self._parse_image_parameters(parameters)
        except ValueError as exc:
            self.add_notice(alias, str(exc))
            return
        view = self.views[alias]
        source_generation = view.session.connection_generation
        source_server = view.session.config.server
        source_encoding = view.session.encoding
        unicode_allowed = view.session.config.capabilities.unicode
        mode: ImageGlyphMode = requested_mode or ("braille" if unicode_allowed else "ascii")
        if mode == "braille" and not unicode_allowed:
            self.add_notice(alias, "Unicode image glyphs are disabled for this world")
            return
        self._image_operation_active = True
        try:
            _emit_commands(
                (),
                server=view.session.config.server,
                encoding=view.session.encoding,
                source="image",
            )
            async with self._image_work_lock:
                image = await asyncio.to_thread(
                    load_image_file if path is not None else load_clipboard_image,
                    *([path] if path is not None else []),
                )
                rendered = await asyncio.to_thread(
                    render_image,
                    image,
                    width=width,
                    mode=mode,
                    with_color=with_color,
                )
            commands = _emit_commands(
                rendered.lines,
                server=source_server,
                encoding=source_encoding,
                source="image",
            )
            if (
                view.session.connection_generation != source_generation
                or view.session.config.server != source_server
                or view.session.encoding != source_encoding
            ):
                raise ValueError("world connection changed while creating the preview")
        except (OSError, ValueError) as exc:
            self.add_notice(alias, f"Image preview failed: {exc}")
            return
        finally:
            self._image_operation_active = False
        self.image_preview = ImagePreview(
            alias=alias,
            image=image,
            rendered=rendered,
            commands=commands,
            unicode_allowed=unicode_allowed,
            requested_width=width,
            requested_mode=rendered.mode,
            requested_with_color=with_color,
            connection_generation=source_generation,
            server=source_server,
            encoding=source_encoding,
        )
        self.application.layout.focus(self.image_preview_control)
        self.application.invalidate()

    @staticmethod
    def _parse_image_parameters(
        parameters: list[str],
    ) -> tuple[int, ImageGlyphMode | None, bool, Path | None]:
        width = DEFAULT_IMAGE_WIDTH
        mode: ImageGlyphMode | None = None
        with_color = False
        path: Path | None = None
        index = 0
        while index < len(parameters):
            parameter = parameters[index]
            if parameter == "--width":
                index += 1
                if index >= len(parameters):
                    raise ValueError(
                        "Usage: /image [--width 1-80] [--ascii|--unicode] "
                        "[--withcolor] [path]"
                    )
                try:
                    width = int(parameters[index])
                except ValueError as exc:
                    raise ValueError("image width must be an integer") from exc
            elif parameter == "--ascii":
                if mode is not None:
                    raise ValueError("select only one image glyph mode")
                mode = "ascii"
            elif parameter == "--unicode":
                if mode is not None:
                    raise ValueError("select only one image glyph mode")
                mode = "braille"
            elif parameter == "--withcolor":
                with_color = True
            elif parameter.startswith("--") or path is not None:
                raise ValueError(
                    "Usage: /image [--width 1-80] [--ascii|--unicode] "
                    "[--withcolor] [path]"
                )
            else:
                path = Path(parameter).expanduser()
            index += 1
        if not 1 <= width <= MAXIMUM_IMAGE_WIDTH:
            raise ValueError(f"image width must be between 1 and {MAXIMUM_IMAGE_WIDTH}")
        return width, mode, with_color, path

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
        elif command == "image":
            await self.open_image_preview(alias, parameters)
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
        elif command == "plugins":
            if parameters:
                self.add_notice(alias, "Usage: /plugins")
            else:
                self.add_notice(alias, self._plugin_status_text())
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
            self._jump_to_end(alias)
        elif command == "agent":
            await self._handle_agent_command(alias, parameters)
        elif command == "nospoof":
            self._handle_nospoof_command(alias, parameters)
        elif command == "lowbw":
            self._handle_low_bandwidth_command(alias, parameters)
        elif command == "mouse":
            self._handle_mouse_command(alias, parameters)
        elif command == "animations":
            self._handle_animations_command(alias, parameters)
        elif command == "spellcheck":
            self._handle_spellcheck_command(alias, parameters)
        elif self.plugins is not None and await self.plugins.execute_command(
            command, tuple(parameters), alias
        ):
            pass
        elif command in self.world_switch_aliases:
            if parameters:
                self.add_notice(alias, f"Usage: /{command}")
            else:
                self.switch_world(self.world_switch_aliases[command])
        else:
            self.add_notice(alias, f"Unknown client command: /{command}")

    def _shadowed_world_aliases(self) -> dict[str, str]:
        shadowed: dict[str, str] = {}
        for command in self.world_switch_aliases:
            if command in _CORE_CLIENT_COMMANDS:
                shadowed[command] = "core command"
            elif registered := self.plugins.registry.commands.get(command):
                shadowed[command] = f"{registered[0]} plugin command"
        return shadowed

    def help_text(self) -> str:
        lines = [
            "TFR commands",
            "  /help - show this help",
            "  /sh - temporarily open an interactive local shell",
            "  ! command - run one local shell command; !!TEXT sends a literal !",
            "  /world ALIAS - switch worlds; /next (/n) and /previous (/p) also switch",
            "  /connect, /disconnect, /reconnect - manage the active connection",
            "  /image [--width N] [--ascii|--unicode] [--withcolor] [path] - send an image",
            "  /clear [status|cycle|random|lock EFFECT] - clear input/output or select effect",
            "  /recall X - show the last X retained lines for the active world",
            "  /end - return to live output",
            "  /nospoof show|hide|status - control NOSPOOF prefix visibility",
            "  /lowbw [on|off|status] - suppress continuous UI animation",
            "  /mouse auto|terminal|tfr|status - choose who handles mouse input",
            "  /animations [on|off|status] - enable continuous UI effects",
            "  /spellcheck on|off|status|undo - correct explicit speech and poses locally",
            "  /update - update TFR and stable-auto plugins on the Gateway and all connected UIs",
            "  /update status|check - inspect stable TFR and plugin releases",
            "  /plugins - show configured, loaded, and failed plugins",
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
            "  PageUp/PageDown scroll or page; empty-input Tab also pages when more output exists",
            "  Ctrl-L clear visible input/output; Ctrl-R reconnect; F8 agent inspector",
            "  Ctrl-Q quit; Ctrl-C interrupt",
            "",
            "World-switch aliases",
        ]
        if self.restart_supported:
            lines.insert(
                4,
                "  /reload, /restart - reload this UI without disconnecting the gateway",
            )
        if self.gateway_reconnect is not None:
            lines.insert(5, "  /gateway reconnect - reconnect this UI to the gateway")
        shadowed = self._shadowed_world_aliases()
        if not self.world_switch_aliases:
            lines.append("  none")
        else:
            for command, world in sorted(self.world_switch_aliases.items()):
                suffix = f"; shadowed by {shadowed[command]}" if command in shadowed else ""
                lines.append(f"  /{command} - switch to {world}{suffix}")
        lines.extend(("", "Loaded plugin commands"))
        if not self.plugins.registry.commands:
            lines.append("  none")
        else:
            for command, (plugin, _handler) in sorted(self.plugins.registry.commands.items()):
                description = self.plugins.registry.command_help.get(command, "plugin command")
                lines.append(f"  /{command} ({plugin}) - {description}")
        return "\n".join(lines)

    def _plugin_status_text(self) -> str:
        requested = self.plugins.requested_plugins
        if not requested:
            return "No plugins are configured in plugins.enabled."
        loaded = set(self.plugins.loaded_plugins)
        lines = ["Configured plugins"]
        for name in requested:
            if name in loaded:
                lines.append(f"  {name}: loaded")
            else:
                reason = self.plugins.load_failures.get(name, "not loaded")
                lines.append(f"  {name}: failed ({reason})")
        return "\n".join(lines)

    async def _handle_update_command(self, alias: str, parameters: list[str]) -> None:
        if parameters not in ([], ["status"], ["check"]):
            self.add_notice(alias, "Usage: /update [status|check]")
            return
        core_enabled = self.update_checker is not None and self.update_checker.config.enabled
        plugins_enabled = (
            self.plugin_update_checker is not None and self.plugin_update_checker.enabled
        )
        if not core_enabled and not plugins_enabled:
            self.add_notice(alias, "Stable update checks are disabled")
            return
        if not parameters:
            if not core_enabled:
                self.add_notice(alias, "Stable TFR updates are disabled")
                return
            self.add_notice(
                alias,
                "Checking for stable updates on the Gateway and all connected UIs...",
            )
            try:
                if self.service_runtime is not None and hasattr(
                    self.service_runtime, "request_update"
                ):
                    result = await self.service_runtime.request_update()
                else:
                    core_result = await self.update_checker.check()
                    if core_result.error is not None:
                        raise ValueError(
                            f"cannot check stable TFR updates: {core_result.error}"
                        )
                    core_available = core_result.available_for(self.update_checker.build)
                    plugin_available = (
                        await self.plugin_update_checker.stable_auto_update_available()
                        if self.plugin_update_checker is not None
                        else False
                    )
                    if not core_available and not plugin_available:
                        result = {"updated": False}
                    else:
                        staged = await stage_managed_update(self.update_checker.config)
                        activate_managed_update(staged.release_id)
                        result = {
                            "updated": True,
                            "version": staged.version,
                            "release_url": staged.release_url,
                        }
                        self.update_restart_requested = True
                        get_app().exit(result=0)
            except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                self.add_notice(alias, f"Update failed: {exc}")
                return
            if result.get("updated") is False:
                self.add_notice(alias, "TFR and stable-auto plugins are up to date")
            else:
                self.add_notice(
                    alias,
                    f"Update {result.get('version', 'release')} transaction started; "
                    f"connected UIs and the Gateway are preparing. "
                    f"{result.get('release_url', '')}".rstrip(),
                )
            return
        if parameters == ["check"]:
            scope = (
                "TFR and plugin"
                if core_enabled and plugins_enabled
                else "plugin" if plugins_enabled else "TFR"
            )
            self.add_notice(alias, f"Checking for stable {scope} updates...")
            if core_enabled and plugins_enabled:
                result, plugin_results = await asyncio.gather(
                    self.update_checker.check(),
                    self.plugin_update_checker.check(),
                )
            elif core_enabled:
                result = await self.update_checker.check()
                plugin_results = ()
            else:
                result = None
                plugin_results = await self.plugin_update_checker.check()
            if self.plugin_update_checker is not None:
                self.plugin_update_checker.mark_notified(plugin_results)
        else:
            result = self.update_checker.result if self.update_checker is not None else None
            plugin_results = (
                self.plugin_update_checker.results
                if self.plugin_update_checker is not None
                else ()
            )
        if result is not None:
            self._show_update_status(result, available_only=False, alias=alias)
        self._show_plugin_update_status(plugin_results, available_only=False, alias=alias)

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

    def _show_plugin_update_status(
        self,
        results: Sequence[PluginUpdateResult],
        *,
        available_only: bool,
        alias: str | None = None,
    ) -> None:
        target = alias or self.active_alias
        for result in results:
            if not available_only or result.available:
                self.add_notice(target, format_plugin_update_status(result))

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
            self.clear_screen(alias)
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
        view.display.append(
            self.theme.ansi_text("warning", f"-- Recall {count}"), recallable=False
        )
        elapsed_seconds = self._animation_elapsed_seconds()
        for text, decorations, presentations, style_spans in recalled:
            replayed = tuple(
                replace(decoration, phase_offset_seconds=-elapsed_seconds)
                for decoration in decorations
            )
            view.display.append(
                text,
                decorations=replayed,
                presentations=presentations,
                style_spans=style_spans,
                recallable=False,
            )
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

    def _handle_mouse_command(self, alias: str, parameters: list[str]) -> None:
        operation = parameters[0].casefold() if len(parameters) == 1 else ""
        if operation in {"auto", "terminal", "tfr"}:
            self.mouse_mode = operation
            if operation == "auto":
                self._mosh_detected = _running_under_mosh()
            self.application.invalidate()
        elif operation not in {"", "status"} or len(parameters) > 1:
            self.add_notice(alias, "Usage: /mouse auto|terminal|tfr|status")
            return
        reason = (
            "; mosh-server detected"
            if self.mouse_mode == "auto" and self._mosh_detected
            else ""
        )
        self.add_notice(
            alias,
            f"Mouse mode: {self.mouse_mode} ({self.effective_mouse_mode}{reason})",
        )

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
        if enabled:
            self._clear_combo_visuals()
        if enabled and self._screen_clear_world is not None:
            self._stop_screen_clear()

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
        if not self.animations_enabled:
            self._clear_combo_visuals()
        self._sync_animation_task()
        state = "on" if self.animations_enabled else "off"
        self.add_notice(alias, f"Continuous UI animations are {state}")

    def _clear_combo_visuals(self) -> None:
        self._combo_notices.clear()
        self._firework_particles.clear()
        for view in self.views.values():
            view.display.clear_combo_effects()

    def _handle_spellcheck_command(self, alias: str, parameters: list[str]) -> None:
        operation = parameters[0].casefold() if len(parameters) == 1 else ""
        if operation == "on":
            self.spellcheck_enabled = True
        elif operation == "off":
            self.spellcheck_enabled = False
        elif operation == "undo":
            original = self._spellcheck_undo.get(alias)
            if original is None:
                self.add_notice(alias, "No spell-check correction is available to undo")
                return
            buffer = self.views[alias].input_buffer
            buffer.document = Document(original, cursor_position=len(original))
            self.application.layout.focus(buffer)
            self.application.invalidate()
            self.add_notice(alias, "Restored the original pre-correction draft")
            return
        elif operation not in {"", "status"} or len(parameters) > 1:
            self.add_notice(alias, "Usage: /spellcheck on|off|status|undo")
            return
        state = "on" if self.spellcheck_enabled else "off"
        self.add_notice(alias, f"Submit-time spell checking is {state}")

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
        has_spellcheck_effects = self._spellcheck_frame_delay() is not None
        has_typing_glow = self._typing_glow_frame_delay() is not None
        has_combo_notice = self._combo_frame_delay() is not None
        has_fireworks = self._firework_frame_delay() is not None
        can_animate = (
            self._animations_started
            and self.animations_enabled
            and not self.low_bandwidth
            and not self.boss_mode
        )
        should_run = can_animate and (
            has_border_effects
            or has_text_effects
            or has_typing_glow
            or has_combo_notice
            or has_fireworks
        )
        should_run = should_run or (
            self._animations_started and not self.boss_mode and has_spellcheck_effects
        )
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

    def _combo_frame_delay(self) -> float | None:
        notice = self._combo_notices.get(self.active_alias)
        if notice is None:
            return None
        remaining = notice.started_at + 3.6 - time.monotonic()
        return min(0.1, remaining) if remaining > 0 else None

    def _firework_frame_delay(self) -> float | None:
        now = time.monotonic()
        if not any(
            particle.world == self.active_alias and now - particle.started_at < 2.2
            for particle in self._firework_particles
        ):
            return None
        return 1 / 20

    def _spellcheck_frame_delay(self) -> float | None:
        now = time.monotonic()
        animated = self.animations_enabled and not self.low_bandwidth
        delays = (
            self.active_view.display.transient_style_frame_delay(now, animated=animated),
            self.active_view.transient_style_frame_delay(now, animated=animated),
        )
        return min((delay for delay in delays if delay is not None), default=None)

    def _typing_glow_frame_delay(self) -> float | None:
        if not self.typing_glow.enabled or not self.animations_enabled or self.low_bandwidth:
            return None
        return self.active_view.typing_glow_tracker.frame_delay(
            time.monotonic(), self.typing_glow.duration_seconds
        )

    async def _animate_ui(self) -> None:
        while True:
            elapsed_seconds = self._animation_elapsed_seconds()
            can_animate = self.animations_enabled and not self.low_bandwidth
            border_delay = (
                self.plugins.border_frame_delay(elapsed_seconds)
                if can_animate and self.plugins is not None
                else None
            )
            text_delay = self._text_frame_delay(elapsed_seconds) if can_animate else None
            spellcheck_delay = self._spellcheck_frame_delay()
            typing_glow_delay = self._typing_glow_frame_delay() if can_animate else None
            combo_delay = self._combo_frame_delay() if can_animate else None
            firework_delay = self._firework_frame_delay() if can_animate else None
            delays = [
                delay
                for delay in (
                    border_delay,
                    text_delay,
                    spellcheck_delay,
                    typing_glow_delay,
                    combo_delay,
                    firework_delay,
                )
                if delay is not None
            ]
            delay = min(delays, default=None)
            if delay is None or self.boss_mode:
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
            self._accept_combo_events = False
            for event in self.initial_events:
                self.handle_event(event)
            self._accept_combo_events = True
            for alias, text in self._startup_notices:
                self.add_notice(alias, text)
            self._startup_notices.clear()
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
            if self.plugin_update_checker is not None and self.plugin_update_checker.enabled:
                self._spawn(
                    self.plugin_update_checker.run_periodically(
                        lambda result: self._show_plugin_update_status(
                            (result,), available_only=True
                        )
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
        plugin_source_notices = getattr(runtime, "plugin_source_notices", ())
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
            mouse_mode=bundle.main.ui.mouse_mode,
            spellcheck=bundle.main.ui.spellcheck,
            typing_glow=getattr(bundle.main.ui, "typing_glow", TypingGlowConfig()),
            animations_enabled=bundle.main.ui.animations_enabled,
            low_bandwidth=bundle.main.ui.low_bandwidth,
            output_color=bundle.main.ui.output_color,
            theme=bundle.main.ui.theme,
            screen_clear_mode=bundle.main.ui.screen_clear.mode,
            screen_clear_effect=bundle.main.ui.screen_clear.effect,
            boss_screen_mode=bundle.main.ui.boss.mode,
            boss_screen=bundle.main.ui.boss.screen,
            plugins=runtime.plugins,
            agents=runtime.agents,
            service_runtime=runtime,
            update_checker=UpdateChecker(bundle.main.updates),
            plugin_update_checker=PluginUpdateChecker(
                bundle.main.plugins.sources,
                plugins_directory=bundle.main.plugins.state_directory,
                config=bundle.main.updates,
                notified_versions={
                    notice.source_id: notice.available_version
                    for notice in plugin_source_notices
                    if notice.available_version is not None
                },
            ),
        )
        for message in getattr(runtime, "plugin_source_messages", ()):
            tui.queue_startup_notice(tui.active_alias, message)
    except BaseException:
        await runtime.stop()
        raise
    result = await tui.run()
    if getattr(tui, "update_restart_requested", False):
        from tfr.installations import managed_restart_command

        restart = managed_restart_command(sys.argv[1:])
        if restart is None:
            raise RuntimeError("activated TFR release cannot be restarted")
        os.execv(restart[0], restart)
    return result
