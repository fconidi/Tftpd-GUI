"""TFTP packet encoding and decoding (RFC 1350, RFC 2347)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

RRQ = 1
WRQ = 2
DATA = 3
ACK = 4
ERROR = 5
OACK = 6

_ILLEGAL = 4


class TftpError(Exception):
    """Malformed packet or TFTP protocol error."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Packet:
    opcode: int
    filename: str | None = None
    mode: str | None = None
    options: dict[str, str] = field(default_factory=dict)
    block: int | None = None
    payload: bytes | None = None
    error_code: int | None = None
    message: str | None = None


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _parse_options(fields: list[bytes]) -> dict[str, str]:
    if len(fields) % 2:
        raise TftpError(_ILLEGAL, "odd number of option fields")
    options: dict[str, str] = {}
    for i in range(0, len(fields), 2):
        options[_text(fields[i]).lower()] = _text(fields[i + 1])
    return options


def decode(data: bytes) -> Packet:
    if len(data) < 2:
        raise TftpError(_ILLEGAL, "packet too short")
    opcode = struct.unpack("!H", data[:2])[0]
    body = data[2:]
    if opcode in (RRQ, WRQ):
        if not body.endswith(b"\x00"):
            raise TftpError(_ILLEGAL, "request not NUL-terminated")
        fields = body[:-1].split(b"\x00")
        if len(fields) < 2:
            raise TftpError(_ILLEGAL, "request missing mode")
        return Packet(
            opcode,
            filename=_text(fields[0]),
            mode=_text(fields[1]).lower(),
            options=_parse_options(fields[2:]),
        )
    if opcode in (DATA, ACK):
        if len(body) < 2:
            raise TftpError(_ILLEGAL, "packet too short")
        block = struct.unpack("!H", body[:2])[0]
        if opcode == ACK:
            return Packet(opcode, block=block)
        return Packet(opcode, block=block, payload=body[2:])
    if opcode == ERROR:
        if len(body) < 2:
            raise TftpError(_ILLEGAL, "packet too short")
        code = struct.unpack("!H", body[:2])[0]
        message = body[2:].split(b"\x00", 1)[0]
        return Packet(opcode, error_code=code, message=_text(message))
    if opcode == OACK:
        if body and not body.endswith(b"\x00"):
            raise TftpError(_ILLEGAL, "option ack not NUL-terminated")
        fields = body[:-1].split(b"\x00") if body else []
        return Packet(opcode, options=_parse_options(fields))
    raise TftpError(_ILLEGAL, "unknown opcode")


def _options_bytes(options: dict[str, str] | None) -> bytes:
    out = b""
    for name, value in (options or {}).items():
        out += name.lower().encode() + b"\x00" + str(value).encode() + b"\x00"
    return out


def encode_request(
    opcode: int, filename: str, mode: str, options: dict[str, str] | None = None
) -> bytes:
    if opcode not in (RRQ, WRQ):
        raise ValueError("opcode must be RRQ or WRQ")
    return (
        struct.pack("!H", opcode)
        + filename.encode()
        + b"\x00"
        + mode.encode()
        + b"\x00"
        + _options_bytes(options)
    )


def _check_u16(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} out of range 0..65535: {value}")


def encode_data(block: int, payload: bytes) -> bytes:
    _check_u16("block", block)
    return struct.pack("!HH", DATA, block) + payload


def encode_ack(block: int) -> bytes:
    _check_u16("block", block)
    return struct.pack("!HH", ACK, block)


def encode_error(code: int, message: str) -> bytes:
    _check_u16("error code", code)
    return struct.pack("!HH", ERROR, code) + message.encode() + b"\x00"


def encode_oack(options: dict[str, str]) -> bytes:
    return struct.pack("!H", OACK) + _options_bytes(options)
