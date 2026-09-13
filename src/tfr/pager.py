from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.utils import get_cwidth

from tfr.ansi import safe_ansi_formatted_text, terminal_plain_text
from tfr.text_effects import TextDecoration, TextEffectKind
from tfr.urls import find_urls


class PagerMode(StrEnum):
    FOLLOW = "follow"
    PAUSED = "paused"
    SCROLLED = "scrolled"


@dataclass(slots=True)
class PagerState:
    height: int
    overlap: int = 1
    enabled: bool = True
    total_rows: int = 0
    visible_end: int = 0
    top_row: int = 0
    mode: PagerMode = PagerMode.FOLLOW
    page_budget: int = 0

    def __post_init__(self) -> None:
        if self.height <= 0:
            raise ValueError("pager height must be positive")
        if self.overlap < 0:
            raise ValueError("pager overlap cannot be negative")
        self.page_budget = self.height

    @property
    def page_size(self) -> int:
        return max(1, self.height - min(self.overlap, self.height - 1))

    @property
    def more_rows(self) -> int:
        if self.mode is PagerMode.PAUSED:
            return max(0, self.total_rows - self.visible_end)
        if self.mode is PagerMode.SCROLLED:
            return max(0, self.total_rows - (self.top_row + self.height))
        return 0

    @property
    def visible_range(self) -> tuple[int, int]:
        end = min(self.total_rows, self.top_row + self.height)
        if self.mode is PagerMode.PAUSED:
            end = min(end, self.visible_end)
        return self.top_row, end

    def append_rows(self, count: int) -> None:
        if count < 0:
            raise ValueError("appended row count cannot be negative")
        if count == 0:
            return
        self.total_rows += count
        if not self.enabled:
            self.jump_to_end()
            return
        if self.mode is not PagerMode.FOLLOW:
            return

        shown = min(count, self.page_budget)
        self.visible_end += shown
        self.page_budget -= shown
        self.top_row = max(0, self.visible_end - self.height)
        if shown < count:
            self.mode = PagerMode.PAUSED

    def advance(self) -> None:
        if self.mode is PagerMode.SCROLLED:
            self.scroll_rows(self.page_size)
            return
        if self.mode is not PagerMode.PAUSED:
            return

        self.visible_end = min(self.total_rows, self.visible_end + self.page_size)
        self.top_row = max(0, self.visible_end - self.height)
        if self.visible_end == self.total_rows:
            self.mode = PagerMode.FOLLOW
            self.page_budget = self.page_size

    def scroll_rows(self, amount: int) -> None:
        maximum_top = max(0, self.total_rows - self.height)
        self.top_row = min(maximum_top, max(0, self.top_row + amount))
        if self.top_row == maximum_top:
            self.mode = PagerMode.FOLLOW
            self.visible_end = self.total_rows
            self.page_budget = self.page_size
        else:
            self.mode = PagerMode.SCROLLED

    def jump_to_end(self) -> None:
        self.visible_end = self.total_rows
        self.top_row = max(0, self.total_rows - self.height)
        self.mode = PagerMode.FOLLOW
        self.page_budget = self.height if self.total_rows == 0 else self.page_size

    def trim_rows(self, count: int) -> None:
        if count < 0:
            raise ValueError("trimmed row count cannot be negative")
        self.total_rows = max(0, self.total_rows - count)
        self.visible_end = max(0, self.visible_end - count)
        self.top_row = max(0, self.top_row - count)
        if self.total_rows == 0 or (
            self.mode is PagerMode.PAUSED and self.visible_end >= self.total_rows
        ):
            self.jump_to_end()
        elif self.mode is PagerMode.SCROLLED:
            self.top_row = min(self.top_row, max(0, self.total_rows - self.height))

    def resize(self, height: int) -> None:
        if height <= 0:
            raise ValueError("pager height must be positive")
        self.height = height
        if self.mode is PagerMode.FOLLOW:
            self.jump_to_end()
        elif self.mode is PagerMode.PAUSED:
            self.top_row = max(0, self.visible_end - self.height)
        else:
            self.top_row = min(self.top_row, max(0, self.total_rows - self.height))
        self._clamp()

    def reflow(self, total_rows: int) -> None:
        if total_rows < 0:
            raise ValueError("total row count cannot be negative")
        old_total = self.total_rows
        if self.mode is PagerMode.FOLLOW or old_total == 0:
            self.total_rows = total_rows
            self.jump_to_end()
            return

        visible_ratio = self.visible_end / old_total
        top_ratio = self.top_row / old_total
        self.total_rows = total_rows
        self.visible_end = min(total_rows, round(total_rows * visible_ratio))
        self.top_row = min(
            max(0, total_rows - self.height),
            round(total_rows * top_ratio),
        )
        if self.mode is PagerMode.PAUSED:
            self.top_row = max(0, self.visible_end - self.height)
            if self.visible_end >= self.total_rows:
                self.jump_to_end()
        self._clamp()

    def _clamp(self) -> None:
        self.visible_end = min(max(0, self.visible_end), self.total_rows)
        self.top_row = min(
            max(0, self.top_row),
            max(0, self.total_rows - self.height),
        )


FormattedRow = tuple[tuple[str, str], ...]


def _append_fragment(row: list[tuple[str, str]], style: str, text: str) -> None:
    if row and row[-1][0] == style:
        previous_style, previous_text = row[-1]
        row[-1] = (previous_style, previous_text + text)
    else:
        row.append((style, text))


def wrap_ansi_text(
    text: str,
    width: int,
    *,
    default_style: str = "",
    decorations: tuple[TextDecoration, ...] = (),
    elapsed_seconds: float = 0.0,
    animations_enabled: bool = False,
    url_spans: tuple[tuple[int, int], ...] = (),
    row_offsets: list[int] | None = None,
) -> tuple[FormattedRow, ...]:
    if width <= 0:
        raise ValueError("display width must be positive")
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith(("\r", "\n")):
        text = text[:-1]

    rows: list[list[tuple[str, str]]] = [[]]
    column = 0
    visible_offset = 0
    if row_offsets is not None:
        row_offsets.append(0)
    fragments = safe_ansi_formatted_text(text)
    for fragment in fragments:
        style, fragment_text = fragment[:2]
        for character in fragment_text:
            if character == "\r":
                visible_offset += 1
                continue
            if character == "\n":
                rows.append([])
                column = 0
                visible_offset += 1
                if row_offsets is not None:
                    row_offsets.append(visible_offset)
                continue
            rendered_style = f"{default_style} {style}".strip()
            rendered_character = character
            ordered_decorations = sorted(
                decorations,
                key=lambda item: item.effect is TextEffectKind.TERMINAL_REVEAL,
            )
            for decoration in ordered_decorations:
                if decoration.start <= visible_offset < decoration.end:
                    effect_style, rendered_character = decoration.render_character(
                        rendered_character,
                        visible_offset - decoration.start,
                        elapsed_seconds,
                        animations_enabled,
                        rendered_style,
                    )
                    if effect_style:
                        rendered_style = f"{rendered_style} {effect_style}".strip()
            if any(start <= visible_offset < end for start, end in url_spans):
                rendered_style = f"{rendered_style} underline".strip()
            if character == "\t":
                cell_width = 8 - (column % 8)
                remaining = cell_width
                while remaining:
                    available = width - column
                    if available == 0:
                        rows.append([])
                        column = 0
                        available = width
                        if row_offsets is not None:
                            row_offsets.append(visible_offset)
                    chunk_width = min(remaining, available)
                    _append_fragment(rows[-1], rendered_style, " " * chunk_width)
                    column += chunk_width
                    remaining -= chunk_width
                visible_offset += 1
                continue
            else:
                cell_width = max(0, get_cwidth(rendered_character))
                if cell_width > width:
                    rendered_character = (
                        " " if rendered_character.isspace() else "\N{REPLACEMENT CHARACTER}"
                    )
                    cell_width = 1
                rendered = rendered_character
            if cell_width > 0 and column > 0 and column + cell_width > width:
                rows.append([])
                column = 0
                if row_offsets is not None:
                    row_offsets.append(visible_offset)
            _append_fragment(rows[-1], rendered_style, rendered)
            column += cell_width
            visible_offset += 1
    return tuple(tuple(row) for row in rows)


def rows_to_formatted_text(rows: tuple[FormattedRow, ...]) -> StyleAndTextTuples:
    output: StyleAndTextTuples = []
    for index, row in enumerate(rows):
        output.extend(row)
        if index + 1 < len(rows):
            output.append(("", "\n"))
    return output


class DisplayBuffer:
    def __init__(
        self,
        *,
        max_rows: int,
        width: int = 80,
        height: int = 24,
        pager_enabled: bool = True,
        pager_overlap: int = 1,
        default_style: str = "",
    ) -> None:
        if max_rows <= 0:
            raise ValueError("display buffer size must be positive")
        self.max_rows = max_rows
        self.width = width
        self.default_style = default_style
        self.entries: list[str] = []
        self._entry_rows: list[list[FormattedRow]] = []
        self._entry_row_offsets: list[int] = []
        self._entry_decorations: list[tuple[TextDecoration, ...]] = []
        self._entry_recallable: list[bool] = []
        self._entry_urls: list[tuple[tuple[int, int, str], ...]] = []
        self._entry_row_starts: list[list[int]] = []
        self.rows: list[FormattedRow] = []
        self._screen_start_entry = 0
        self.pager = PagerState(
            height=height,
            overlap=pager_overlap,
            enabled=pager_enabled,
        )

    def append(
        self,
        text: str,
        *,
        decorations: tuple[TextDecoration, ...] = (),
        recallable: bool = True,
    ) -> None:
        urls = find_urls(terminal_plain_text(text))
        row_starts: list[int] = []
        new_rows = list(
            wrap_ansi_text(
                text,
                self.width,
                default_style=self.default_style,
                decorations=decorations,
                url_spans=tuple((start, end) for start, end, _url in urls),
                row_offsets=row_starts,
            )
        )
        self.entries.append(text)
        self._entry_rows.append(new_rows)
        self._entry_row_offsets.append(0)
        self._entry_decorations.append(decorations)
        self._entry_recallable.append(recallable)
        self._entry_urls.append(urls)
        self._entry_row_starts.append(row_starts)
        self.rows.extend(new_rows)
        self.pager.append_rows(len(new_rows))
        self._trim_rows()

    def _trim_rows(self) -> None:
        trim_count = max(0, len(self.rows) - self.max_rows)
        remaining = trim_count
        removed_entries = 0
        while self._entry_rows and remaining >= len(self._entry_rows[0]):
            remaining -= len(self._entry_rows.pop(0))
            self.entries.pop(0)
            self._entry_row_offsets.pop(0)
            self._entry_decorations.pop(0)
            self._entry_recallable.pop(0)
            self._entry_urls.pop(0)
            self._entry_row_starts.pop(0)
            removed_entries += 1
        self._screen_start_entry = max(0, self._screen_start_entry - removed_entries)
        if remaining:
            self._entry_rows[0] = self._entry_rows[0][remaining:]
            self._entry_row_offsets[0] += remaining
            self._entry_row_starts[0] = self._entry_row_starts[0][remaining:]
        if trim_count:
            del self.rows[:trim_count]
            self.pager.trim_rows(trim_count)

    def resize(self, *, width: int, height: int) -> None:
        if width <= 0:
            raise ValueError("display width must be positive")
        if height <= 0:
            raise ValueError("display height must be positive")
        if width != self.width:
            self.width = width
            row_starts_by_entry: list[list[int]] = []
            entry_rows = []
            for entry, decorations, urls in zip(
                self.entries,
                self._entry_decorations,
                self._entry_urls,
                strict=True,
            ):
                row_starts: list[int] = []
                entry_rows.append(
                    list(
                        wrap_ansi_text(
                            entry,
                            width,
                            default_style=self.default_style,
                            decorations=decorations,
                            url_spans=tuple((start, end) for start, end, _url in urls),
                            row_offsets=row_starts,
                        )
                    )
                )
                row_starts_by_entry.append(row_starts)
            self._entry_rows = entry_rows
            self._entry_row_starts = row_starts_by_entry
            self._entry_row_offsets = [0] * len(self.entries)
            self.rows = [row for entry in self._entry_rows for row in entry]
            self.pager.reflow(len(self.rows))
            self._trim_rows()
        self.pager.resize(height)

    def recent_rows(self, count: int) -> tuple[FormattedRow, ...]:
        if count <= 0:
            raise ValueError("recall row count must be positive")
        selected: list[FormattedRow] = []
        remaining = count
        for rows, recallable in reversed(
            tuple(zip(self._entry_rows, self._entry_recallable, strict=True))
        ):
            if not recallable:
                continue
            chunk = rows[-remaining:]
            selected[0:0] = chunk
            remaining -= len(chunk)
            if remaining == 0:
                break
        return tuple(selected)

    def _visible_bounds(self) -> tuple[int, int]:
        start, end = self.pager.visible_range
        if self.pager.mode is not PagerMode.SCROLLED:
            if self._screen_start_entry <= len(self._entry_rows) // 2:
                screen_start = sum(
                    len(rows) for rows in self._entry_rows[: self._screen_start_entry]
                )
            else:
                screen_start = len(self.rows) - sum(
                    len(rows) for rows in self._entry_rows[self._screen_start_entry :]
                )
            start = max(start, screen_start)
        return start, end

    def _visible_entries(self, start: int, end: int) -> list[tuple[int, int]]:
        visible: list[tuple[int, int]] = []
        if start >= end:
            return visible
        if start <= len(self.rows) - end:
            row_start = 0
            for index, rows in enumerate(self._entry_rows):
                row_end = row_start + len(rows)
                if row_end > start and row_start < end:
                    visible.append((index, row_start))
                row_start = row_end
                if row_start >= end:
                    break
            return visible

        row_end = len(self.rows)
        for index in range(len(self._entry_rows) - 1, -1, -1):
            rows = self._entry_rows[index]
            row_start = row_end - len(rows)
            if row_end <= start:
                break
            if row_start < end:
                visible.append((index, row_start))
            row_end = row_start
        visible.reverse()
        return visible

    def visible_rows(
        self,
        *,
        elapsed_seconds: float | None = None,
        animations_enabled: bool = False,
    ) -> tuple[FormattedRow, ...]:
        start, end = self._visible_bounds()
        if elapsed_seconds is None or not any(self._entry_decorations):
            return tuple(self.rows[start:end])
        visible: list[FormattedRow] = []
        for index, row_start in self._visible_entries(start, end):
            entry = self.entries[index]
            base_rows = self._entry_rows[index]
            offset = self._entry_row_offsets[index]
            decorations = self._entry_decorations[index]
            rows = (
                list(
                    wrap_ansi_text(
                        entry,
                        self.width,
                        default_style=self.default_style,
                        decorations=decorations,
                        elapsed_seconds=elapsed_seconds,
                        animations_enabled=animations_enabled,
                        url_spans=tuple(
                            (url_start, url_end)
                            for url_start, url_end, _url in self._entry_urls[index]
                        ),
                    )
                )[offset:]
                if decorations
                else base_rows
            )
            visible.extend(rows[max(0, start - row_start) : end - row_start])
        return tuple(visible)

    def padded_visible_rows(
        self,
        *,
        elapsed_seconds: float | None = None,
        animations_enabled: bool = False,
    ) -> tuple[FormattedRow, ...]:
        """Like :meth:`visible_rows`, but padded to the pane's full height.

        When there are fewer buffered rows than the pane is tall (for
        example, right after a screen clear or a small ``/recall``), blank
        rows are added *above* the real content so it stays anchored to the
        pane's bottom edge, matching a normal terminal, instead of leaving
        blank space below newly displayed text.
        """
        rows = self.visible_rows(
            elapsed_seconds=elapsed_seconds,
            animations_enabled=animations_enabled,
        )
        pad_count = self.pager.height - len(rows)
        if pad_count <= 0:
            return rows
        return ((),) * pad_count + rows

    def animation_frame_delay(self, elapsed_seconds: float) -> float | None:
        start, end = self._visible_bounds()
        delays: list[float] = []
        for index, _row_start in self._visible_entries(start, end):
            delays.extend(
                delay
                for decoration in self._entry_decorations[index]
                if (delay := decoration.frame_delay(elapsed_seconds)) is not None
            )
        return min(delays, default=None)

    def url_at(self, row: int, column: int) -> str | None:
        """Return the URL under a visible ``(row, column)``, if any.

        ``row`` and ``column`` use the same coordinates as the rows
        returned by :meth:`visible_rows`: ``row`` is an index into the
        currently visible rows, and ``column`` is a character offset into
        that row's plain text.
        """
        start, end = self._visible_bounds()
        absolute_row = start + row
        if not start <= absolute_row < end:
            return None
        for index, row_start in self._visible_entries(start, end):
            local_row = absolute_row - row_start
            if not 0 <= local_row < len(self._entry_rows[index]):
                continue
            row_starts = self._entry_row_starts[index]
            if local_row >= len(row_starts):
                return None
            target_offset = row_starts[local_row] + max(0, column)
            for url_start, url_end, url in self._entry_urls[index]:
                if url_start <= target_offset < url_end:
                    return url
            return None
        return None

    @property
    def screen_is_cleared(self) -> bool:
        return self._screen_start_entry > 0

    def clear_screen(self) -> None:
        self._screen_start_entry = len(self.entries)
        self.pager.jump_to_end()
        self.pager.page_budget = self.pager.height

    def restore_scrollback(self) -> None:
        self._screen_start_entry = 0

    def formatted_text(self) -> StyleAndTextTuples:
        return rows_to_formatted_text(self.visible_rows())
