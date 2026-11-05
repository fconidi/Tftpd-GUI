"""Network interface discovery."""

from __future__ import annotations

import fcntl
import os
import socket
import struct
from pathlib import Path

_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B


def list_interfaces(sysfs: Path = Path("/sys/class/net")) -> list[str]:
    """Names of physical-looking interfaces: no loopback, no virtual ones."""
    try:
        names = os.listdir(sysfs)
    except OSError:
        return []
    found = [
        name
        for name in names
        if name != "lo" and os.path.lexists(os.path.join(sysfs, name, "device"))
    ]
    return sorted(found)


def _query(sock: socket.socket, request: int, name: bytes) -> str:
    packed = struct.pack("256s", name)
    reply = fcntl.ioctl(sock.fileno(), request, packed)
    return socket.inet_ntoa(reply[20:24])


def interface_ipv4(name: str) -> tuple[str, str] | None:
    """(address, netmask) of the first IPv4 address of ``name``, or None."""
    raw = name.encode()[:15]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            return _query(sock, _SIOCGIFADDR, raw), _query(sock, _SIOCGIFNETMASK, raw)
    except OSError:
        return None
