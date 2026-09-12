from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class Command(IntEnum):
    SE = 240
    NOP = 241
    SB = 250
    WILL = 251
    WONT = 252
    DO = 253
    DONT = 254
    IAC = 255


class Option(IntEnum):
    ECHO = 1
    SUPPRESS_GO_AHEAD = 3
    TERMINAL_TYPE = 24
    NAWS = 31
    CHARSET = 42


class NegotiationKind(StrEnum):
    NEGOTIATION = "negotiation"
    COMMAND = "command"
    SUBNEGOTIATION = "subnegotiation"


@dataclass(frozen=True, slots=True)
class TelnetEvent:
    kind: NegotiationKind
    command: int
    option: int | None = None
    payload: bytes = b""


@dataclass(frozen=True, slots=True)
class TelnetResult:
    data: bytes
    responses: tuple[bytes, ...]
    events: tuple[TelnetEvent, ...]


class _State(IntEnum):
    DATA = 0
    IAC = 1
    NEGOTIATION = 2
    SUBNEGOTIATION_OPTION = 3
    SUBNEGOTIATION = 4
    SUBNEGOTIATION_IAC = 5


_CHARSET_REQUEST = 1
_CHARSET_ACCEPTED = 2
_TERMINAL_TYPE_IS = 0
_TERMINAL_TYPE_SEND = 1


def escape_iac(data: bytes) -> bytes:
    return data.replace(bytes((Command.IAC,)), bytes((Command.IAC, Command.IAC)))


def _command(command: Command, option: int) -> bytes:
    return bytes((Command.IAC, command, option))


def _subnegotiation(option: int, payload: bytes) -> bytes:
    return (
        bytes((Command.IAC, Command.SB, option))
        + escape_iac(payload)
        + bytes((Command.IAC, Command.SE))
    )


class TelnetCodec:
    """Incrementally separates Telnet control traffic from application bytes."""

    def __init__(
        self,
        *,
        terminal_type: str = "TFR",
        columns: int = 80,
        rows: int = 24,
        encoding: str = "utf-8",
    ) -> None:
        self.terminal_type = terminal_type
        self.columns = columns
        self.rows = rows
        self.encoding = encoding
        self._state = _State.DATA
        self._negotiation_command: int | None = None
        self._subnegotiation_option: int | None = None
        self._subnegotiation_data = bytearray()
        self._enabled_local: set[int] = set()
        self._enabled_remote: set[int] = set()

    def feed(self, chunk: bytes) -> TelnetResult:
        data = bytearray()
        responses: list[bytes] = []
        events: list[TelnetEvent] = []

        for value in chunk:
            if self._state is _State.DATA:
                if value == Command.IAC:
                    self._state = _State.IAC
                else:
                    data.append(value)
                continue

            if self._state is _State.IAC:
                if value == Command.IAC:
                    data.append(value)
                    self._state = _State.DATA
                elif value in (Command.WILL, Command.WONT, Command.DO, Command.DONT):
                    self._negotiation_command = value
                    self._state = _State.NEGOTIATION
                elif value == Command.SB:
                    self._state = _State.SUBNEGOTIATION_OPTION
                else:
                    events.append(TelnetEvent(NegotiationKind.COMMAND, value))
                    self._state = _State.DATA
                continue

            if self._state is _State.NEGOTIATION:
                assert self._negotiation_command is not None
                command = self._negotiation_command
                events.append(TelnetEvent(NegotiationKind.NEGOTIATION, command, value))
                responses.extend(self._negotiate(command, value))
                self._negotiation_command = None
                self._state = _State.DATA
                continue

            if self._state is _State.SUBNEGOTIATION_OPTION:
                self._subnegotiation_option = value
                self._subnegotiation_data.clear()
                self._state = _State.SUBNEGOTIATION
                continue

            if self._state is _State.SUBNEGOTIATION:
                if value == Command.IAC:
                    self._state = _State.SUBNEGOTIATION_IAC
                else:
                    self._subnegotiation_data.append(value)
                continue

            if self._state is _State.SUBNEGOTIATION_IAC:
                if value == Command.SE:
                    assert self._subnegotiation_option is not None
                    option = self._subnegotiation_option
                    payload = bytes(self._subnegotiation_data)
                    events.append(
                        TelnetEvent(
                            NegotiationKind.SUBNEGOTIATION,
                            Command.SB,
                            option,
                            payload,
                        )
                    )
                    response = self._handle_subnegotiation(option, payload)
                    if response is not None:
                        responses.append(response)
                    self._subnegotiation_option = None
                    self._subnegotiation_data.clear()
                    self._state = _State.DATA
                elif value == Command.IAC:
                    self._subnegotiation_data.append(value)
                    self._state = _State.SUBNEGOTIATION
                else:
                    self._subnegotiation_data.extend((Command.IAC, value))
                    self._state = _State.SUBNEGOTIATION

        return TelnetResult(bytes(data), tuple(responses), tuple(events))

    def _negotiate(self, command: int, option: int) -> tuple[bytes, ...]:
        responses: list[bytes] = []
        if command == Command.WILL:
            if option in (Option.ECHO, Option.SUPPRESS_GO_AHEAD, Option.CHARSET):
                if option not in self._enabled_remote:
                    self._enabled_remote.add(option)
                    responses.append(_command(Command.DO, option))
            else:
                responses.append(_command(Command.DONT, option))
        elif command == Command.WONT:
            self._enabled_remote.discard(option)
        elif command == Command.DO:
            if option in (Option.TERMINAL_TYPE, Option.NAWS, Option.CHARSET):
                if option not in self._enabled_local:
                    self._enabled_local.add(option)
                    responses.append(_command(Command.WILL, option))
                if option == Option.NAWS:
                    responses.append(self.window_size_message())
            else:
                responses.append(_command(Command.WONT, option))
        elif command == Command.DONT:
            self._enabled_local.discard(option)
        return tuple(responses)

    def _handle_subnegotiation(self, option: int, payload: bytes) -> bytes | None:
        if (
            option == Option.TERMINAL_TYPE
            and option in self._enabled_local
            and payload == bytes((_TERMINAL_TYPE_SEND,))
        ):
            terminal = self.terminal_type.encode("ascii", errors="replace")
            return _subnegotiation(option, bytes((_TERMINAL_TYPE_IS,)) + terminal)

        if option == Option.CHARSET and payload[:1] == bytes((_CHARSET_REQUEST,)):
            selected = self._select_charset(payload[1:])
            if selected is not None:
                return _subnegotiation(
                    option,
                    bytes((_CHARSET_ACCEPTED,)) + selected.encode("ascii"),
                )
        return None

    def _select_charset(self, request: bytes) -> str | None:
        if len(request) < 2:
            return None
        separator = request[:1]
        offered = request[1:].split(separator)
        configured = self.encoding.casefold().replace("_", "-")
        for candidate in offered:
            name = candidate.decode("ascii", errors="ignore")
            normalized = name.casefold().replace("_", "-")
            if normalized == configured or (configured == "utf-8" and normalized == "utf8"):
                return name
        return None

    def set_window_size(self, columns: int, rows: int) -> bytes | None:
        if not 0 <= columns <= 65_535 or not 0 <= rows <= 65_535:
            raise ValueError("Telnet window dimensions must fit in 16 bits")
        self.columns = columns
        self.rows = rows
        if Option.NAWS in self._enabled_local:
            return self.window_size_message()
        return None

    def window_size_message(self) -> bytes:
        payload = self.columns.to_bytes(2, "big") + self.rows.to_bytes(2, "big")
        return _subnegotiation(Option.NAWS, payload)
