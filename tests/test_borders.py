from __future__ import annotations

import pytest

from tfr.borders import BorderEdge, BorderFragment, border_cell, validate_border_fragment


def test_border_cells_form_a_clockwise_perimeter() -> None:
    width = 5
    inner_height = 2
    cells = [
        border_cell(
            panel="output",
            world="alpha",
            edge=edge,
            edge_index=index,
            width=width,
            inner_height=inner_height,
            focused=True,
            elapsed_seconds=0.5,
        )
        for edge, length in (
            (BorderEdge.TOP, width),
            (BorderEdge.RIGHT, inner_height),
            (BorderEdge.BOTTOM, width),
            (BorderEdge.LEFT, inner_height),
        )
        for index in range(length)
    ]

    assert sorted(context.perimeter_index for context, _fragment in cells) == list(range(14))
    assert "".join(fragment.character for _context, fragment in cells[:5]) == "┌───┐"
    assert "".join(fragment.character for _context, fragment in cells[7:12]) == "└───┘"
    assert all(context.perimeter_length == 14 for context, _fragment in cells)
    assert all(context.height == 4 for context, _fragment in cells)


@pytest.mark.parametrize("character", ("", "xx", "\n", "界"))
def test_border_fragments_must_occupy_one_safe_terminal_cell(character: str) -> None:
    with pytest.raises(ValueError, match="exactly one cell"):
        validate_border_fragment(BorderFragment(character, ""))


def test_border_fragment_style_must_be_safe_to_render() -> None:
    with pytest.raises(ValueError, match="valid prompt_toolkit style"):
        validate_border_fragment(BorderFragment("─", "fg:not-a-color"))
