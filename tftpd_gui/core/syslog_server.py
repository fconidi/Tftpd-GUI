"""Syslog collector: UDP and optionally TCP (RFC 3164, RFC 5424, RFC 6587).

Senders are unauthenticated, so every buffer is bounded: the message ring,
the length of one message, the number of TCP connections and the time a
connection may stay idle.
"""

from __future__ import annotations

import collections
import errno
import logging
import os
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from .errors import PortBusy

_log = logging.getLogger(__name__)

MAX_TCP_CONNECTIONS = 32
TCP_IDLE_SECONDS = 60.0
_POLL = 0.1
_MAX_LENGTH_DIGITS = 7
_PORT_RETRIES = 5

_PRI = re.compile(r"<(\d{1,3})>")
_RFC3164 = re.compile(
    r"[*.]?(?:\d+: )?[*.]?"
    r"(?P<ts>[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d(?:\.\d+)?):? "
    r"(?:(?P<host>[^\s:]+) )?"
    r"(?P<tag>[^\s:\[]+(?:\[\d+\])?):\s?(?P<msg>.*)",
    re.DOTALL,
)
_TOKEN = re.compile(r"(\S+) ?")


def _nil(value: str) -> str | None:
    return None if value == "-" else value


def _skip_structured_data(text: str) -> str:
    """Return ``text`` without a leading structured-data field."""
    if text.startswith("-"):
        return text[1:].lstrip(" ") if text[1:2] in ("", " ") else text
    pos = 0
    while text.startswith("[", pos):
        i = pos + 1
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == "]":
                break
            i += 1
        else:
            return ""  # unterminated element: nothing usable follows
        pos = i + 1
    return text[pos:].lstrip(" ")


def _parse_5424(rest: str, result: dict) -> bool:
    pos = 0
    fields = []
    for _ in range(5):
        match = _TOKEN.match(rest, pos)
        if match is None:
            return False
        fields.append(match.group(1))
        pos = match.end()
    after = _skip_structured_data(rest[pos:])
    result["timestamp"] = _nil(fields[0])
    result["host"] = _nil(fields[1])
    result["tag"] = _nil(fields[2])
    result["message"] = after
    return True


def parse_line(raw: bytes) -> dict:
    """Split one syslog line; never raises."""
    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
    result = {"facility": None, "severity": None, "timestamp": None, "host": None,
              "tag": None, "message": text, "raw": text}
    match = _PRI.match(text)
    if match is None or int(match.group(1)) > 191:
        return result
    value = int(match.group(1))
    result["facility"], result["severity"] = value >> 3, value & 7
    rest = text[match.end():]
    result["message"] = rest
    if rest.startswith("1 "):
        _parse_5424(rest[2:], result)
        return result
    found = _RFC3164.fullmatch(rest)
    if found is not None:
        result["timestamp"] = found.group("ts")
        result["host"] = found.group("host")
        result["tag"] = found.group("tag")
        result["message"] = found.group("msg")
    return result


class _Close(Exception):
    """The peer sent something the framing cannot accept."""


class _Framer:
    """Turns a TCP byte stream into messages, bounded by ``max_line``."""

    def __init__(self, max_line: int) -> None:
        self.max_line = max_line
        self.mode: str | None = None
        self._line = bytearray()
        self._discard = False
        self._digits = bytearray()
        self._need = -1  # octet counting: bytes still missing, -1 while reading the header
        self._body = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        if self.mode is None and data:
            self.mode = "octet" if data[:1].isdigit() else "lf"
        if self.mode == "lf":
            self._feed_lf(data, out)
        else:
            self._feed_octet(data, out)
        return out

    def _feed_lf(self, data: bytes, out: list[bytes]) -> None:
        start = 0
        while start < len(data):
            end = data.find(b"\n", start)
            chunk = data[start:end if end >= 0 else len(data)]
            if not self._discard:
                room = self.max_line - len(self._line)
                self._line += chunk[:max(room, 0)]
                if len(chunk) > room:
                    self._discard = True
            if end < 0:
                return
            line = bytes(self._line)
            self._line.clear()
            self._discard = False
            if line.endswith(b"\r"):
                line = line[:-1]
            if line:
                out.append(line)
            start = end + 1

    def _feed_octet(self, data: bytes, out: list[bytes]) -> None:
        pos = 0
        while pos < len(data):
            if self._need < 0:
                byte = data[pos:pos + 1]
                pos += 1
                if byte.isdigit():
                    self._digits += byte
                    if len(self._digits) > _MAX_LENGTH_DIGITS:
                        raise _Close
                elif byte == b" " and self._digits:
                    length = int(self._digits)
                    self._digits.clear()
                    if length > self.max_line:
                        raise _Close
                    self._need = length
                    self._body.clear()
                    if length == 0:
                        self._need = -1
                else:
                    raise _Close
            else:
                take = data[pos:pos + self._need]
                self._body += take
                pos += len(take)
                self._need -= len(take)
                if self._need == 0:
                    out.append(bytes(self._body))
                    self._body.clear()
                    self._need = -1


class SyslogCollector:
    """Receives syslog over UDP (and TCP) and keeps the latest messages.

    ``on_message`` runs on the receiving thread after the message is stored.
    It must return quickly: a slow callback delays every later message.
    Exceptions it raises are swallowed.
    """

    def __init__(
        self,
        *,
        port: int = 514,
        bind_device: str | None = None,
        tcp: bool = False,
        capacity: int = 10000,
        max_line: int = 65536,
        on_message: Callable[[dict], None] | None = None,
    ) -> None:
        self.port = port
        self.bind_device = bind_device
        self.tcp = tcp
        self.capacity = capacity
        self.max_line = max_line
        self.on_message = on_message
        self._lock = threading.Lock()
        self._messages: collections.deque = collections.deque(maxlen=capacity)
        self._stopping = threading.Event()
        self._udp: socket.socket | None = None
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._conns: dict[socket.socket, threading.Thread] = {}

    # -- lifecycle -------------------------------------------------------

    def _socket(self, kind: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, kind)
        try:
            if self.bind_device:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.bind_device.encode())
            if kind == socket.SOCK_STREAM:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except BaseException:
            sock.close()
            raise
        return sock

    def _bind(self, kind: int, port: int) -> socket.socket:
        sock = self._socket(kind)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE:
                raise PortBusy(port) from exc
            raise
        except BaseException:
            sock.close()
            raise
        return sock

    def _bind_pair(self) -> tuple[socket.socket, socket.socket | None]:
        attempts = _PORT_RETRIES if (self.tcp and self.port == 0) else 1
        for attempt in range(attempts):
            udp = self._bind(socket.SOCK_DGRAM, self.port)
            if not self.tcp:
                return udp, None
            try:
                listener = self._bind(socket.SOCK_STREAM, udp.getsockname()[1])
                listener.listen(socket.SOMAXCONN)
                return udp, listener
            except PortBusy as exc:
                udp.close()
                if attempt == attempts - 1:
                    raise PortBusy(exc.port) from None
            except BaseException:
                udp.close()
                raise
        raise PortBusy(self.port)  # pragma: no cover

    def start(self) -> int:
        with self._lock:
            if self._udp is not None:
                raise RuntimeError("collector already running")
        udp, listener = self._bind_pair()
        udp.settimeout(_POLL)
        self._stopping.clear()
        threads = [threading.Thread(target=self._serve_udp, args=(udp,),
                                    name="syslog-udp", daemon=True)]
        if listener is not None:
            listener.settimeout(_POLL)
            threads.append(threading.Thread(target=self._serve_tcp, args=(listener,),
                                            name="syslog-tcp", daemon=True))
        with self._lock:
            self._udp, self._listener, self._threads = udp, listener, threads
        for thread in threads:
            thread.start()
        return udp.getsockname()[1]

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            udp, self._udp = self._udp, None
            listener, self._listener = self._listener, None
            threads, self._threads = self._threads, []
            conns = dict(self._conns)
        deadline = time.monotonic() + 2.0
        for thread in threads + list(conns.values()):
            thread.join(max(0.0, deadline - time.monotonic()))
        for sock in (udp, listener, *conns):
            if sock is not None:
                sock.close()
        with self._lock:
            self._conns.clear()

    # -- storage ---------------------------------------------------------

    def _ingest(self, raw: bytes, source: str) -> None:
        message = parse_line(raw)
        message["received"] = time.time()
        message["source"] = source
        with self._lock:
            self._messages.append(message)
        callback = self.on_message
        if callback is not None:
            try:
                callback(dict(message))
            except Exception as exc:
                _log.warning("syslog callback failed: %s", type(exc).__name__)

    def messages(self, *, text: str | None = None, min_severity: int | None = None,
                 host: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._messages)
        needle = text.lower() if text else None
        wanted = host.lower() if host else None
        out = []
        for item in items:
            if needle is not None and needle not in item["raw"].lower():
                continue
            if min_severity is not None:
                severity = item["severity"]
                if severity is None or severity > min_severity:
                    continue
            if wanted is not None and (item["host"] or item["source"]).lower() != wanted:
                continue
            out.append(item)
        return out

    # -- receivers -------------------------------------------------------

    def _serve_udp(self, sock: socket.socket) -> None:
        while not self._stopping.is_set():
            try:
                data, addr = sock.recvfrom(self.max_line + 1)
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                time.sleep(_POLL)
                continue
            try:
                if data:
                    self._ingest(data[:self.max_line], addr[0])
            except Exception as exc:
                _log.warning("syslog datagram dropped: %s", type(exc).__name__)

    def _serve_tcp(self, listener: socket.socket) -> None:
        while not self._stopping.is_set():
            try:
                conn, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                time.sleep(_POLL)
                continue
            try:
                with self._lock:
                    self._conns = {c: t for c, t in self._conns.items() if t.is_alive()}
                    full = len(self._conns) >= MAX_TCP_CONNECTIONS
                    if not full:
                        thread = threading.Thread(target=self._serve_conn, args=(conn, addr[0]),
                                                  name="syslog-conn", daemon=True)
                        self._conns[conn] = thread
                if full:
                    conn.close()
                else:
                    thread.start()
            except BaseException as exc:
                conn.close()
                _log.warning("syslog connection refused: %s", type(exc).__name__)

    def _serve_conn(self, conn: socket.socket, source: str) -> None:
        framer = _Framer(self.max_line)
        last = time.monotonic()
        try:
            conn.settimeout(_POLL)
            while not self._stopping.is_set():
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    if time.monotonic() - last >= TCP_IDLE_SECONDS:
                        return
                    continue
                if not data:
                    return
                last = time.monotonic()
                for line in framer.feed(data):
                    self._ingest(line, source)
        except (_Close, OSError):
            pass
        except Exception as exc:
            _log.warning("syslog connection failed: %s", type(exc).__name__)
        finally:
            conn.close()
            with self._lock:
                self._conns.pop(conn, None)


def save_messages(messages: list[dict], path: Path) -> int:
    """Write the ``raw`` text of each message to ``path``, one per line.

    The data goes to a temporary file in the same directory and replaces
    ``path`` atomically; a symlink at ``path`` is replaced, not followed.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent or ".", prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for message in messages:
                handle.write(message["raw"] + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return len(messages)
