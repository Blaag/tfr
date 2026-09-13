from __future__ import annotations

import pytest

from tfr.urls import find_urls


def test_finds_a_simple_http_and_https_url() -> None:
    text = "see http://example.com and https://example.org/path?q=1 now"

    spans = find_urls(text)

    assert [text[start:end] for start, end, _url in spans] == [
        "http://example.com",
        "https://example.org/path?q=1",
    ]
    assert [url for _start, _end, url in spans] == [
        "http://example.com",
        "https://example.org/path?q=1",
    ]


def test_offsets_point_back_into_the_original_text() -> None:
    text = "prefix https://example.com/page suffix"

    (start, end, url) = find_urls(text)[0]

    assert text[start:end] == url
    assert text[:start] == "prefix "
    assert text[end:] == " suffix"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("visit https://example.com.", "https://example.com"),
        ("visit https://example.com, thanks", "https://example.com"),
        ("visit https://example.com!", "https://example.com"),
        ("visit https://example.com?", "https://example.com"),
        ('"https://example.com"', "https://example.com"),
        ("'https://example.com'", "https://example.com"),
    ],
)
def test_trims_trailing_sentence_punctuation(text: str, expected: str) -> None:
    ((_start, _end, url),) = find_urls(text)

    assert url == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(see https://example.com)", "https://example.com"),
        ("[see https://example.com]", "https://example.com"),
        ("https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)"),
        ("(https://en.wikipedia.org/wiki/Foo_(bar))", "https://en.wikipedia.org/wiki/Foo_(bar)"),
    ],
)
def test_trims_unbalanced_trailing_brackets_but_keeps_balanced_ones(
    text: str, expected: str
) -> None:
    ((_start, _end, url),) = find_urls(text)

    assert url == expected


def test_no_urls_returns_an_empty_tuple() -> None:
    assert find_urls("just plain text, no links here") == ()


def test_ignores_urls_missing_the_scheme() -> None:
    assert find_urls("visit example.com or www.example.com") == ()


def test_matching_is_case_insensitive_on_the_scheme() -> None:
    ((_start, _end, url),) = find_urls("HTTPS://EXAMPLE.COM/Path")

    assert url == "HTTPS://EXAMPLE.COM/Path"


def test_multiple_urls_on_one_line_each_get_their_own_span() -> None:
    text = "https://a.example/one https://b.example/two"

    spans = find_urls(text)

    assert len(spans) == 2
    assert text[spans[0][0] : spans[0][1]] == "https://a.example/one"
    assert text[spans[1][0] : spans[1][1]] == "https://b.example/two"


def test_a_url_that_is_entirely_punctuation_after_trimming_is_dropped() -> None:
    assert find_urls("http://.") == ()
