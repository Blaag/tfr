from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING

import regex

from tfr.urls import find_urls

if TYPE_CHECKING:
    from symspellpy import SymSpell

_SPEECH_PREFIX = re.compile(r'^(?:"|:|(?:say|pose)\s+)', re.IGNORECASE)
_WORD = regex.compile(r"(?<![\p{L}\p{N}_])\p{L}+(?:['’]\p{L}+)*(?![\p{L}\p{N}_])")
_MINIMUM_FREQUENCY = 1_000_000
_GENERAL_FREQUENCY_RATIO = 10
_TRANSPOSITION_FREQUENCY_RATIO = 2

# These are protected vocabulary, not a filter. Correctly spelled profanity and
# common informal variants must pass through exactly as typed.
_BUILTIN_PROTECTED_WORDS = frozenset(
    {
        "ass",
        "asshole",
        "bastard",
        "bitch",
        "bullshit",
        "cock",
        "crap",
        "cunt",
        "damn",
        "dick",
        "fuck",
        "fucking",
        "fuk",
        "hell",
        "mush",
        "mushes",
        "mux",
        "nospoof",
        "piss",
        "shit",
        "tfr",
        "tinymush",
        "tinymux",
    }
)


@dataclass(frozen=True, slots=True)
class Correction:
    original: str
    replacement: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    text: str
    corrections: tuple[Correction, ...] = ()


def _is_adjacent_transposition(source: str, target: str) -> bool:
    if len(source) != len(target):
        return False
    differences = [
        index
        for index, pair in enumerate(zip(source, target, strict=True))
        if pair[0] != pair[1]
    ]
    if len(differences) != 2 or differences[1] != differences[0] + 1:
        return False
    first, second = differences
    return source[first] == target[second] and source[second] == target[first]


def _is_embedded_token(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    if before in "@/_\\-" or after in "@/_\\-":
        return True
    if before == "." and start >= 2 and text[start - 2].isalnum():
        return True
    return after == "." and end + 1 < len(text) and text[end + 1].isalnum()


def _transfer_casing(source: str, replacement: str) -> str:
    if source.isupper():
        return replacement.upper()
    if source.istitle():
        return replacement[:1].upper() + replacement[1:]
    return replacement


class LocalSpellChecker:
    def __init__(self) -> None:
        self._checker: SymSpell | None = None

    def _load(self) -> SymSpell:
        if self._checker is None:
            from symspellpy import SymSpell

            checker = SymSpell(max_dictionary_edit_distance=1)
            dictionary = files("symspellpy").joinpath("frequency_dictionary_en_82_765.txt")
            if not checker.load_dictionary(dictionary, term_index=0, count_index=1):
                raise RuntimeError("could not load the bundled English spelling dictionary")
            self._checker = checker
        return self._checker

    def correct(
        self,
        text: str,
        *,
        protected_words: set[str] | frozenset[str] = frozenset(),
    ) -> CorrectionResult:
        if "\n" in text or "\r" in text:
            return CorrectionResult(text)
        prefix = _SPEECH_PREFIX.match(text)
        if prefix is None or prefix.end() == len(text):
            return CorrectionResult(text)

        checker = self._load()
        from symspellpy import Verbosity

        configured_protected = {word.casefold() for word in protected_words if word}
        for phrase in protected_words:
            configured_protected.update(
                match.group().casefold() for match in _WORD.finditer(phrase)
            )
        protected = _BUILTIN_PROTECTED_WORDS | configured_protected
        urls = tuple((start, end) for start, end, _url in find_urls(text))
        output: list[str] = []
        corrections: list[Correction] = []
        cursor = 0
        output_length = 0
        first_payload_word = True
        for match in _WORD.finditer(text, pos=prefix.end()):
            start, end = match.span()
            word = match.group()
            folded = word.casefold()
            protected_token = (
                folded in protected
                or not word.isascii()
                or len(word) < 3
                or word.isupper()
                or (word != word.lower() and word != word.title())
                or (word.istitle() and not first_payload_word)
                or _is_embedded_token(text, start, end)
                or any(start < url_end and end > url_start for url_start, url_end in urls)
            )
            first_payload_word = False
            if protected_token:
                continue

            suggestions = checker.lookup(
                folded,
                Verbosity.ALL,
                max_edit_distance=1,
                include_unknown=False,
            )
            if any(suggestion.distance == 0 for suggestion in suggestions):
                continue
            candidates = [suggestion for suggestion in suggestions if suggestion.distance == 1]
            if not candidates:
                continue
            best = candidates[0]
            second_count = candidates[1].count if len(candidates) > 1 else 1
            ratio = (
                _TRANSPOSITION_FREQUENCY_RATIO
                if _is_adjacent_transposition(folded, best.term.casefold())
                else _GENERAL_FREQUENCY_RATIO
            )
            if word.istitle() and not _is_adjacent_transposition(
                folded, best.term.casefold()
            ):
                continue
            if best.count < _MINIMUM_FREQUENCY or best.count < second_count * ratio:
                continue
            replacement = _transfer_casing(word, best.term)

            unchanged = text[cursor:start]
            output.append(unchanged)
            output_length += len(unchanged)
            replacement_start = output_length
            output.append(replacement)
            replacement_end = replacement_start + len(replacement)
            output_length = replacement_end
            corrections.append(
                Correction(
                    original=word,
                    replacement=replacement,
                    start=replacement_start,
                    end=replacement_end,
                )
            )
            cursor = end

        if not corrections:
            return CorrectionResult(text)
        output.append(text[cursor:])
        return CorrectionResult("".join(output), tuple(corrections))
