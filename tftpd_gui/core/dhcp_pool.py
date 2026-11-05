"""Address pool with leases for the staging DHCP server."""

from __future__ import annotations

import ipaddress
import threading
import time
from typing import Callable


class LeasePool:
    def __init__(
        self,
        start: str,
        end: str,
        mask: str,
        *,
        lease_seconds: int = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.start = start
        self.end = end
        self.mask = mask
        self.lease_seconds = lease_seconds
        self._clock = clock
        self._first = int(ipaddress.IPv4Address(start))
        self._last = int(ipaddress.IPv4Address(end))
        self._leases: dict[bytes, dict] = {}
        self._declined: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- helpers (call with the lock held) -------------------------------

    def _purge(self) -> None:
        now = self._clock()
        for mac in [m for m, lease in self._leases.items() if lease["expires"] <= now]:
            del self._leases[mac]
        for ip in [i for i, until in self._declined.items() if until <= now]:
            del self._declined[ip]

    def _holder(self, ip: str) -> bytes | None:
        for mac, lease in self._leases.items():
            if lease["ip"] == ip:
                return mac
        return None

    def _free(self, ip: str) -> bool:
        return self._holder(ip) is None and ip not in self._declined

    def contains(self, ip: str) -> bool:
        try:
            value = int(ipaddress.IPv4Address(ip))
        except ValueError:
            return False
        return self._first <= value <= self._last

    # -- public ----------------------------------------------------------

    def acquire(self, mac: bytes, requested: str | None = None, hostname: str | None = None) -> str | None:
        with self._lock:
            self._purge()
            ip = None
            current = self._leases.get(mac)
            if current is not None:
                ip = current["ip"]
            elif requested and self.contains(requested) and self._free(requested):
                ip = requested
            else:
                for value in range(self._first, self._last + 1):
                    candidate = str(ipaddress.IPv4Address(value))
                    if self._free(candidate):
                        ip = candidate
                        break
            if ip is None:
                return None
            self._leases[mac] = {
                "ip": ip,
                "hostname": hostname if hostname is not None else (current or {}).get("hostname"),
                "expires": self._clock() + self.lease_seconds,
            }
            return ip

    def confirm(self, mac: bytes, ip: str) -> bool:
        with self._lock:
            self._purge()
            lease = self._leases.get(mac)
            if lease is None or lease["ip"] != ip:
                return False
            lease["expires"] = self._clock() + self.lease_seconds
            return True

    def release(self, mac: bytes) -> None:
        with self._lock:
            self._leases.pop(mac, None)

    def decline(self, ip: str) -> None:
        with self._lock:
            holder = self._holder(ip)
            if holder is not None:
                del self._leases[holder]
            self._declined[ip] = self._clock() + self.lease_seconds

    def owner(self, ip: str) -> bytes | None:
        with self._lock:
            self._purge()
            return self._holder(ip)

    def leases(self) -> list[dict]:
        with self._lock:
            self._purge()
            return [
                {"mac": mac.hex(":"), "ip": lease["ip"], "hostname": lease["hostname"],
                 "expires": lease["expires"]}
                for mac, lease in sorted(self._leases.items(), key=lambda kv: kv[1]["ip"])
            ]
