from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.utils import get_cwidth

MAX_SCREEN_CLEAR_DURATION_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ScreenClearContext:
    lines: tuple[str, ...]
    width: int
    progress: float
    world: str = ""
    seed: int = 0
    styled_lines: tuple[tuple[tuple[str, str], ...], ...] = ()
    elapsed_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError("screen-clear width must be positive")
        if not math.isfinite(self.progress) or not 0 <= self.progress <= 1:
            raise ValueError("screen-clear progress must be between 0 and 1")
        if self.seed < 0:
            raise ValueError("screen-clear seed cannot be negative")
        if self.elapsed_seconds is not None and (
            not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0
        ):
            raise ValueError("screen-clear elapsed time must be finite and non-negative")
        if self.styled_lines:
            if len(self.styled_lines) != len(self.lines):
                raise ValueError("screen-clear styled lines must match the snapshot height")
            for line, fragments in zip(self.lines, self.styled_lines, strict=True):
                if any(
                    not isinstance(fragment, tuple)
                    or len(fragment) != 2
                    or not all(isinstance(value, str) for value in fragment)
                    for fragment in fragments
                ):
                    raise ValueError("screen-clear styled lines must contain style and text pairs")
                if "".join(text for _style, text in fragments) != line:
                    raise ValueError("screen-clear styled lines must match the snapshot text")


class ScreenClearEffect(Protocol):
    def __call__(self, context: ScreenClearContext) -> Iterable[tuple[str, str]]: ...


def terminal_cell_width(text: str) -> int:
    return get_cwidth(text)


def screen_clear_fragment_limit(context: ScreenClearContext) -> int:
    return max(1, len(context.lines) * context.width * 2)


def validate_screen_clear_frame(
    context: ScreenClearContext,
    fragments: Sequence[tuple[str, str]],
) -> StyleAndTextTuples:
    maximum_fragments = screen_clear_fragment_limit(context)
    if len(fragments) > maximum_fragments:
        raise ValueError("screen-clear frame contains too many fragments")
    result: StyleAndTextTuples = []
    text_characters = 0
    maximum_text_characters = max(1, len(context.lines) * (context.width + 1) * 4)
    for fragment in fragments:
        if not isinstance(fragment, tuple) or len(fragment) != 2:
            raise ValueError("screen-clear fragments must be style and text pairs")
        style, text = fragment
        if not isinstance(style, str) or not isinstance(text, str):
            raise ValueError("screen-clear fragment style and text must be strings")
        if len(style) > 256:
            raise ValueError("screen-clear fragment style is too long")
        text_characters += len(text)
        if text_characters > maximum_text_characters:
            raise ValueError("screen-clear frame contains too much text")
        if any(
            (ord(character) < 0x20 and character != "\n") or 0x7F <= ord(character) <= 0x9F
            for character in text
        ):
            raise ValueError("screen-clear frame contains control characters")
        result.append((style, text))

    lines = "".join(text for _style, text in result).split("\n")
    if len(lines) > max(1, len(context.lines)):
        raise ValueError("screen-clear frame exceeds the snapshot height")
    if any(get_cwidth(line) > context.width for line in lines):
        raise ValueError("screen-clear frame exceeds the snapshot width")
    return result
