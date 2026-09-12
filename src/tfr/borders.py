from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

_STYLE_VALIDATOR = Style([])


class BorderEdge(StrEnum):
    TOP = "top"
    RIGHT = "right"
    BOTTOM = "bottom"
    LEFT = "left"


@dataclass(frozen=True, slots=True)
class BorderCellContext:
    panel: str
    world: str
    edge: BorderEdge
    edge_index: int
    perimeter_index: int
    perimeter_length: int
    width: int
    height: int
    focused: bool
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class BorderFragment:
    character: str
    style: str


class BorderEffect(Protocol):
    def __call__(
        self,
        context: BorderCellContext,
        fragment: BorderFragment,
    ) -> BorderFragment | None: ...


def validate_border_fragment(fragment: BorderFragment) -> None:
    if not isinstance(fragment, BorderFragment):
        raise TypeError("border effect must return BorderFragment or None")
    if get_cwidth(fragment.character) != 1 or any(
        character in fragment.character for character in "\r\n\x1b"
    ):
        raise ValueError("border character must occupy exactly one cell")
    if not isinstance(fragment.style, str):
        raise TypeError("border style must be a string")
    try:
        _STYLE_VALIDATOR.get_attrs_for_style_str(fragment.style)
    except (AssertionError, ValueError) as exc:
        raise ValueError("border style must be a valid prompt_toolkit style") from exc


def border_cell(
    *,
    panel: str,
    world: str,
    edge: BorderEdge,
    edge_index: int,
    width: int,
    inner_height: int,
    focused: bool,
    elapsed_seconds: float,
) -> tuple[BorderCellContext, BorderFragment]:
    if width < 1 or inner_height < 1:
        raise ValueError("border dimensions must be positive")
    edge_length = width if edge in {BorderEdge.TOP, BorderEdge.BOTTOM} else inner_height
    if not 0 <= edge_index < edge_length:
        raise ValueError("border edge index is outside the edge")

    perimeter_length = 2 * width + 2 * inner_height
    if edge is BorderEdge.TOP:
        perimeter_index = edge_index
        if width == 1:
            character = "+"
        elif edge_index == 0:
            character = "┌"
        elif edge_index == width - 1:
            character = "┐"
        else:
            character = "─"
    elif edge is BorderEdge.RIGHT:
        perimeter_index = width + edge_index
        character = "│"
    elif edge is BorderEdge.BOTTOM:
        perimeter_index = width + inner_height + (width - 1 - edge_index)
        if width == 1:
            character = "+"
        elif edge_index == 0:
            character = "└"
        elif edge_index == width - 1:
            character = "┘"
        else:
            character = "─"
    else:
        perimeter_index = 2 * width + inner_height + (inner_height - 1 - edge_index)
        character = "│"

    context = BorderCellContext(
        panel=panel,
        world=world,
        edge=edge,
        edge_index=edge_index,
        perimeter_index=perimeter_index,
        perimeter_length=perimeter_length,
        width=width,
        height=inner_height + 2,
        focused=focused,
        elapsed_seconds=elapsed_seconds,
    )
    return context, BorderFragment(character, f"class:border.{panel}")
