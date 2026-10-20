from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tfr.world_text import escape_world_text

SUPPORTED_EMIT_SERVERS = frozenset({"bare", "tinymush", "tinymux"})


@dataclass(frozen=True, slots=True)
class PreparedMultiline:
    commands: tuple[str, ...]
    line_count: int
    source_bytes: int


def prepare_emit_lines(
    lines: Sequence[str],
    *,
    server: str,
    encoding: str,
    source: str,
    maximum_source_bytes: int,
    maximum_lines: int,
    maximum_command_bytes: int,
) -> PreparedMultiline:
    if server not in SUPPORTED_EMIT_SERVERS:
        raise ValueError(f"{source} requires a bare, tinymush, or tinymux world")
    text = "\n".join(lines)
    if "\x00" in text:
        raise ValueError(f"{source} contains a NUL character")
    source_bytes = len(text.encode("utf-8"))
    if source_bytes > maximum_source_bytes:
        raise ValueError(f"{source} exceeds the {maximum_source_bytes} byte limit")
    if len(lines) > maximum_lines:
        raise ValueError(f"{source} exceeds the {maximum_lines} line limit")
    commands = tuple(f"@emit {escape_world_text(line, server)}" for line in lines)
    for line_number, command in enumerate(commands, start=1):
        try:
            command_size = len(command.encode(encoding, errors="strict"))
        except (LookupError, UnicodeEncodeError) as exc:
            raise ValueError(
                f"{source} line {line_number} cannot be encoded as {encoding}"
            ) from exc
        if command_size > maximum_command_bytes:
            raise ValueError(
                f"{source} line {line_number} exceeds the "
                f"{maximum_command_bytes} byte command limit"
            )
    return PreparedMultiline(
        commands=commands,
        line_count=len(commands),
        source_bytes=source_bytes,
    )


def prepare_multiline_text(
    text: str,
    *,
    server: str,
    encoding: str,
    source: str,
    maximum_source_bytes: int,
    maximum_lines: int,
    maximum_command_bytes: int,
) -> PreparedMultiline:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return prepare_emit_lines(
        lines,
        server=server,
        encoding=encoding,
        source=source,
        maximum_source_bytes=maximum_source_bytes,
        maximum_lines=maximum_lines,
        maximum_command_bytes=maximum_command_bytes,
    )
