from __future__ import annotations

import os
import pty
import select
import shlex
import shutil
import subprocess
import time
from dataclasses import replace
from unittest.mock import Mock
from uuid import UUID

import pytest

from tfr.config import NotificationConfig
from tfr.events import Confidence, Direction, Event, EventKind, Provenance
from tfr.notifications import (
    NotificationPolicy,
    TerminalNotificationTransport,
    _tmux_passthrough,
)


def event(
    text: str,
    *,
    world: str = "alpha",
    kind: EventKind = EventKind.SAY,
    sender: str = "Alice",
    redacted: bool = False,
) -> Event:
    return Event(
        session_id=UUID("11111111-1111-4111-8111-111111111111"),
        world=world,
        connection_generation=1,
        sequence=1,
        direction=Direction.INBOUND,
        kind=kind,
        canonical_text=text,
        plain_text=text,
        display_text=text,
        provenance=Provenance(sender_name=sender, confidence=Confidence.HIGH),
        redacted=redacted,
    )


def test_policy_requires_explicit_focus_out_and_notifies_only_once() -> None:
    policy = NotificationPolicy(
        NotificationConfig(enabled=True, any_activity=True), characters={"alpha": "Barnaby"}
    )

    assert policy.evaluate(event('Alice says, "hello"')) is None
    policy.focus_out()
    first = policy.evaluate(event('Alice says, "hello"'))
    assert first is not None and first.message == "TFR has activity"
    policy.mark_delivered()
    assert policy.evaluate(event('Alice says, "again"')) is None
    policy.focus_in()
    assert policy.evaluate(event('Alice says, "focused"')) is None
    policy.focus_out()
    assert policy.evaluate(event('Alice says, "new interval"')) is not None


def test_policy_supports_selected_world_mentions_and_pages() -> None:
    selected = NotificationPolicy(
        NotificationConfig(enabled=True, worlds=("beta",), mentions=False, pages=False),
        characters={"alpha": "Barnaby", "beta": "Barnaby"},
    )
    selected.focus_out()
    assert selected.evaluate(event("noise", world="alpha")) is None
    assert selected.evaluate(event("noise", world="beta")) is not None

    mention = NotificationPolicy(
        NotificationConfig(enabled=True, mentions=True, pages=False, preview="full"),
        characters={"alpha": "Barnaby"},
    )
    mention.focus_out()
    decision = mention.evaluate(event('Alice says, "Barnaby, hello."'))
    assert decision is not None
    assert decision.reason == "mention"
    assert decision.message == "TFR · alpha — Your character name was mentioned"

    page = NotificationPolicy(
        NotificationConfig(enabled=True, mentions=False, pages=True),
        characters={"alpha": "Barnaby"},
    )
    page.focus_out()
    assert page.evaluate(event("Alice pages: hello", kind=EventKind.PAGE)) is not None


def test_policy_excludes_redacted_outbound_and_partial_name_matches() -> None:
    policy = NotificationPolicy(
        NotificationConfig(enabled=True, mentions=True, pages=False),
        characters={"alpha": "Barnaby"},
    )
    policy.focus_out()
    assert policy.evaluate(event("Alice discusses barnabyard.")) is None
    assert policy.evaluate(event("Barnaby is hidden", redacted=True)) is None
    outbound = event("Barnaby")
    outbound = replace(outbound, direction=Direction.OUTBOUND)
    assert policy.evaluate(outbound) is None


def test_osc9_and_tmux_transport_are_bounded_and_wrapped() -> None:
    written: list[str] = []
    transport = TerminalNotificationTransport(
        "auto",
        environment={
            "TERM_PROGRAM": "tmux",
            "TERM": "tmux-256color",
            "TMUX": "/tmp/tmux",
            "GHOSTTY_SURFACE_ID": "surface-1",
        },
        write_raw=written.append,
    )

    assert transport.send("TFR has\nactivity") is True
    assert transport.resolved == "osc-9"
    assert transport.multiplexer == "tmux"
    assert transport.terminal == "ghostty"
    assert written == [_tmux_passthrough("\x1b]9;TFR has activity\x07")]

    direct: list[str] = []
    direct_transport = TerminalNotificationTransport(
        "osc-9",
        environment={"TERM_PROGRAM": "ghostty"},
        write_raw=direct.append,
    )
    assert direct_transport.send("TFR direct") is True
    assert direct == ["\x1b]9;TFR direct\x07"]


def test_tmux_without_an_outer_terminal_marker_falls_back_to_bell() -> None:
    transport = TerminalNotificationTransport(
        "auto",
        environment={"TERM_PROGRAM": "tmux", "TERM": "tmux-256color", "TMUX": "/tmp/tmux"},
        write_raw=Mock(),
    )

    assert transport.terminal == "tmux"
    assert transport.resolved == "bell"


def test_linux_auto_prefers_ghostty_osc_over_freedesktop(monkeypatch) -> None:
    monkeypatch.setattr(
        "tfr.notifications.shutil.which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    transport = TerminalNotificationTransport(
        "auto",
        environment={
            "TERM_PROGRAM": "ghostty",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        },
        write_raw=Mock(),
        platform_name="linux",
    )

    assert transport.resolved == "osc-9"


def test_linux_auto_uses_freedesktop_for_unknown_local_terminal(monkeypatch) -> None:
    monkeypatch.setattr(
        "tfr.notifications.shutil.which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    run = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("tfr.notifications.subprocess.run", run)
    environment = {
        "TERM_PROGRAM": "unknown-terminal",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "PATH": "/usr/bin",
    }
    transport = TerminalNotificationTransport(
        "auto",
        environment=environment,
        write_raw=Mock(),
        platform_name="linux",
    )

    assert transport.resolved == "freedesktop"
    assert transport.send("TFR has\nactivity") is True
    assert run.call_args.args[0] == [
        "/usr/bin/notify-send",
        "--app-name=TFR",
        "--urgency=normal",
        "--expire-time=8000",
        "TFR has activity",
    ]
    assert run.call_args.kwargs["env"] == environment
    status = transport.status(focused=False, latched=True)
    assert status["session_bus"] is True
    assert status["notify_send"] is True
    assert "unix:path" not in str(status)


def test_linux_system_transport_fails_closed_without_a_session_bus(monkeypatch) -> None:
    monkeypatch.setattr(
        "tfr.notifications.shutil.which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    transport = TerminalNotificationTransport(
        "system",
        environment={"TERM": "xterm-256color"},
        write_raw=Mock(),
        platform_name="linux",
    )

    assert transport.resolved == "freedesktop"
    assert transport.send("TFR has activity") is False
    assert transport.last_error == "the Linux desktop session bus is unavailable"


def test_linux_auto_without_desktop_notification_service_falls_back_to_bell(
    monkeypatch,
) -> None:
    monkeypatch.setattr("tfr.notifications.shutil.which", lambda _name: None)
    written: list[str] = []
    transport = TerminalNotificationTransport(
        "auto",
        environment={"TERM": "xterm-256color"},
        write_raw=written.append,
        platform_name="linux",
    )

    assert transport.resolved == "bell"
    assert transport.send("TFR has activity") is True
    assert written == ["\a"]


def test_herdr_transport_uses_herdr_notification_command(monkeypatch) -> None:
    monkeypatch.setattr("tfr.notifications.shutil.which", lambda _name: "/usr/bin/herdr")
    run = Mock(
        return_value=Mock(
            returncode=0,
            stdout='{"result":{"shown":true,"reason":"shown"}}',
        )
    )
    monkeypatch.setattr("tfr.notifications.subprocess.run", run)
    transport = TerminalNotificationTransport(
        "auto",
        environment={"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"},
        write_raw=Mock(),
    )

    assert transport.send("TFR has activity") is True
    assert transport.resolved == "herdr"
    assert run.call_args.args[0] == [
        "/usr/bin/herdr",
        "notification",
        "show",
        "TFR",
        "--body",
        "TFR has activity",
        "--sound",
        "none",
    ]


def test_herdr_focused_pane_suppression_is_not_reported_as_delivery(monkeypatch) -> None:
    monkeypatch.setattr("tfr.notifications.shutil.which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(
        "tfr.notifications.subprocess.run",
        Mock(return_value=Mock(returncode=0, stdout='{"result":{"shown":false}}')),
    )
    transport = TerminalNotificationTransport(
        "herdr", environment={"HERDR_ENV": "1"}, write_raw=Mock()
    )

    assert transport.send("TFR has activity") is False
    assert transport.sent == 0


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_tmux_live_passthrough_reaches_outer_terminal(tmp_path) -> None:
    socket_name = f"tfr-notify-{os.getpid()}"
    sequence = _tmux_passthrough("\x1b]9;TFR tmux test\x07")
    script = (
        "import os,time; time.sleep(.2); "
        f"os.write(1,bytes.fromhex('{sequence.encode().hex()}')); time.sleep(.2)"
    )
    command = f"python3 -c {shlex.quote(script)}"
    subprocess.run(
        ["tmux", "-L", socket_name, "-f", "/dev/null", "new-session", "-d", command],
        check=True,
    )
    subprocess.run(
        ["tmux", "-L", socket_name, "set-option", "-g", "allow-passthrough", "on"],
        check=True,
    )
    terminal, slave = pty.openpty()
    process = subprocess.Popen(
        ["tmux", "-L", socket_name, "attach-session"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline and process.poll() is None:
            ready, _, _ = select.select([terminal], [], [], 0.1)
            if ready:
                try:
                    output.extend(os.read(terminal, 4096))
                except OSError:
                    break
        process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(terminal)
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    assert b"\x1b]9;TFR tmux test\x07" in output
