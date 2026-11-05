"""Segment probe: send one DHCPDISCOVER and list who answers.

The probe never sends a DHCPREQUEST, so it cannot take a lease. Silence does
not prove the segment is free of other DHCP servers.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from typing import Callable

from . import dhcp_packet as dp
from .dhcp_packet import DhcpError, DhcpPacket

_SERVER_PORT = 67


@dataclass
class ProbeResult:
    status: str  # "found", "not_found" or "error"
    servers: list[dict] = field(default_factory=list)
    reason: str | None = None


def probe_segment(
    interface: str,
    *,
    timeout: float = 3.0,
    client_port: int = 68,
    socket_factory: Callable[..., socket.socket] = socket.socket,
) -> ProbeResult:
    sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
            sock.bind(("0.0.0.0", client_port))
        except OSError as exc:
            return ProbeResult("error", [], f"cannot listen on UDP port {client_port}: {exc.strerror or exc}")
        mac = bytes([0x02]) + os.urandom(5)
        xid = int.from_bytes(os.urandom(4), "big")
        discover = DhcpPacket(
            op=1, htype=1, hlen=6, hops=0, xid=xid, secs=0, flags=0x8000,
            ciaddr="0.0.0.0", yiaddr="0.0.0.0", siaddr="0.0.0.0", giaddr="0.0.0.0",
            chaddr=mac.ljust(16, b"\x00"),
            options={dp.OPT_MESSAGE_TYPE: bytes([dp.DISCOVER]), 55: bytes([1, 3, 6])},
        )
        try:
            sock.sendto(dp.encode(discover), ("255.255.255.255", _SERVER_PORT))
        except OSError as exc:
            return ProbeResult("error", [], f"cannot send the probe: {exc.strerror or exc}")
        servers: list[dict] = []
        seen: set[str] = set()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, peer = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError as exc:
                return ProbeResult("error", servers, f"receive failed: {exc.strerror or exc}")
            try:
                reply = dp.decode(data)
            except DhcpError:
                continue
            if reply.xid != xid or dp.message_type(reply) != dp.OFFER:
                continue
            raw_id = reply.options.get(54)
            server_id = dp.bytes_to_ip(raw_id) if raw_id and len(raw_id) == 4 else peer[0]
            if server_id in seen:
                continue
            seen.add(server_id)
            # UDP does not reveal the server's MAC address.
            servers.append({"server_id": server_id, "offered_ip": reply.yiaddr, "mac": None})
        return ProbeResult("found" if servers else "not_found", servers, None)
    finally:
        sock.close()
