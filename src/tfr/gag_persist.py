"""Surgical, comment-preserving insertion of a gag pattern into worlds.jsonc.

The gateway edits the user's hand-maintained JSONC configuration only on an
explicit ``/gag ... --persist``. The file contains comments, deliberate
formatting, and world passwords, so this never re-serialises it. Instead it
locates the target world's object by brace matching and inserts the pattern
into (or creates) its ``gag.patterns`` array, writing a timestamped backup
first and verifying the result re-parses with the pattern present.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import jsonc


class GagPersistError(ValueError):
    """Raised when a gag pattern cannot be safely persisted."""


def _find_world_object_span(text: str, world: str) -> tuple[int, int]:
    """Return (open_brace_index, close_brace_index) of the world's object."""
    key = json.dumps(world)  # exact JSON-quoted key, handles escaping
    search_start = 0
    while True:
        key_index = text.find(key, search_start)
        if key_index == -1:
            raise GagPersistError(f"world {world!r} not found in configuration")
        after = key_index + len(key)
        rest = text[after:]
        colon_offset = rest.find(":")
        if colon_offset == -1:
            search_start = after
            continue
        # The first non-space after the colon must be an opening brace for the
        # world object; otherwise this was a different occurrence of the string.
        brace_rel = after + colon_offset + 1
        cursor = brace_rel
        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1
        if cursor < len(text) and text[cursor] == "{":
            open_brace = cursor
            close_brace = _matching_brace(text, open_brace)
            return open_brace, close_brace
        search_start = after


def _matching_brace(text: str, open_index: int) -> int:
    """Return the index of the brace matching the one at open_index.

    String literals (and their escapes) are skipped so braces inside values do
    not confuse the match. Line and block comments are also skipped.
    """
    depth = 0
    i = open_index
    in_string = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            newline = text.find("\n", i)
            i = len(text) if newline == -1 else newline
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise GagPersistError("unbalanced braces while locating world object")


def _detect_indent(text: str, open_brace: int) -> str:
    """Infer the indentation used for keys inside the world object."""
    newline = text.find("\n", open_brace)
    if newline == -1:
        return "      "
    line_start = newline + 1
    cursor = line_start
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    indent = text[line_start:cursor]
    return indent or "      "


def persist_gag_pattern(worlds_path: Path | str, world: str, pattern: str) -> bool:
    """Insert ``pattern`` into the world's ``gag.patterns`` array in worlds.jsonc.

    Returns True if the file was changed, False if the pattern was already
    persisted for that world. Raises :class:`GagPersistError` on any failure,
    leaving the original file untouched.
    """
    path = Path(worlds_path).expanduser()
    original = path.read_text(encoding="utf-8")
    original_mode = stat.S_IMODE(path.stat().st_mode)

    # Validate the current file parses before we touch it.
    try:
        parsed = jsonc.loads(original)
    except ValueError as exc:
        raise GagPersistError(f"worlds configuration is not valid JSONC: {exc}") from exc
    worlds = parsed.get("worlds") if isinstance(parsed, dict) else None
    if not isinstance(worlds, dict) or world not in worlds:
        raise GagPersistError(f"world {world!r} not found in configuration")

    # No-op if already persisted.
    existing = worlds[world].get("gag") if isinstance(worlds[world], dict) else None
    if isinstance(existing, dict):
        patterns = existing.get("patterns")
        if isinstance(patterns, list) and pattern in patterns:
            return False

    open_brace, close_brace = _find_world_object_span(original, world)
    world_body = original[open_brace : close_brace + 1]
    indent = _detect_indent(original, open_brace)
    encoded = json.dumps(pattern)

    gag_key_index = world_body.find('"gag"')
    if gag_key_index != -1:
        # Find the patterns array inside the existing gag object.
        patterns_index = world_body.find('"patterns"', gag_key_index)
        if patterns_index == -1:
            raise GagPersistError("existing gag block has no patterns array")
        open_bracket = world_body.find("[", patterns_index)
        if open_bracket == -1:
            raise GagPersistError("gag patterns array is malformed")
        close_bracket = world_body.find("]", open_bracket)
        if close_bracket == -1:
            raise GagPersistError("gag patterns array is not closed")
        inner = world_body[open_bracket + 1 : close_bracket]
        if inner.strip():
            # Append after the last element, preserving a trailing comma style.
            insertion = f"{inner.rstrip()}"
            if not insertion.rstrip().endswith(","):
                insertion += ","
            insertion += f" {encoded},"
            new_body = (
                world_body[: open_bracket + 1]
                + insertion
                + world_body[close_bracket:]
            )
        else:
            new_body = (
                world_body[: open_bracket + 1]
                + encoded
                + world_body[close_bracket:]
            )
    else:
        # Insert a new gag block right after the world's opening brace.
        block = (
            f"\n{indent}\"gag\": {{\n"
            f"{indent}  \"patterns\": [{encoded}],\n"
            f"{indent}}},"
        )
        new_body = world_body[:1] + block + world_body[1:]

    new_text = original[:open_brace] + new_body + original[close_brace + 1 :]

    # Verify the result parses and the pattern is now present for this world.
    try:
        reparsed = jsonc.loads(new_text)
    except ValueError as exc:
        raise GagPersistError(f"edit produced invalid JSONC: {exc}") from exc
    result_patterns = (
        reparsed.get("worlds", {}).get(world, {}).get("gag", {}).get("patterns", [])
    )
    if pattern not in result_patterns:
        raise GagPersistError("edit did not add the pattern as expected")

    # Timestamped backup, then atomic replace.
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-gag-{timestamp}")
    backup.write_text(original, encoding="utf-8")
    os.chmod(backup, original_mode)
    temporary = path.with_name(f"{path.name}.tmp-gag-{timestamp}")
    temporary.write_text(new_text, encoding="utf-8")
    os.chmod(temporary, original_mode)
    temporary.replace(path)
    return True
