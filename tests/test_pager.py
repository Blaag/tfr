from __future__ import annotations

from prompt_toolkit.formatted_text import fragment_list_to_text

from tfr.ansi import terminal_plain_text
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


def test_url_spans_are_underlined_without_altering_the_text() -> None:
    text = "see http://example.com now"
    start, end = text.index("http://"), text.index(" now")

    rows = wrap_ansi_text(text, 40, url_spans=((start, end),))

    assert fragment_list_to_text(list(rows[0])) == text
    assert "underline" in rows[0][1][0]
    assert "underline" not in rows[0][0][0]
    assert "underline" not in rows[0][2][0]


def test_url_underline_is_combined_with_the_default_style() -> None:
    text = "http://example.com"

    rows = wrap_ansi_text(text, 40, default_style="fg:#d7d7d7", url_spans=((0, len(text)),))

    assert rows[0][0] == ("fg:#d7d7d7 underline", text)


def test_url_span_survives_a_hard_wrap_mid_url() -> None:
    text = "http://example.com"

    rows = wrap_ansi_text(text, 10, url_spans=((0, len(text)),))

    assert len(rows) == 2
    assert fragment_list_to_text(list(rows[0])) + fragment_list_to_text(list(rows[1])) == text
    assert all("underline" in style for row in rows for style, _text in row)


def test_row_offsets_are_populated_at_each_wrap_boundary() -> None:
    offsets: list[int] = []

    rows = wrap_ansi_text("abcdefghij", 4, row_offsets=offsets)

    assert len(rows) == 3
    assert offsets == [0, 4, 8]


def test_row_offsets_account_for_newlines_and_carriage_returns() -> None:
    offsets: list[int] = []

    rows = wrap_ansi_text("ab\r\ncd\nef", 40, row_offsets=offsets)

    assert [fragment_list_to_text(list(row)) for row in rows] == ["ab", "cd", "ef"]
    assert offsets == [0, 4, 7]


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


def test_padded_visible_rows_pads_short_content_above_not_below() -> None:
    display = DisplayBuffer(max_rows=10, width=20, height=5, pager_enabled=False)
    display.append("first")
    display.append("second")

    padded = display.padded_visible_rows()

    assert len(padded) == 5
    assert [fragment_list_to_text(list(row)) for row in padded] == [
        "",
        "",
        "",
        "first",
        "second",
    ]


def test_padded_visible_rows_is_unchanged_once_content_fills_the_pane() -> None:
    display = DisplayBuffer(max_rows=10, width=20, height=2, pager_enabled=False)
    display.append("first")
    display.append("second")

    assert display.padded_visible_rows() == display.visible_rows()


def test_padded_visible_rows_pads_fully_when_the_buffer_is_empty() -> None:
    display = DisplayBuffer(max_rows=10, width=20, height=3, pager_enabled=False)

    padded = display.padded_visible_rows()

    assert padded == ((), (), ())


def test_url_at_resolves_a_click_within_a_detected_url() -> None:
    text = "see http://example.com now"
    url = "http://example.com"
    start = text.index(url)
    display = DisplayBuffer(max_rows=10, width=40, height=5, pager_enabled=False)
    display.append(text)

    assert display.url_at(0, start) == url
    assert display.url_at(0, start + 1) == url
    assert display.url_at(0, start + len(url) - 1) == url


def test_url_at_returns_none_outside_any_url_span() -> None:
    text = "see http://example.com now"
    url = "http://example.com"
    start = text.index(url)
    display = DisplayBuffer(max_rows=10, width=40, height=5, pager_enabled=False)
    display.append(text)

    assert display.url_at(0, 0) is None
    assert display.url_at(0, start - 1) is None
    assert display.url_at(0, start + len(url)) is None
    assert display.url_at(0, len(text) - 1) is None


def test_url_at_returns_none_when_no_urls_are_present() -> None:
    display = DisplayBuffer(max_rows=10, width=40, height=5, pager_enabled=False)
    display.append("just plain chat text")

    assert display.url_at(0, 5) is None


def test_url_at_resolves_a_url_wrapped_across_rows() -> None:
    display = DisplayBuffer(max_rows=10, width=10, height=5, pager_enabled=False)
    display.append("http://example.com")

    assert [fragment_list_to_text(list(row)) for row in display.rows] == [
        "http://exa",
        "mple.com",
    ]
    assert display.url_at(0, 0) == "http://example.com"
    assert display.url_at(1, 3) == "http://example.com"


def test_url_at_remains_correct_after_scrollback_trimming() -> None:
    display = DisplayBuffer(max_rows=1, width=40, height=5, pager_enabled=False)
    display.append("http://first.example")
    display.append("http://second.example")

    assert display.entries == ["http://second.example"]
    assert display.url_at(0, 5) == "http://second.example"


def test_url_at_remains_correct_after_a_partial_row_trim() -> None:
    # max_rows=2 forces trimming exactly one row off the *front* of the
    # first (wrapped-into-two-rows) entry, rather than removing it whole.
    display = DisplayBuffer(max_rows=2, width=10, height=5, pager_enabled=False)
    display.append("http://example.com")
    display.append("hi")

    assert [fragment_list_to_text(list(row)) for row in display.rows] == ["mple.com", "hi"]
    assert display.url_at(0, 0) == "http://example.com"


def test_url_at_remains_correct_after_a_width_resize() -> None:
    display = DisplayBuffer(max_rows=10, width=40, height=5, pager_enabled=False)
    display.append("see http://example.com now")

    display.resize(width=10, height=5)

    assert len(display.rows) > 1
    found = {display.url_at(row, 0) for row in range(len(display.rows))}
    assert found == {None, "http://example.com"}


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


def test_url_underline_survives_the_per_frame_decoration_rewrap() -> None:
    # visible_rows() re-wraps entries that carry an active decoration fresh
    # on every frame (for animation), which previously dropped the URL
    # underline for any line a plugin also decorated (e.g. a speaker
    # effect on the sender's name, or a broadly-applied effect like
    # terminal_reveal).
    text = "Alice says, check http://example.com now"
    url = "http://example.com"
    decoration = TextDecoration(
        start=0,
        end=5,
        effect=TextEffectKind.CAPITALIZATION_ROLL,
        base_color="#a9914a",
        accent_color="#e6c965",
        interval_seconds=0.5,
    )
    display = DisplayBuffer(max_rows=10, width=60, height=5, pager_enabled=False)
    display.append(text, decorations=(decoration,))

    rows = display.visible_rows(elapsed_seconds=0.1, animations_enabled=True)

    underlined = "".join(
        fragment_text for row in rows for style, fragment_text in row if "underline" in style
    )
    assert underlined == url


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


def test_recent_entries_preserve_decorations_and_clip_partial_wrapped_entries() -> None:
    display = DisplayBuffer(max_rows=20, width=5, height=3, pager_enabled=False)
    decoration = TextDecoration(
        start=3,
        end=8,
        effect=TextEffectKind.SHIMMER,
        base_color="#d70000",
        accent_color="#ffffff",
        interval_seconds=1.4,
    )
    display.append("abcdefghij", decorations=(decoration,))

    full = display.recent_entries(2)
    partial = display.recent_entries(1)

    assert full == (("abcdefghij", (decoration,)),)
    assert partial[0][0] == "fghij"
    assert [(item.start, item.end) for item in partial[0][1]] == [(0, 3)]


def test_partial_recent_entry_preserves_ansi_and_source_offsets() -> None:
    display = DisplayBuffer(max_rows=20, width=5, height=3, pager_enabled=False)
    decoration = TextDecoration(
        start=4,
        end=7,
        effect=TextEffectKind.SHIMMER,
        base_color="#d70000",
        accent_color="#ffffff",
        interval_seconds=1.4,
    )
    display.append("\x1b[31mabc\tdef\x1b[0m", decorations=(decoration,))

    partial = display.recent_entries(2)

    assert terminal_plain_text(partial[0][0]) == "\tdef"
    assert "\x1b[31m" in partial[0][0]
    assert [(item.start, item.end) for item in partial[0][1]] == [(1, 4)]


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
