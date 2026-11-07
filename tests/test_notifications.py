from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock
from uuid import UUID

from tfr.config import NotificationConfig
from tfr.events import Confidence, Direction, Event, EventKind, Provenance
from tfr.notifications import NotificationPolicy, TerminalNotificationTransport


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
        environment={"TERM_PROGRAM": "ghostty", "TMUX": "/tmp/tmux"},
        write_raw=written.append,
    )

    assert transport.send("TFR has\nactivity") is True
    assert transport.resolved == "osc-9"
    assert transport.multiplexer == "tmux"
    assert written[0].startswith("\x1bPtmux;")
    assert "TFR has activity" in written[0]


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
