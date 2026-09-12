from __future__ import annotations

from tfr.ansi import project_ansi, safe_ansi_formatted_text, strip_ansi, terminal_plain_text


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


def test_strips_osc_and_c1_control_sequences() -> None:
    raw = "\x1b]0;title\x07hello\x9b31m red\x9b0m"

    assert strip_ansi(raw) == "hello red"


def test_incomplete_control_sequence_is_not_displayed_as_text() -> None:
    assert strip_ansi("hello\x1b[31") == "hello"


def test_terminal_projection_drops_raw_zero_width_escapes_and_controls() -> None:
    text = "A\x01\x1b]52;c;injected\x07\x02B\x07"

    assert terminal_plain_text(text) == "AB"
    assert safe_ansi_formatted_text(text) == [("", "A"), ("", "B")]
