"""Privileged helper: runs the network services for the window.

It talks JSON lines on stdin/stdout (see ``core.protocol``). When stdin
closes, because the window exited or died, every service is stopped and the
process ends, so no root service is ever left listening. There is no command
that writes a file: saving a log is done by the window as the user.
"""

from __future__ import annotations

import collections
import os
import pwd
import sys
import threading
from pathlib import Path
from typing import IO, Callable, Mapping

from ..core import dhcp_probe
from ..core.dhcp_pool import LeasePool
from ..core.dhcp_server import DhcpServer
from ..core.errors import DhcpConfigError, PortBusy
from ..core.ifaces import interface_ipv4
from ..core.protocol import encode_message, iter_messages
from ..core.syslog_server import SyslogCollector
from ..core.tftp_server import TftpServer

EVENT_QUEUE_MAX = 1000
MIN_MAX_BYTES = 1024 ** 2
MAX_MAX_BYTES = 4096 * 1024 ** 2
DEFAULT_MAX_BYTES = 256 * 1024 ** 2


class CommandError(Exception):
    def __init__(self, code: str, detail=None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _arg(args: dict, name: str, kind, default=None, *, required: bool = False):
    value = args.get(name, default)
    if value is None:
        if required:
            raise CommandError("bad_arguments", name)
        return None
    if kind is int and isinstance(value, bool):
        raise CommandError("bad_arguments", name)
    if kind is float and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, kind):
        raise CommandError("bad_arguments", name)
    return value


def _dns(args: dict) -> tuple[str, ...]:
    value = args.get("dns")
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise CommandError("bad_arguments", "dns")
    return tuple(value)


class Daemon:
    def __init__(self, stdout: IO[bytes], environ: Mapping[str, str] | None = None) -> None:
        self._out = stdout
        self._write_lock = threading.Lock()
        self._cond = threading.Condition()
        self.events: collections.deque = collections.deque(maxlen=EVENT_QUEUE_MAX)
        self._dropped = 0
        self._closing = False
        self._environ = os.environ if environ is None else environ
        self._tftp: TftpServer | None = None
        self._dhcp: DhcpServer | None = None
        self._syslog: SyslogCollector | None = None
        self._pool: LeasePool | None = None
        self._dhcp_info: dict = {}
        self._writer = threading.Thread(target=self._drain, name="helper-events", daemon=True)

    # -- output ----------------------------------------------------------

    def _write(self, obj: dict) -> bool:
        data = encode_message(obj)
        with self._write_lock:
            try:
                self._out.write(data)
                self._out.flush()
            except (OSError, ValueError):
                return False
        return True

    def emit(self, name: str, data: dict) -> None:
        """Queue an event; the oldest entries are dropped past EVENT_QUEUE_MAX."""
        with self._cond:
            if len(self.events) == self.events.maxlen:
                self._dropped += 1
            self.events.append({"event": name, "data": data})
            self._cond.notify()

    def _drain(self) -> None:
        while True:
            with self._cond:
                while not self.events and not self._dropped and not self._closing:
                    self._cond.wait()
                if self._closing and not self.events and not self._dropped:
                    return
                batch = list(self.events)
                self.events.clear()
                dropped, self._dropped = self._dropped, 0
            if dropped:
                self._write({"event": "dropped", "data": {"count": dropped}})
            for event in batch:
                if not self._write(event):
                    return

    # -- lifecycle -------------------------------------------------------

    def run(self, stdin: IO[bytes]) -> int:
        self._writer.start()
        try:
            for request in iter_messages(stdin):
                self._write(self._answer(request))
        finally:
            self.stop_all()
            with self._cond:
                self._closing = True
                self._cond.notify()
            self._writer.join(2.0)
        return 0

    def stop_all(self) -> None:
        for name in ("_tftp", "_dhcp", "_syslog"):
            service = getattr(self, name)
            setattr(self, name, None)
            if service is not None:
                try:
                    service.stop()
                except Exception:
                    pass
        self._pool = None

    # -- commands --------------------------------------------------------

    def _answer(self, request: dict) -> dict:
        ident = request.get("id")
        if "error" in request:
            return {"id": ident, "ok": False, "error": request["error"]}
        cmd = request.get("cmd")
        args = request.get("args", {})
        handler = self._handlers().get(cmd) if isinstance(cmd, str) else None
        if handler is None:
            return {"id": ident, "ok": False, "error": "unknown_command"}
        if not isinstance(args, dict):
            return {"id": ident, "ok": False, "error": "bad_arguments"}
        try:
            return {"id": ident, "ok": True, "result": handler(args)}
        except CommandError as exc:
            return {"id": ident, "ok": False, "error": exc.code, "detail": exc.detail}
        except PortBusy as exc:
            return {"id": ident, "ok": False, "error": "port_busy", "detail": exc.port}
        except DhcpConfigError as exc:
            return {"id": ident, "ok": False, "error": "config_invalid", "detail": exc.code}
        except OSError as exc:
            return {"id": ident, "ok": False, "error": "os_error", "detail": exc.strerror or str(exc)}
        except Exception as exc:  # keep the loop alive whatever a handler does
            return {"id": ident, "ok": False, "error": "internal_error", "detail": type(exc).__name__}

    def _handlers(self) -> dict[str, Callable[[dict], dict]]:
        return {
            "start_tftp": self._start_tftp, "stop_tftp": self._stop_tftp,
            "start_dhcp": self._start_dhcp, "stop_dhcp": self._stop_dhcp,
            "probe_dhcp": self._probe_dhcp,
            "start_syslog": self._start_syslog, "stop_syslog": self._stop_syslog,
            "status": self._status,
        }

    def _file_identity(self) -> tuple[int | None, int | None]:
        raw = self._environ.get("PKEXEC_UID")
        if not raw:
            return None, None
        try:
            uid = int(raw)
            return uid, pwd.getpwuid(uid).pw_gid
        except (ValueError, KeyError):
            raise CommandError("bad_pkexec_uid", raw) from None

    def _start_tftp(self, args: dict) -> dict:
        if self._tftp is not None:
            raise CommandError("already_running", "tftp")
        root = _arg(args, "root", str, required=True)
        if not Path(root).is_dir():
            raise CommandError("bad_arguments", "root")
        port = _arg(args, "port", int, 69)
        max_bytes = _arg(args, "max_bytes", int, DEFAULT_MAX_BYTES)
        max_bytes = max(MIN_MAX_BYTES, min(MAX_MAX_BYTES, max_bytes))
        uid, gid = self._file_identity()
        server = TftpServer(
            Path(root), port=port, bind_device=_arg(args, "interface", str),
            allow_write=bool(_arg(args, "allow_write", bool, False)),
            allow_overwrite=bool(_arg(args, "allow_overwrite", bool, False)),
            max_bytes=max_bytes, file_uid=uid, file_gid=gid,
            on_event=lambda event: self.emit("tftp", event),
        )
        bound = server.start()
        self._tftp = server
        return {"port": bound}

    def _stop_tftp(self, args: dict) -> dict:
        server, self._tftp = self._tftp, None
        if server is not None:
            server.stop()
        return {}

    def _start_syslog(self, args: dict) -> dict:
        if self._syslog is not None:
            raise CommandError("already_running", "syslog")
        collector = SyslogCollector(
            port=_arg(args, "port", int, 514), bind_device=_arg(args, "interface", str),
            tcp=bool(_arg(args, "tcp", bool, False)),
            on_message=lambda message: self.emit("syslog", message),
        )
        bound = collector.start()
        self._syslog = collector
        return {"port": bound}

    def _stop_syslog(self, args: dict) -> dict:
        collector, self._syslog = self._syslog, None
        if collector is not None:
            collector.stop()
        return {}

    def _start_dhcp(self, args: dict) -> dict:
        if self._dhcp is not None:
            raise CommandError("already_running", "dhcp")
        interface = _arg(args, "interface", str, required=True)
        start = _arg(args, "pool_start", str, required=True)
        end = _arg(args, "pool_end", str, required=True)
        found = interface_ipv4(interface)
        if found is None:
            raise DhcpConfigError("no_ipv4")
        server_ip, netmask = found  # never taken from the request
        pool = LeasePool(start, end, netmask, lease_seconds=_arg(args, "lease_seconds", int, 600))
        auto_stop = _arg(args, "auto_stop_seconds", int, 1800)
        server = DhcpServer(
            pool, interface=interface, server_ip=server_ip, netmask=netmask,
            port=_arg(args, "port", int, 67),
            router=_arg(args, "router", str), dns=_dns(args),
            next_server=_arg(args, "next_server", str), bootfile=_arg(args, "bootfile", str),
            auto_stop_seconds=auto_stop or None,
            on_event=self._dhcp_event,
        )
        port = server.start()
        self._dhcp, self._pool = server, pool
        self._dhcp_info = {"interface": interface, "server_ip": server_ip, "netmask": netmask}
        return {"port": port, **self._dhcp_info}

    def _dhcp_event(self, event: dict) -> None:
        if event.get("event") == "stopped":
            self._dhcp = None
            self._pool = None
        self.emit("dhcp", event)

    def _stop_dhcp(self, args: dict) -> dict:
        server, self._dhcp = self._dhcp, None
        self._pool = None
        if server is not None:
            server.stop()
        return {}

    def _probe_dhcp(self, args: dict) -> dict:
        interface = _arg(args, "interface", str, required=True)
        result = dhcp_probe.probe_segment(interface, timeout=_arg(args, "timeout", float, 3.0))
        return {"status": result.status, "servers": result.servers, "reason": result.reason}

    def _status(self, args: dict) -> dict:
        return {
            "tftp": {"running": self._tftp is not None,
                     "transfers": list(self._tftp.transfers)[-50:] if self._tftp else []},
            "dhcp": {"running": self._dhcp is not None, **self._dhcp_info,
                     "leases": self._pool.leases() if self._pool else []},
            "syslog": {"running": self._syslog is not None},
        }


def main(
    stdin: IO[bytes],
    stdout: IO[bytes],
    argv: list[str] | None = None,
    environ: Mapping[str, str] = os.environ,
) -> int:
    return Daemon(stdout, environ).run(stdin)
