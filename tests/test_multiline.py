import pytest

from tfr.multiline import prepare_multiline_text


def prepare(text: str, *, server: str = "tinymux", encoding: str = "utf-8"):
    return prepare_multiline_text(
        text,
        server=server,
        encoding=encoding,
        source="test transfer",
        maximum_source_bytes=32,
        maximum_lines=4,
        maximum_command_bytes=32,
    )


def test_multiline_normalizes_newlines_and_removes_one_terminal_empty_line() -> None:
    prepared = prepare("one\r\ntwo\rthree\n")

    assert prepared.commands == ("@emit one", "@emit two", "@emit three")
    assert prepared.line_count == 3
    assert prepared.source_bytes == len(b"one\ntwo\nthree")


def test_multiline_preserves_empty_lines_whitespace_and_escaping() -> None:
    assert prepare(" one\t\n\n", server="tinymush").commands == (
        "@emit %bone%t",
        "@emit ",
    )
    assert prepare("a;b[c]{d}\\%#,(x)", server="bare").commands == (
        "@emit a;b[c]{d}\\%#,(x)",
    )


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("one\x00two", "NUL"),
        ("123456789012345678901234567890123", "byte limit"),
        ("1\n2\n3\n4\n5", "line limit"),
    ],
)
def test_multiline_rejects_invalid_or_oversized_source(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        prepare(text)


def test_multiline_rejects_unsupported_encoding_and_encoded_command_size() -> None:
    with pytest.raises(ValueError, match="cannot be encoded"):
        prepare("snowman ☃", encoding="ascii")
    with pytest.raises(ValueError, match="command limit"):
        prepare("x" * 27)
    with pytest.raises(ValueError, match="bare, tinymush, or tinymux"):
        prepare("one\ntwo", server="generic")
