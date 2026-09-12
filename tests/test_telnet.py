from __future__ import annotations

from tfr.telnet import Command, NegotiationKind, Option, TelnetCodec, escape_iac


def test_preserves_fragmented_application_data_and_escaped_iac() -> None:
    codec = TelnetCodec()

    first = codec.feed(b"hello\xff")
    second = codec.feed(b"\xffworld")

    assert first.data == b"hello"
    assert second.data == b"\xffworld"


def test_accepts_supported_remote_option_across_chunks() -> None:
    codec = TelnetCodec()

    first = codec.feed(bytes((Command.IAC, Command.WILL)))
    second = codec.feed(bytes((Option.ECHO,)))

    assert first.responses == ()
    assert second.responses == (bytes((Command.IAC, Command.DO, Option.ECHO)),)
    assert second.events[0].kind is NegotiationKind.NEGOTIATION
    assert second.events[0].option == Option.ECHO


def test_rejects_unsupported_options() -> None:
    codec = TelnetCodec()

    remote = codec.feed(bytes((Command.IAC, Command.WILL, 99)))
    local = codec.feed(bytes((Command.IAC, Command.DO, 98)))

    assert remote.responses == (bytes((Command.IAC, Command.DONT, 99)),)
    assert local.responses == (bytes((Command.IAC, Command.WONT, 98)),)


def test_reports_telnet_commands_without_application_data() -> None:
    codec = TelnetCodec()

    result = codec.feed(bytes((Command.IAC, Command.NOP)))

    assert result.data == b""
    assert result.events[0].kind is NegotiationKind.COMMAND
    assert result.events[0].command == Command.NOP


def test_answers_terminal_type_subnegotiation_incrementally() -> None:
    codec = TelnetCodec(terminal_type="TFR")
    codec.feed(bytes((Command.IAC, Command.DO, Option.TERMINAL_TYPE)))

    codec.feed(bytes((Command.IAC, Command.SB, Option.TERMINAL_TYPE, 1, Command.IAC)))
    result = codec.feed(bytes((Command.SE,)))

    assert result.responses == (
        bytes(
            (
                Command.IAC,
                Command.SB,
                Option.TERMINAL_TYPE,
                0,
                ord("T"),
                ord("F"),
                ord("R"),
                Command.IAC,
                Command.SE,
            )
        ),
    )
    assert result.events[0].kind is NegotiationKind.SUBNEGOTIATION


def test_sends_window_size_and_escapes_iac_bytes() -> None:
    codec = TelnetCodec(columns=255, rows=24)

    result = codec.feed(bytes((Command.IAC, Command.DO, Option.NAWS)))

    assert result.responses[0] == bytes((Command.IAC, Command.WILL, Option.NAWS))
    assert result.responses[1] == bytes(
        (
            Command.IAC,
            Command.SB,
            Option.NAWS,
            0,
            255,
            255,
            0,
            24,
            Command.IAC,
            Command.SE,
        )
    )


def test_accepts_requested_utf8_charset() -> None:
    codec = TelnetCodec(encoding="utf-8")
    codec.feed(bytes((Command.IAC, Command.DO, Option.CHARSET)))
    request = (
        bytes(
            (
                Command.IAC,
                Command.SB,
                Option.CHARSET,
                1,
                ord(";"),
            )
        )
        + b"US-ASCII;UTF-8"
        + bytes((Command.IAC, Command.SE))
    )

    result = codec.feed(request)

    assert result.responses == (
        bytes((Command.IAC, Command.SB, Option.CHARSET, 2))
        + b"UTF-8"
        + bytes((Command.IAC, Command.SE)),
    )


def test_escapes_outbound_iac() -> None:
    assert escape_iac(b"a\xffb") == b"a\xff\xffb"
