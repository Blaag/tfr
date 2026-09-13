from __future__ import annotations

import re

_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_STRIP = ".,;:!?\"'"
_BRACKET_PAIRS = ((")", "("), ("]", "["))


def find_urls(text: str) -> tuple[tuple[int, int, str], ...]:
    """Find HTTP(S) URLs in plain text.

    Returns ``(start, end, url)`` tuples with character offsets into
    ``text``. Likely trailing sentence punctuation (a period, comma, or
    closing quote that ends a sentence rather than the URL) and unbalanced
    closing brackets are trimmed from the end of each match, so a URL
    written as ``(see https://example.com/page)`` or ``https://example.com.``
    does not swallow the surrounding punctuation.
    """
    spans: list[tuple[int, int, str]] = []
    for match in _URL_PATTERN.finditer(text):
        end = match.end()
        url = match.group()
        changed = True
        while changed:
            changed = False
            while url and url[-1] in _TRAILING_STRIP:
                url = url[:-1]
                end -= 1
                changed = True
            for close, open_ in _BRACKET_PAIRS:
                while url.endswith(close) and url.count(open_) < url.count(close):
                    url = url[:-1]
                    end -= 1
                    changed = True
        if url:
            scheme_end = url.index("://") + 3
            if any(character.isalnum() for character in url[scheme_end:]):
                spans.append((match.start(), end, url))
    return tuple(spans)
