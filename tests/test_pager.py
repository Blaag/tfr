from __future__ import annotations

from prompt_toolkit.formatted_text import fragment_list_to_text

from tfr.pager import DisplayBuffer, PagerMode, PagerState, wrap_ansi_text
from tfr.text_effects import TextDecoration, TextEffectKind, validate_decorations


def test_pager_stops_after_first_screen_and_advances_with_overlap() -> None:
    pager = PagerState(height=4, overlap=1)

    pager.append_rows(10)

    assert pager.mode is PagerMode.PAUSED
    assert pager.visible_range == (0, 4)
    assert pager.more_rows == 6

    pager.advance()
    assert pager.visible_range == (3, 7)
    assert pager.more_rows == 3

    pager.advance()
    assert pager.mode is PagerMode.FOLLOW
    assert pager.visible_range == (6, 10)
    assert pager.more_rows == 0


def test_steady_output_consumes_one_page_budget_before_pausing() -> None:
    pager = PagerState(height=3, overlap=0)

    pager.append_rows(1)
    pager.append_rows(1)
    pager.append_rows(1)
    assert pager.mode is PagerMode.FOLLOW

    pager.append_rows(1)
    assert pager.mode is PagerMode.PAUSED
    assert pager.more_rows == 1
    assert pager.visible_range == (0, 3)


def test_manual_scrollback_does_not_follow_new_output() -> None:
    pager = PagerState(height=4, enabled=False)
    pager.append_rows(10)
    pager.enabled = True

    pager.scroll_rows(-3)
    assert pager.mode is PagerMode.SCROLLED
    assert pager.visible_range == (3, 7)

    pager.append_rows(2)
    assert pager.visible_range == (3, 7)
    assert pager.more_rows == 5

    pager.jump_to_end()
    assert pager.visible_range == (8, 12)
    assert pager.mode is PagerMode.FOLLOW


def test_trim_and_reflow_keep_pager_in_valid_range() -> None:
    pager = PagerState(height=4)
    pager.append_rows(10)
    pager.trim_rows(2)
    pager.reflow(5)
    pager.resize(3)

    start, end = pager.visible_range
    assert 0 <= start <= end <= pager.total_rows == 5
    assert 0 <= pager.visible_end <= pager.total_rows


def test_wrap_counts_wide_and_combining_characters() -> None:
    rows = wrap_ansi_text("A\N{COMBINING ACUTE ACCENT}\N{CJK UNIFIED IDEOGRAPH-754C}B", 3)

    assert len(rows) == 2
    assert (
        fragment_list_to_text(list(rows[0]))
        == "A\N{COMBINING ACUTE ACCENT}\N{CJK UNIFIED IDEOGRAPH-754C}"
    )
    assert fragment_list_to_text(list(rows[1])) == "B"


def test_wrap_preserves_ansi_styles() -> None:
    rows = wrap_ansi_text("\x1b[31mred\x1b[0m plain", 20)

    assert fragment_list_to_text(list(rows[0])) == "red plain"
    assert rows[0][0][0] == "ansired"


def test_wrap_applies_a_default_output_color_beneath_ansi_styles() -> None:
    rows = wrap_ansi_text(
        "plain \x1b[31mred",
        20,
        default_style="fg:#d7d7d7",
    )

    assert rows == (
        (
            ("fg:#d7d7d7", "plain "),
            ("fg:#d7d7d7 ansired", "red"),
        ),
    )


def test_wrap_sanitizes_raw_zero_width_terminal_escapes() -> None:
    rows = wrap_ansi_text("A\x01\x1b]52;c;injected\x07\x02B", 20)

    assert fragment_list_to_text(list(rows[0])) == "AB"


def test_tab_expansion_respects_narrow_row_widths() -> None:
    rows = wrap_ansi_text("\tX", 3)

    assert [fragment_list_to_text(list(row)) for row in rows] == ["   ", "   ", "  X"]


def test_wide_glyph_is_replaced_when_it_cannot_fit_the_row() -> None:
    rows = wrap_ansi_text("界", 1)

    assert fragment_list_to_text(list(rows[0])) == "\N{REPLACEMENT CHARACTER}"


def test_display_buffer_bounds_rendered_rows_and_reflows() -> None:
    display = DisplayBuffer(max_rows=3, width=10, height=5, pager_enabled=False)
    display.append("first")
    display.append("second line")
    display.append("third")

    assert display.entries == ["second line", "third"]
    assert fragment_list_to_text(display.formatted_text()) == "second lin\ne\nthird"

    display.resize(width=4, height=2)
    assert len(display.rows) == 3

    display.resize(width=20, height=2)
    assert len(display.rows) == 2
    assert display.pager.visible_range == (0, 2)


def test_recent_rows_excludes_previous_recall_output() -> None:
    display = DisplayBuffer(max_rows=10, width=20, height=5, pager_enabled=False)
    display.append("first\nsecond")
    display.append("-- Recall 1\nsecond", recallable=False)
    display.append("third")

    rows = display.recent_rows(3)

    assert [fragment_list_to_text(list(row)) for row in rows] == ["first", "second", "third"]


def test_recent_rows_remains_aligned_after_scrollback_trimming() -> None:
    display = DisplayBuffer(max_rows=2, width=20, height=5, pager_enabled=False)
    display.append("first")
    display.append("second")
    display.append("recall copy", recallable=False)

    rows = display.recent_rows(5)

    assert [fragment_list_to_text(list(row)) for row in rows] == ["second"]


def test_long_running_display_stays_within_rendered_scrollback_bound() -> None:
    display = DisplayBuffer(max_rows=100, width=80, height=24, pager_enabled=False)

    for index in range(10_000):
        display.append(f"line {index}")

    assert len(display.rows) == 100
    assert len(display.entries) == 100
    assert fragment_list_to_text(display.formatted_text()).endswith("line 9999")


def test_clear_starts_fresh_screen_without_deleting_scrollback() -> None:
    display = DisplayBuffer(max_rows=100, width=80, height=3, pager_enabled=True)
    display.append("old one\nold two")

    display.clear_screen()
    assert display.entries == ["old one\nold two"]
    assert fragment_list_to_text(display.formatted_text()) == ""

    display.append("new one\nnew two\nnew three")
    assert fragment_list_to_text(display.formatted_text()) == "new one\nnew two\nnew three"
    display.restore_scrollback()
    assert fragment_list_to_text(display.formatted_text()) == "new one\nnew two\nnew three"

    display.pager.scroll_rows(-display.pager.page_size)
    assert fragment_list_to_text(display.formatted_text()) == "old one\nold two\nnew one"


def test_decorations_render_after_ansi_parsing_without_changing_wrapping() -> None:
    decoration = TextDecoration(
        start=0,
        end=5,
        effect=TextEffectKind.CAPITALIZATION_ROLL,
        base_color="#a9914a",
        accent_color="#e6c965",
        interval_seconds=0.5,
    )

    rows = wrap_ansi_text(
        "\x1b[1mAlice\x1b[0m says, hello",
        8,
        decorations=(decoration,),
        elapsed_seconds=0.6,
        animations_enabled=True,
    )

    assert fragment_list_to_text([fragment for row in rows for fragment in row]) == (
        "aLice says, hello"
    )
    assert len(rows) == 3
    assert all("bold" in style for style, _text in rows[0] if "fg:#" in style)
    assert any("fg:#e6c965" in style for style, _text in rows[0])


def test_display_decorations_are_static_when_animation_is_disabled() -> None:
    display = DisplayBuffer(max_rows=20, width=20, height=5, pager_enabled=False)
    display.append(
        "aLiCe says, hello",
        decorations=(
            TextDecoration(
                start=0,
                end=5,
                effect=TextEffectKind.CAPITALIZATION_ROLL,
                base_color="#a9914a",
                accent_color="#e6c965",
                interval_seconds=0.5,
            ),
        ),
    )

    rows = display.visible_rows(elapsed_seconds=1.0, animations_enabled=False)

    assert fragment_list_to_text(list(rows[0])) == "aLiCe says, hello"
    assert rows[0][0] == ("fg:#a9914a", "aLiCe")


def test_display_decoration_runs_once_then_waits_for_its_next_cycle() -> None:
    display = DisplayBuffer(max_rows=20, width=20, height=5, pager_enabled=False)
    decoration = TextDecoration(
        start=0,
        end=5,
        effect=TextEffectKind.CAPITALIZATION_ROLL,
        base_color="#a9914a",
        accent_color="#e6c965",
        interval_seconds=0.5,
        repeat_seconds=10,
    )
    display.append("Alice says, hello", decorations=(decoration,))

    active = display.visible_rows(elapsed_seconds=0.6, animations_enabled=True)
    waiting = display.visible_rows(elapsed_seconds=3.0, animations_enabled=True)
    repeated = display.visible_rows(elapsed_seconds=10.6, animations_enabled=True)

    assert fragment_list_to_text(list(active[0])).startswith("aLice")
    assert fragment_list_to_text(list(waiting[0])).startswith("Alice")
    assert fragment_list_to_text(list(repeated[0])).startswith("aLice")
    assert decoration.frame_delay(3.0) == 7.0


def test_display_schedules_only_decorations_in_visible_rows() -> None:
    display = DisplayBuffer(max_rows=20, width=20, height=1, pager_enabled=False)
    display.append(
        "Carol says, hello",
        decorations=(
            TextDecoration(
                start=0,
                end=5,
                effect=TextEffectKind.SHIMMER,
                base_color="#d70000",
                accent_color="#ffffff",
                interval_seconds=1.4,
            ),
        ),
    )
    display.append("ordinary output")

    assert display.animation_frame_delay(0.0) is None

    display.pager.scroll_rows(-1)
    assert display.animation_frame_delay(0.0) == 0.05


def test_terminal_reveal_preserves_wrapping_and_composes_with_speaker_effects() -> None:
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
        effect_width=2,
        glitch_characters="#",
    )

    validate_decorations(11, (speaker, reveal))
    original = wrap_ansi_text("hello world", 5)
    animated = wrap_ansi_text(
        "hello world",
        5,
        decorations=(reveal, speaker),
        elapsed_seconds=0.35,
        animations_enabled=True,
    )

    assert len(animated) == len(original)
    assert [sum(len(text) for _style, text in row) for row in animated] == [5, 5, 1]
    flattened = [fragment for row in animated for fragment in row]
    assert fragment_list_to_text(flattened) == "H##        "
    assert "fg:#e6c965" in animated[0][0][0]


def test_terminal_reveal_keeps_a_hidden_wide_glyph_blank_at_one_column() -> None:
    reveal = TextDecoration(
        start=0,
        end=1,
        effect=TextEffectKind.TERMINAL_REVEAL,
        base_color="#d7ff5f",
        accent_color="#d7ff5f",
        interval_seconds=0.1,
        loop=False,
    )

    rows = wrap_ansi_text(
        "界",
        1,
        decorations=(reveal,),
        elapsed_seconds=0.0,
        animations_enabled=True,
    )

    assert rows == ((("", " "),),)
