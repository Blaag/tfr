from __future__ import annotations

import contextlib
import json
import os
import re
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

MAX_DIAGNOSTIC_ENTRIES = 100
MAX_DIAGNOSTIC_MESSAGE_CHARACTERS = 240
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SECRET = re.compile(
    r"(?i)\b(authorization|bearer|password|passwd|secret|token|api[_-]?key)\b"
    r"(\s*[:=]?\s*)([^\s,;]+)"
)
_HOME_PATH = re.compile(r"(?<![A-Za-z0-9])(?:/Users|/home)/[^/\s]+")


def redact_diagnostic_text(value: str) -> str:
    """Remove common credentials and user home names from diagnostic text."""

    redacted = _SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", value)
    return _HOME_PATH.sub("~", redacted)


def bounded_diagnostic_message(value: str) -> str:
    """Create a safe one-line diagnostic message with visible truncation."""

    value = redact_diagnostic_text(value).replace("\r", " ").replace("\n", " ").replace("\x00", "")
    if len(value) <= MAX_DIAGNOSTIC_MESSAGE_CHARACTERS:
        return value
    marker = "… [truncated]"
    return value[: MAX_DIAGNOSTIC_MESSAGE_CHARACTERS - len(marker)] + marker


@dataclass(frozen=True, slots=True)
class DiagnosticEntry:
    timestamp: str
    severity: Literal["info", "warning", "error"]
    component: str
    code: str
    message: str
    operation: str | None = None
    client: Literal["gateway", "native-ui", "pwa"] = "gateway"
    event_id: str | None = None
    device_id_suffix: str | None = None
    shell_version: str | None = None
    ui_name: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


class DiagnosticBuffer:
    def __init__(
        self,
        maximum_entries: int = MAX_DIAGNOSTIC_ENTRIES,
        *,
        path: Path | None = None,
    ) -> None:
        if not 1 <= maximum_entries <= MAX_DIAGNOSTIC_ENTRIES:
            raise ValueError("diagnostic buffer size must be between 1 and 100")
        self._entries: deque[DiagnosticEntry] = deque(maxlen=maximum_entries)
        self.path = path.expanduser() if path is not None else None
        if self.path is not None:
            self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return
        for line in lines[-self._entries.maxlen :]:
            try:
                value = json.loads(line)
                if not isinstance(value, dict) or not isinstance(value.get("message"), str):
                    continue
                value["message"] = bounded_diagnostic_message(value["message"])
                entry = DiagnosticEntry(**value)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                entry.severity in {"info", "warning", "error"}
                and _SAFE_NAME.fullmatch(entry.component)
                and _SAFE_NAME.fullmatch(entry.code)
            ):
                self._entries.append(entry)

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        payload = "".join(
            json.dumps(entry.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
            for entry in self._entries
        )
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temporary, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def record(
        self,
        *,
        severity: Literal["info", "warning", "error"],
        component: str,
        code: str,
        message: str,
        operation: str | None = None,
        client: Literal["gateway", "native-ui", "pwa"] = "gateway",
        event_id: str | None = None,
        device_id_suffix: str | None = None,
        shell_version: str | None = None,
        ui_name: str | None = None,
    ) -> DiagnosticEntry:
        if severity not in {"info", "warning", "error"}:
            raise ValueError("diagnostic severity is invalid")
        for name, value in (("component", component), ("code", code)):
            if not isinstance(value, str) or _SAFE_NAME.fullmatch(value) is None:
                raise ValueError(f"diagnostic {name} is invalid")
        if (
            not isinstance(message, str)
            or not message
            or len(message) > MAX_DIAGNOSTIC_MESSAGE_CHARACTERS
        ):
            raise ValueError("diagnostic message is invalid")
        if any(character in message for character in "\r\n\x00"):
            raise ValueError("diagnostic message must be one bounded line")
        message = redact_diagnostic_text(message)
        for value in (operation, event_id, device_id_suffix, shell_version, ui_name):
            if value is not None and (
                not isinstance(value, str)
                or len(value) > 100
                or any(character in value for character in "\r\n\x00")
            ):
                raise ValueError("diagnostic optional field is invalid")
        entry = DiagnosticEntry(
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            severity=severity,
            component=component,
            code=code,
            message=message,
            operation=operation,
            client=client,
            event_id=event_id,
            device_id_suffix=device_id_suffix,
            shell_version=shell_version,
            ui_name=ui_name,
        )
        self._entries.append(entry)
        self._persist()
        return entry

    def entries(self) -> tuple[DiagnosticEntry, ...]:
        return tuple(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._persist()
