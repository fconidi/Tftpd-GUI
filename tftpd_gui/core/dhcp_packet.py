"""DHCP packet encoding and decoding (RFC 2131, RFC 2132)."""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass, field

DISCOVER = 1
OFFER = 2
REQUEST = 3
DECLINE = 4
ACK = 5
NAK = 6
RELEASE = 7
INFORM = 8

MAGIC = b"\x63\x82\x53\x63"
OPT_MESSAGE_TYPE = 53

_HEADER = struct.Struct("!4BIHH4s4s4s4s16s64s128s")
_MIN_LENGTH = 300
_PAD = 0
_END = 255


class DhcpError(Exception):
    """Malformed DHCP packet or invalid address."""


@dataclass
class DhcpPacket:
    op: int
    htype: int
    hlen: int
    hops: int
    xid: int
    secs: int
    flags: int
    ciaddr: str
    yiaddr: str
    siaddr: str
    giaddr: str
    chaddr: bytes
    options: dict[int, bytes] = field(default_factory=dict)


def ip_to_bytes(text: str) -> bytes:
    try:
        return socket.inet_pton(socket.AF_INET, text)
    except (OSError, TypeError, ValueError) as exc:
        raise DhcpError(f"invalid IPv4 address: {text!r}") from exc


def bytes_to_ip(raw: bytes) -> str:
    if len(raw) != 4:
        raise DhcpError("an IPv4 address needs 4 bytes")
    return socket.inet_ntop(socket.AF_INET, raw)


def _parse_options(data: bytes) -> dict[int, bytes]:
    options: dict[int, bytes] = {}
    i = 0
    while i < len(data):
        code = data[i]
        i += 1
        if code == _PAD:
            continue
        if code == _END:
            return options
        if i >= len(data):
            raise DhcpError("option without length")
        length = data[i]
        i += 1
        if i + length > len(data):
            raise DhcpError("option longer than the packet")
        options[code] = data[i : i + length]
        i += length
    raise DhcpError("option list has no END")


def decode(data: bytes) -> DhcpPacket:
    if len(data) < _HEADER.size + len(MAGIC):
        raise DhcpError("packet too short")
    fields = _HEADER.unpack_from(data)
    if data[_HEADER.size : _HEADER.size + 4] != MAGIC:
        raise DhcpError("bad magic cookie")
    op, htype, hlen, hops, xid, secs, flags, ci, yi, si, gi, chaddr, _sname, _file = fields
    return DhcpPacket(
        op=op,
        htype=htype,
        hlen=hlen,
        hops=hops,
        xid=xid,
        secs=secs,
        flags=flags,
        ciaddr=bytes_to_ip(ci),
        yiaddr=bytes_to_ip(yi),
        siaddr=bytes_to_ip(si),
        giaddr=bytes_to_ip(gi),
        chaddr=chaddr,
        options=_parse_options(data[_HEADER.size + 4 :]),
    )


def encode_options(options: dict[int, bytes]) -> bytes:
    out = bytearray()
    for code, value in options.items():
        if code in (_PAD, _END):
            raise DhcpError(f"option {code} is reserved")
        if not 0 < code < 255 or len(value) > 255:
            raise DhcpError(f"option {code} cannot be encoded")
        out += bytes((code, len(value))) + value
    out.append(_END)
    return bytes(out)


def encode(packet: DhcpPacket) -> bytes:
    header = _HEADER.pack(
        packet.op,
        packet.htype,
        packet.hlen,
        packet.hops,
        packet.xid,
        packet.secs,
        packet.flags,
        ip_to_bytes(packet.ciaddr),
        ip_to_bytes(packet.yiaddr),
        ip_to_bytes(packet.siaddr),
        ip_to_bytes(packet.giaddr),
        packet.chaddr[:16].ljust(16, b"\x00"),
        b"",
        b"",
    )
    raw = header + MAGIC + encode_options(packet.options)
    return raw.ljust(_MIN_LENGTH, b"\x00")


def message_type(packet: DhcpPacket) -> int | None:
    value = packet.options.get(OPT_MESSAGE_TYPE)
    if value is None or len(value) != 1:
        return None
    return value[0]
