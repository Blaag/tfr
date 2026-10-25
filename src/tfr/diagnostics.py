from __future__ import annotations

import re
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

MAX_DIAGNOSTIC_ENTRIES = 100
MAX_DIAGNOSTIC_MESSAGE_CHARACTERS = 240
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


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
    def __init__(self, maximum_entries: int = MAX_DIAGNOSTIC_ENTRIES) -> None:
        if not 1 <= maximum_entries <= MAX_DIAGNOSTIC_ENTRIES:
            raise ValueError("diagnostic buffer size must be between 1 and 100")
        self._entries: deque[DiagnosticEntry] = deque(maxlen=maximum_entries)

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
        return entry

    def entries(self) -> tuple[DiagnosticEntry, ...]:
        return tuple(self._entries)

    def clear(self) -> None:
        self._entries.clear()
