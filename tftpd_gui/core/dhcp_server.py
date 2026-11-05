"""Staging DHCP server (RFC 2131 section 4.3.2).

The server never answers a request it cannot be sure is meant for it: a
REQUEST naming another server is ignored, and an address outside the pool is
ignored rather than refused because another server may own it.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import socket
import threading
import time
from typing import Callable

from . import dhcp_packet as dp
from .dhcp_packet import DhcpError, DhcpPacket
from .dhcp_pool import LeasePool
from .errors import DhcpConfigError, PortBusy

_log = logging.getLogger(__name__)

_POLL = 0.1
_BROADCAST = "255.255.255.255"
_ANY = "0.0.0.0"
OPT_MASK, OPT_ROUTER, OPT_DNS, OPT_HOSTNAME = 1, 3, 6, 12
OPT_REQUESTED, OPT_LEASE, OPT_SERVER_ID = 50, 51, 54
OPT_TFTP_SERVER, OPT_BOOTFILE, OPT_T1, OPT_T2 = 66, 67, 58, 59
BOOTREQUEST, BOOTREPLY = 1, 2


def _ipv4(text: str) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(text)


def validate_config(
    server_ip: str,
    netmask: str,
    start: str,
    end: str,
    router: str | None,
    dns: tuple[str, ...],
) -> None:
    try:
        server = _ipv4(server_ip)
    except ValueError:
        raise DhcpConfigError("no_ipv4", "the interface has no usable IPv4 address") from None
    try:
        network = ipaddress.IPv4Network(f"{server}/{netmask}", strict=False)
    except ValueError:
        raise DhcpConfigError("mask_invalid", f"invalid netmask {netmask!r}") from None
    if network.prefixlen > 30 or network.prefixlen == 0:
        raise DhcpConfigError("mask_invalid", f"netmask {netmask} leaves no room for a pool")
    try:
        first, last = _ipv4(start), _ipv4(end)
    except ValueError:
        raise DhcpConfigError("pool_outside_subnet", "pool address is not IPv4") from None
    if first > last:
        raise DhcpConfigError("pool_reversed", "pool start is after pool end")
    usable = (network.network_address + 1, network.broadcast_address - 1)
    if not (usable[0] <= first and last <= usable[1]):
        raise DhcpConfigError("pool_outside_subnet", "pool is not inside the interface subnet")
    if first <= server <= last:
        raise DhcpConfigError("pool_contains_server", "pool contains the server address")
    if router is not None:
        try:
            inside = _ipv4(router) in network
        except ValueError:
            inside = False
        if not inside:
            raise DhcpConfigError("gateway_outside_subnet", "gateway is not inside the subnet")
    for entry in dns:
        try:
            _ipv4(entry)
        except ValueError:
            raise DhcpConfigError("dns_invalid", f"invalid DNS server {entry!r}") from None


def _text(raw: bytes | None) -> str | None:
    return raw.decode("utf-8", errors="replace") if raw else None


class DhcpServer:
    def __init__(
        self,
        pool: LeasePool,
        *,
        interface: str,
        server_ip: str,
        netmask: str,
        port: int = 67,
        client_port: int = 68,
        router: str | None = None,
        dns: tuple[str, ...] = (),
        next_server: str | None = None,
        bootfile: str | None = None,
        auto_stop_seconds: int | None = 1800,
        on_event: Callable[[dict], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        socket_factory: Callable[..., socket.socket] = socket.socket,
    ) -> None:
        self.pool = pool
        self.interface = interface
        self.server_ip = server_ip
        self.netmask = netmask
        self.port = port
        self.client_port = client_port
        self.router = router
        self.dns = dns
        self.next_server = next_server
        self.bootfile = bootfile
        self.auto_stop_seconds = auto_stop_seconds
        self.on_event = on_event
        self._clock = clock
        self._socket_factory = socket_factory
        self._started_at = clock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._timed_out = False
        self._lock = threading.Lock()

    # -- events ----------------------------------------------------------

    def _emit(self, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:  # a broken listener must not stop the server
            _log.exception("DHCP event listener failed")

    # -- reply logic -----------------------------------------------------

    def _reply_packet(self, request: DhcpPacket, kind: int, yiaddr: str = "0.0.0.0",
                      *, ciaddr: str = "0.0.0.0") -> DhcpPacket:
        options: dict[int, bytes] = {
            dp.OPT_MESSAGE_TYPE: bytes([kind]),
            OPT_SERVER_ID: dp.ip_to_bytes(self.server_ip),
        }
        siaddr = "0.0.0.0"
        if kind in (dp.OFFER, dp.ACK):
            options[OPT_MASK] = dp.ip_to_bytes(self.netmask)
            if self.router:
                options[OPT_ROUTER] = dp.ip_to_bytes(self.router)
            if self.dns:
                options[OPT_DNS] = b"".join(dp.ip_to_bytes(d) for d in self.dns)
            if yiaddr != "0.0.0.0":
                lease = self.pool.lease_seconds
                options[OPT_LEASE] = lease.to_bytes(4, "big")
                options[OPT_T1] = (lease // 2).to_bytes(4, "big")
                options[OPT_T2] = (lease * 7 // 8).to_bytes(4, "big")
            if self.next_server:
                siaddr = self.next_server
                options[OPT_TFTP_SERVER] = self.next_server.encode()
            if self.bootfile:
                options[OPT_BOOTFILE] = self.bootfile.encode()
        return DhcpPacket(
            op=BOOTREPLY, htype=request.htype, hlen=request.hlen, hops=0, xid=request.xid,
            secs=0, flags=request.flags, ciaddr=ciaddr, yiaddr=yiaddr, siaddr=siaddr,
            giaddr="0.0.0.0", chaddr=request.chaddr, options=options,
        )

    @staticmethod
    def _client_id(request: DhcpPacket) -> bytes:
        return bytes(request.chaddr[: request.hlen or 6])

    def _reply(self, request: DhcpPacket) -> tuple[bytes, str] | None:
        if request.op != BOOTREQUEST or request.giaddr != "0.0.0.0":
            return None
        kind = dp.message_type(request)
        mac = self._client_id(request)
        label = mac.hex(":")
        server_id = request.options.get(OPT_SERVER_ID)
        ours = server_id is None or server_id == dp.ip_to_bytes(self.server_ip)
        hostname = _text(request.options.get(OPT_HOSTNAME))

        if kind == dp.DISCOVER:
            wanted = request.options.get(OPT_REQUESTED)
            ip = self.pool.acquire(mac, dp.bytes_to_ip(wanted) if wanted and len(wanted) == 4 else None,
                                   hostname)
            if ip is None:
                self._emit({"event": "pool_exhausted", "mac": label})
                return None
            self._emit({"event": "offer", "mac": label, "ip": ip})
            return dp.encode(self._reply_packet(request, dp.OFFER, ip)), _BROADCAST

        if kind == dp.REQUEST:
            return self._request(request, mac, label, server_id, hostname)

        if kind == dp.DECLINE:
            wanted = request.options.get(OPT_REQUESTED)
            if ours and server_id is not None and wanted and len(wanted) == 4:
                ip = dp.bytes_to_ip(wanted)
                if self.pool.contains(ip):
                    self.pool.decline(ip)
                    self._emit({"event": "decline", "mac": label, "ip": ip})
            return None

        if kind == dp.RELEASE:
            if ours and server_id is not None and self.pool.owner(request.ciaddr) == mac:
                self.pool.release(mac)
                self._emit({"event": "release", "mac": label, "ip": request.ciaddr})
            return None

        if kind == dp.INFORM:
            if request.ciaddr == "0.0.0.0":
                return None
            reply = self._reply_packet(request, dp.ACK, ciaddr=request.ciaddr)
            reply.options.pop(OPT_LEASE, None)
            self._emit({"event": "inform", "mac": label, "ip": request.ciaddr})
            return dp.encode(reply), request.ciaddr
        return None

    def _request(self, request, mac, label, server_id, hostname):
        wanted = request.options.get(OPT_REQUESTED)
        wanted_ip = dp.bytes_to_ip(wanted) if wanted and len(wanted) == 4 else None
        if server_id is not None:  # SELECTING
            if server_id != dp.ip_to_bytes(self.server_ip):
                if wanted_ip and self.pool.owner(wanted_ip) == mac:
                    self.pool.release(mac)  # the client chose another server
                return None
            if request.ciaddr != "0.0.0.0" or wanted_ip is None:
                return None
            ip = wanted_ip
        elif wanted_ip is not None:  # INIT-REBOOT
            if request.ciaddr != "0.0.0.0":
                return None
            ip = wanted_ip
        elif request.ciaddr != "0.0.0.0":  # RENEWING / REBINDING
            ip = request.ciaddr
        else:
            return None
        if not self.pool.contains(ip):
            return None  # someone else's address: stay silent, never NAK
        if self.pool.confirm(mac, ip) or (
            self.pool.owner(ip) is None and self.pool.acquire(mac, ip, hostname) == ip
        ):
            self._emit({"event": "ack", "mac": label, "ip": ip})
            reply = self._reply_packet(request, dp.ACK, ip, ciaddr=request.ciaddr)
            dest = request.ciaddr if request.ciaddr != "0.0.0.0" else _BROADCAST
            return dp.encode(reply), dest
        self._emit({"event": "nak", "mac": label, "ip": ip})
        return dp.encode(self._reply_packet(request, dp.NAK)), _BROADCAST

    def build_reply(self, packet: DhcpPacket) -> bytes | None:
        result = self._reply(packet)
        return None if result is None else result[0]

    # -- datagrams, timer, lifecycle ------------------------------------

    def _handle(self, data: bytes, sock: socket.socket) -> None:
        try:
            request = dp.decode(data)
            result = self._reply(request)
        except (DhcpError, ValueError):
            return
        if result is None:
            return
        raw, dest = result
        try:
            sock.sendto(raw, (dest, self.client_port))
        except OSError as exc:
            self._emit({"event": "error", "message": str(exc)})

    def tick(self) -> None:
        if self.auto_stop_seconds is None or self._timed_out:
            return
        if self._clock() - self._started_at >= self.auto_stop_seconds:
            self._timed_out = True
            self._stopping.set()
            self._emit({"event": "stopped", "reason": "timeout"})

    def _new_socket(self) -> socket.socket:
        sock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.interface.encode() + b"\0")
            sock.bind((_ANY, self.port))
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE:
                raise PortBusy(self.port) from exc
            raise
        return sock

    def start(self) -> int:
        with self._lock:
            if self._sock is not None:
                raise RuntimeError("DHCP server already running")
        validate_config(self.server_ip, self.netmask, self.pool.start, self.pool.end,
                        self.router, self.dns)
        sock = self._new_socket()
        sock.settimeout(_POLL)
        self._stopping.clear()
        self._timed_out = False
        self._started_at = self._clock()
        thread = threading.Thread(target=self._serve, args=(sock,), name="dhcp", daemon=True)
        with self._lock:
            self._sock, self._thread = sock, thread
        thread.start()
        return sock.getsockname()[1]

    def _serve(self, sock: socket.socket) -> None:
        while not self._stopping.is_set():
            self.tick()
            try:
                data, _peer = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle(data, sock)

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            sock, self._sock = self._sock, None
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)
        if sock is not None:
            sock.close()
