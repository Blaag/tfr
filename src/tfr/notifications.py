from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tfr.config import NotificationConfig
from tfr.events import Direction, Event, EventKind

FOCUS_IN_SEQUENCE = "\x1b[I"
FOCUS_OUT_SEQUENCE = "\x1b[O"
ENABLE_FOCUS_REPORTING = "\x1b[?1004h"
DISABLE_FOCUS_REPORTING = "\x1b[?1004l"

_VISIBLE_ACTIVITY = frozenset(
    {
        EventKind.RAW_OUTPUT,
        EventKind.SPEECH,
        EventKind.SAY,
        EventKind.POSE,
        EventKind.PAGE,
        EventKind.CHANNEL,
        EventKind.EMIT,
        EventKind.SYSTEM,
    }
)
_MENTION_KINDS = frozenset({EventKind.SPEECH, EventKind.SAY, EventKind.POSE})
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _safe_text(value: str, *, maximum: int = 180) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())[:maximum]


def _mentions_name(text: str, name: str) -> bool:
    escaped = re.escape(name.strip())
    if not escaped:
        return False
    return re.search(rf"(?<![\w]){escaped}(?![\w])", text, re.IGNORECASE) is not None


@dataclass(frozen=True, slots=True)
class NotificationDecision:
    reason: str
    message: str


class NotificationPolicy:
    """UI-local policy with one notification at most per unfocused interval."""

    def __init__(
        self,
        config: NotificationConfig,
        *,
        characters: Mapping[str, str | None],
    ) -> None:
        self.config = config
        self.characters = dict(characters)
        self.focused: bool | None = None
        self.notified_while_unfocused = False

    def reconfigure(self, config: NotificationConfig) -> None:
        self.config = config

    def focus_in(self) -> None:
        self.focused = True
        self.notified_while_unfocused = False

    def focus_out(self) -> None:
        self.focused = False
        self.notified_while_unfocused = False

    def evaluate(self, event: Event) -> NotificationDecision | None:
        if (
            not self.config.enabled
            or self.focused is not False
            or self.notified_while_unfocused
            or event.direction is not Direction.INBOUND
            or event.redacted
            or event.kind not in _VISIBLE_ACTIVITY
            or event.metadata.get("sensitive") is True
        ):
            return None
        reason: str | None = None
        if self.config.any_activity:
            reason = "activity"
        elif event.world in self.config.worlds:
            reason = "selected-world"
        elif self.config.pages and event.kind is EventKind.PAGE:
            reason = "page"
        elif self.config.mentions and event.kind in _MENTION_KINDS:
            character = self.characters.get(event.world)
            sender = event.provenance.sender_name if event.provenance is not None else None
            if character and sender and sender.casefold() != character.casefold():
                text = event.plain_text or event.canonical_text or ""
                if _mentions_name(text, character):
                    reason = "mention"
        if reason is None:
            return None
        if self.config.preview == "private":
            message = "TFR has activity"
        else:
            labels = {
                "activity": "New activity",
                "selected-world": "New activity",
                "mention": "Your character name was mentioned",
                "page": "Page received",
            }
            message = f"TFR · {_safe_text(event.world, maximum=80)} — {labels[reason]}"
        return NotificationDecision(reason, message)

    def mark_delivered(self) -> None:
        self.notified_while_unfocused = True


class TerminalNotificationTransport:
    def __init__(
        self,
        configured: str,
        *,
        environment: Mapping[str, str] | None = None,
        write_raw: Callable[[str], None],
    ) -> None:
        self.environment = dict(environment or os.environ)
        self.configured = configured
        self.write_raw = write_raw
        self.multiplexer = (
            "herdr"
            if self.environment.get("HERDR_ENV") or self.environment.get("HERDR_PANE_ID")
            else "tmux"
            if self.environment.get("TMUX")
            else "none"
        )
        self.terminal = self._detect_terminal()
        self.resolved = self._resolve()
        self.sent = 0
        self.last_error: str | None = None

    def _detect_terminal(self) -> str:
        if any(
            self.environment.get(name)
            for name in ("GHOSTTY_SURFACE_ID", "GHOSTTY_RESOURCES_DIR", "GHOSTTY_BIN_DIR")
        ):
            return "ghostty"
        return (
            self.environment.get("TERM_PROGRAM")
            or self.environment.get("LC_TERMINAL")
            or self.environment.get("TERM")
            or "unknown"
        )

    def _resolve(self) -> str:
        if self.configured != "auto":
            return self.configured
        if self.multiplexer == "herdr":
            return "herdr"
        terminal = self.terminal.casefold()
        if any(name in terminal for name in ("ghostty", "iterm", "kitty", "wezterm", "warp")):
            return "osc-9"
        return "bell"

    def send(self, message: str) -> bool:
        text = _safe_text(message)
        if not text:
            return False
        try:
            if self.resolved == "herdr":
                executable = shutil.which("herdr")
                if executable is None:
                    raise RuntimeError("herdr executable is unavailable")
                result = subprocess.run(  # noqa: S603
                    [
                        executable,
                        "notification",
                        "show",
                        "TFR",
                        "--body",
                        text,
                        "--sound",
                        "none",
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError("Herdr rejected the notification request")
                try:
                    response = json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Herdr returned an invalid notification response") from exc
                shown = (
                    response.get("result", {}).get("shown") if isinstance(response, dict) else None
                )
                if shown is not True:
                    return False
            elif self.resolved == "bell":
                self.write_raw("\a")
            else:
                sequence = f"\x1b]9;{text}\x07"
                self.write_raw(
                    _tmux_passthrough(sequence) if self.multiplexer == "tmux" else sequence
                )
            self.sent += 1
            self.last_error = None
            return True
        except (OSError, RuntimeError) as exc:
            self.last_error = str(exc)[:200]
            return False

    def status(self, *, focused: bool | None, latched: bool) -> dict[str, object]:
        return {
            "configured": self.configured,
            "resolved": self.resolved,
            "terminal": self.terminal,
            "multiplexer": self.multiplexer,
            "focus": "focused"
            if focused is True
            else "unfocused"
            if focused is False
            else "unknown",
            "latched": latched,
            "sent": self.sent,
            "last_error": self.last_error,
            "herdr_requirement": (
                "Herdr ui.toast.delivery must be terminal or system"
                if self.resolved == "herdr"
                else None
            ),
        }


def _tmux_passthrough(sequence: str) -> str:
    return "\x1bPtmux;" + sequence.replace("\x1b", "\x1b\x1b") + "\x1b\\"
