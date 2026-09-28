from __future__ import annotations

from tfr.ansi import (
    ansi_visible_text,
    browser_text_spans,
    project_ansi,
    safe_ansi_formatted_text,
    strip_ansi,
    terminal_plain_text,
)


def test_projects_csi_styles_without_losing_raw_boundaries() -> None:
    raw = "\x1b[31m[Name]\x1b[0m body"

    projection = project_ansi(raw)

    assert projection.plain == "[Name] body"
    assert projection.raw_boundary(len("[Name] ")) == raw.index("body")
    assert projection.remove_visible_prefix(len("[Name] ")) == "\x1b[31m\x1b[0mbody"


def test_prefix_removal_retains_message_style_sequences() -> None:
    raw = "[Name] \x1b[32mbody\x1b[0m"

    display = project_ansi(raw).remove_visible_prefix(len("[Name] "))

    assert display == "\x1b[32mbody\x1b[0m"
    assert strip_ansi(display) == "body"


def test_visible_slice_preserves_ansi_state_and_removes_framing() -> None:
    raw = "\x1b[36m[prefix]\x1b[0m \x1b[31mmessage\x1b[0m\r\n"
    projection = project_ansi(raw)

    sliced = projection.visible_slice(len("[prefix] "), len("[prefix] message"))

    assert strip_ansi(sliced) == "message"
    assert browser_text_spans(sliced)[0].foreground == "#aa0000"


def test_extracts_exact_visible_text_with_ansi_and_without_framing() -> None:
    raw = "\x1b[36m[prefix]\x1b[0m \x1b[1;31mmessage\x1b[0m\r\n"

    extracted = ansi_visible_text(raw, "message")

    assert extracted is not None
    assert terminal_plain_text(extracted) == "message"
    span = browser_text_spans(extracted)[0]
    assert span.foreground == "#aa0000"
    assert span.bold is True


def test_strips_osc_and_c1_control_sequences() -> None:
    raw = "\x1b]0;title\x07hello\x9b31m red\x9b0m"

    assert strip_ansi(raw) == "hello red"


def test_incomplete_control_sequence_is_not_displayed_as_text() -> None:
    assert strip_ansi("hello\x1b[31") == "hello"


def test_terminal_projection_drops_raw_zero_width_escapes_and_controls() -> None:
    text = "A\x01\x1b]52;c;injected\x07\x02B\x07"

    assert terminal_plain_text(text) == "AB"
    assert safe_ansi_formatted_text(text) == [("", "A"), ("", "B")]


def test_browser_spans_allowlist_sgr_styles_and_reconstruct_visible_text() -> None:
    raw = "plain \x1b[1;3;4;38;2;1;2;3;44mstyled\x1b[0m end"

    spans = browser_text_spans(raw)

    assert "".join(span.text for span in spans) == terminal_plain_text(raw)
    styled = next(span for span in spans if span.text == "styled")
    assert styled.foreground == "#010203"
    assert styled.background == "#0000aa"
    assert styled.bold is True
    assert styled.italic is True
    assert styled.underline is True


def test_browser_spans_discard_unsupported_controls_and_attributes() -> None:
    raw = (
        "<b>safe</b>"
        "\x1b]8;;https://evil.example\x07linked\x1b]8;;\x07"
        "\x1b[7;8;5mvisible\x1b[0m"
        "\x1b[2J"
        "雪👩🏽‍💻"
        "\x1b[31"
    )

    spans = browser_text_spans(raw)

    assert "".join(span.text for span in spans) == "<b>safe</b>linkedvisible雪👩🏽‍💻"
    assert all(
        span.foreground is None
        and span.background is None
        and not span.bold
        and not span.italic
        and not span.underline
        for span in spans
    )
