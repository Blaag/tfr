from __future__ import annotations

import re
from dataclasses import dataclass

from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text

_SAFE_SGR = re.compile(r"^(?:\x1b\[|\x9b)[0-9;:]{0,64}m$")


@dataclass(frozen=True, slots=True)
class AnsiToken:
    text: str
    start: int
    end: int
    control: bool


@dataclass(frozen=True, slots=True)
class AnsiProjection:
    raw: str
    plain: str
    tokens: tuple[AnsiToken, ...]
    raw_boundaries: tuple[int, ...]

    def raw_boundary(self, visible_offset: int) -> int:
        if not 0 <= visible_offset < len(self.raw_boundaries):
            raise ValueError("visible offset is outside the projected text")
        return self.raw_boundaries[visible_offset]

    def remove_visible_prefix(self, visible_characters: int) -> str:
        if not 0 <= visible_characters <= len(self.plain):
            raise ValueError("visible prefix length is outside the projected text")
        visible_offset = 0
        output: list[str] = []
        for token in self.tokens:
            if token.control:
                output.append(token.text)
            else:
                if visible_offset >= visible_characters:
                    output.append(token.text)
                visible_offset += 1
        return "".join(output)


def _terminated_control_end(text: str, start: int) -> int:
    index = start
    while index < len(text):
        if text[index] == "\x07" or text[index] == "\x9c":
            return index + 1
        if text[index] == "\x1b" and index + 1 < len(text) and text[index + 1] == "\\":
            return index + 2
        index += 1
    return len(text)


def _csi_end(text: str, start: int) -> int:
    index = start
    while index < len(text):
        if 0x40 <= ord(text[index]) <= 0x7E:
            return index + 1
        index += 1
    return len(text)


def _control_end(text: str, start: int) -> int | None:
    character = text[start]
    if character == "\x1b":
        if start + 1 >= len(text):
            return len(text)
        introducer = text[start + 1]
        if introducer == "[":
            return _csi_end(text, start + 2)
        if introducer in "]PX^_":
            return _terminated_control_end(text, start + 2)
        return min(start + 2, len(text))
    if character == "\x9b":
        return _csi_end(text, start + 1)
    if character in "\x90\x98\x9d\x9e\x9f":
        return _terminated_control_end(text, start + 1)
    if character == "\x9c":
        return start + 1
    return None


def project_ansi(text: str) -> AnsiProjection:
    tokens: list[AnsiToken] = []
    plain: list[str] = []
    raw_boundaries = [0]
    index = 0
    while index < len(text):
        control_end = _control_end(text, index)
        if control_end is not None:
            tokens.append(AnsiToken(text[index:control_end], index, control_end, True))
            raw_boundaries[-1] = control_end
            index = control_end
            continue

        end = index + 1
        tokens.append(AnsiToken(text[index:end], index, end, False))
        plain.append(text[index:end])
        raw_boundaries.append(end)
        index = end

    return AnsiProjection(text, "".join(plain), tuple(tokens), tuple(raw_boundaries))


def strip_ansi(text: str) -> str:
    return project_ansi(text).plain


def safe_ansi_formatted_text(text: str) -> StyleAndTextTuples:
    safe_raw: list[str] = []
    for token in project_ansi(text).tokens:
        if token.control:
            if _SAFE_SGR.fullmatch(token.text):
                safe_raw.append(token.text)
            continue
        safe_raw.extend(
            character
            for character in token.text
            if character in "\r\n\t"
            or (ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F)
        )

    output: StyleAndTextTuples = []
    for fragment in to_formatted_text(ANSI("".join(safe_raw))):
        style, fragment_text = fragment[:2]
        if style == "[ZeroWidthEscape]":
            continue
        if fragment_text:
            output.append((style, fragment_text))
    return output


def terminal_plain_text(text: str) -> str:
    return "".join(fragment_text for _style, fragment_text in safe_ansi_formatted_text(text))
