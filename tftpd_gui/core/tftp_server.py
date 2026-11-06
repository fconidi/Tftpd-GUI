"""TFTP server (RFC 1350, options per RFC 2347, 2348 and 2349).

Every file is opened through :mod:`tftpd_gui.core.safe_open`, relative to a
root directory descriptor opened once in :meth:`TftpServer.start`.
"""

from __future__ import annotations

import collections
import ctypes
import errno
import logging
import os
import platform
import re
import socket
import stat
import struct
import threading
import time
from pathlib import Path
from typing import Callable

from .errors import PortBusy
from .safe_open import open_beneath, open_leaf, open_parent, open_root
from .tftp_packet import (
    ACK,
    DATA,
    ERROR,
    RRQ,
    WRQ,
    Packet,
    TftpError,
    decode,
    encode_ack,
    encode_data,
    encode_error,
    encode_oack,
)

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 2.0
RETRIES = 5
MIN_BLKSIZE = 8
MAX_BLKSIZE = 65464
MAX_BLOCK = 65535
_POLL = 0.1
_RECV_SIZE = 65536
_MODES = ("octet", "netascii")
_NUMBER = re.compile(r"[0-9]{1,10}")
_IP_PKTINFO = getattr(socket, "IP_PKTINFO", None)
MAX_NAME_BYTES = 4096
RECORD_NAME_CHARS = 255

_NOT_DEFINED = 0
_ACCESS_VIOLATION = 2
_DISK_FULL = 3
_ILLEGAL = 4
_UNKNOWN_TID = 5
_FILE_EXISTS = 6


def max_transfer_bytes(blksize: int, max_bytes: int) -> int:
    """Largest file that fits in 65535 blocks of ``blksize``, capped by ``max_bytes``."""
    return min(max_bytes, MAX_BLOCK * blksize - 1)


_libc_lock = threading.Lock()
_libc = None


def _get_libc():
    global _libc
    with _libc_lock:
        if _libc is None:
            libc = ctypes.CDLL(None, use_errno=True)
            for fn in (libc.setfsuid, libc.setfsgid):
                fn.argtypes = [ctypes.c_uint]
                fn.restype = ctypes.c_int
            libc.syscall.restype = ctypes.c_long
            _libc = libc
        return _libc


# setgroups(2) numbers for the raw system call. glibc's setgroups() changes
# every thread of the process; the raw call changes the calling thread only.
_SYS_SETGROUPS = {"x86_64": 116, "amd64": 116, "aarch64": 159, "arm64": 159, "riscv64": 159}


def _pointer_size() -> int:
    return ctypes.sizeof(ctypes.c_void_p)


def _drop_thread_groups() -> bool:
    """Clear the calling thread's supplementary groups; True when verified."""
    # platform.machine() names the kernel's architecture; a 32-bit process on
    # a 64-bit kernel uses another system call table.
    number = _SYS_SETGROUPS.get(platform.machine().lower())
    if number is None or _pointer_size() != 8:
        return False
    libc = _get_libc()
    if libc.syscall(ctypes.c_long(number), ctypes.c_long(0), ctypes.c_void_p(None)) != 0:
        return False
    # getgroups(2) reports the calling thread's credentials.
    return os.getgroups() == []


def _set_fs_identity(uid: int | None, gid: int | None) -> bool:
    """Switch the calling thread's file-access identity and verify it took effect.

    The supplementary groups are cleared first: ``setfsuid``/``setfsgid`` leave
    them in place, and the server's own groups would otherwise still grant
    access. ``setfsuid``/``setfsgid`` act on the calling thread only on Linux.
    Passing an invalid id (``(uid_t)-1``) changes nothing and returns the
    current value.
    """
    if not _drop_thread_groups():
        return False
    libc = _get_libc()
    setfsgid, setfsuid = libc.setfsgid, libc.setfsuid
    query = 0xFFFFFFFF
    if gid is not None:
        setfsgid(gid)
        if setfsgid(query) != gid:
            return False
    if uid is not None:
        setfsuid(uid)
        if setfsuid(query) != uid:
            return False
    return True


def _number(value: str) -> int | None:
    if _NUMBER.fullmatch(value):
        return int(value)
    return None


def _netascii_encode(data: bytes) -> bytes:
    # CR becomes CR NUL first, so the CR added for each LF is left alone.
    return data.replace(b"\r", b"\r\x00").replace(b"\n", b"\r\n")


class _NetasciiDecoder:
    """Wire to local: CR LF becomes LF, CR NUL becomes CR, a lone CR is kept."""

    _PAIR = re.compile(rb"\r[\n\x00]")

    def __init__(self) -> None:
        self._pending_cr = False

    def feed(self, data: bytes) -> bytes:
        if self._pending_cr:
            data = b"\r" + data
            self._pending_cr = False
        if data.endswith(b"\r"):
            data = data[:-1]
            self._pending_cr = True
        return self._PAIR.sub(lambda m: b"\n" if m.group() == b"\r\n" else b"\r", data)

    def finish(self) -> bytes:
        if self._pending_cr:
            self._pending_cr = False
            return b"\r"
        return b""


class _Ended(Exception):
    """The transfer ended without an error packet to send."""

    def __init__(self, result: str) -> None:
        super().__init__(result)
        self.result = result


class _Channel:
    """One transfer's socket, peer and retransmission state."""

    def __init__(self, sock: socket.socket, peer, idle_seconds: float, stopping: threading.Event):
        self.sock = sock
        self.peer = peer
        self.idle_seconds = idle_seconds
        self.stopping = stopping
        self.timeout = DEFAULT_TIMEOUT
        self.last_activity = time.monotonic()

    def send(self, raw: bytes) -> None:
        self.sock.sendto(raw, self.peer)

    def error(self, code: int, message: str) -> None:
        try:
            self.send(encode_error(code, message))
        except OSError:
            pass

    def exchange(
        self,
        raw: bytes,
        wanted: Callable[[Packet], bool],
        resend: Callable[[Packet], bool] = lambda p: False,
    ) -> Packet:
        """Send ``raw`` and wait for a wanted packet, retransmitting on timeout."""
        retries = 0
        self.send(raw)
        deadline = time.monotonic() + self.timeout
        while True:
            if self.stopping.is_set():
                raise _Ended("error")
            now = time.monotonic()
            idle_end = self.last_activity + self.idle_seconds
            if now >= idle_end:
                raise _Ended("timeout")
            if now >= deadline:
                if retries >= RETRIES:
                    raise _Ended("timeout")
                retries += 1
                self.send(raw)
                deadline = now + self.timeout
                continue
            self.sock.settimeout(max(0.001, min(deadline, idle_end, now + _POLL) - now))
            try:
                data, addr = self.sock.recvfrom(_RECV_SIZE)
            except socket.timeout:
                continue
            if addr != self.peer:
                try:
                    self.sock.sendto(encode_error(_UNKNOWN_TID, "unknown transfer ID"), addr)
                except OSError:
                    pass
                continue
            try:
                packet = decode(data)
            except TftpError:
                self.error(_ILLEGAL, "illegal operation")
                raise _Ended("error") from None
            if packet.opcode == ERROR:
                raise _Ended("error")
            if wanted(packet):
                self.last_activity = time.monotonic()
                return packet
            if resend(packet):
                self.send(raw)
            # Anything else, such as a duplicate ACK, is ignored: answering it
            # would start the "Sorcerer's Apprentice" duplicate-packet storm.


class TftpServer:
    def __init__(
        self,
        root: Path,
        *,
        port: int = 69,
        bind_device: str | None = None,
        allow_write: bool = False,
        allow_overwrite: bool = False,
        max_bytes: int = 256 * 1024 ** 2,
        max_transfers: int = 8,
        idle_seconds: float = 30.0,
        file_uid: int | None = None,
        file_gid: int | None = None,
        on_event: Callable[[dict], None] | None = None,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        max_records: int = 1000,
    ) -> None:
        if (file_uid is None) != (file_gid is None):
            raise ValueError("file_uid and file_gid must be given together")
        self.root = Path(root)
        self.port = port
        self.bind_device = bind_device
        self.allow_write = allow_write
        self.allow_overwrite = allow_overwrite
        self.max_bytes = max_bytes
        self.max_transfers = max_transfers
        self.idle_seconds = idle_seconds
        self.file_uid = file_uid
        self.file_gid = file_gid
        self.on_event = on_event
        self.socket_factory = socket_factory
        self.transfers: collections.deque[dict] = collections.deque(maxlen=max_records)
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._sock: socket.socket | None = None
        self._root_fd: int | None = None
        self._listener: threading.Thread | None = None
        self._threads: set[threading.Thread] = set()
        self._active = 0
        self._close_root_when_idle = False

    # ----------------------------------------------------------- lifecycle

    def _new_socket(self) -> socket.socket:
        sock = self.socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if self.bind_device:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.bind_device.encode())
        except BaseException:
            sock.close()
            raise
        return sock

    def start(self) -> int:
        with self._lock:
            if self._sock is not None:
                raise RuntimeError("server already running")
            if self._active or self._root_fd is not None:
                raise RuntimeError("previous transfers are still running")
        self._stopping.clear()
        root_fd = self._open_root()
        try:
            sock = self._new_socket()
            try:
                if _IP_PKTINFO is not None:
                    sock.setsockopt(socket.IPPROTO_IP, _IP_PKTINFO, 1)
                sock.bind(("0.0.0.0", self.port))
            except OSError as exc:
                sock.close()
                if exc.errno == errno.EADDRINUSE:
                    raise PortBusy(self.port) from exc
                raise
        except BaseException:
            os.close(root_fd)
            raise
        with self._lock:
            self._root_fd = root_fd
            self._close_root_when_idle = False
            self._sock = sock
        self._listener = threading.Thread(target=self._serve, args=(sock, root_fd),
                                          name="tftp-listener", daemon=True)
        self._listener.start()
        return sock.getsockname()[1]

    def _open_root(self) -> int:
        """Open the root, under the file identity when one is configured.

        With an identity, the root's ancestors are checked with the user's
        permissions too: the open runs in a short-lived thread that switches
        its own file identity and ends.
        """
        if self.file_uid is None:
            return open_root(self.root)
        outcome: dict = {}

        def opener() -> None:
            try:
                if not _set_fs_identity(self.file_uid, self.file_gid):
                    raise PermissionError("cannot switch file identity")
                outcome["fd"] = open_root(self.root)
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=opener, name="tftp-open-root", daemon=True)
        thread.start()
        thread.join()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["fd"]

    def stop(self) -> None:
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.join(2.0)
        with self._lock:
            sock, self._sock = self._sock, None
            threads = list(self._threads)
        if sock is not None:
            sock.close()
        deadline = time.monotonic() + 2.0
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            if self._root_fd is not None:
                if self._active == 0:
                    os.close(self._root_fd)
                    self._root_fd = None
                else:
                    # A transfer still holds the descriptor: closing it now
                    # could let a recycled fd number stand in for the root.
                    self._close_root_when_idle = True

    # ------------------------------------------------------------ listener

    def _emit(self, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:
            _log.exception("TFTP event handler failed")

    def _receive_request(self, sock: socket.socket):
        if _IP_PKTINFO is None:
            data, peer = sock.recvfrom(_RECV_SIZE)
            return data, peer, "0.0.0.0"
        data, ancdata, _flags, peer = sock.recvmsg(_RECV_SIZE, socket.CMSG_SPACE(12))
        local = "0.0.0.0"
        for level, kind, value in ancdata:
            if level == socket.IPPROTO_IP and kind == _IP_PKTINFO and len(value) >= 12:
                _ifindex, spec_dst, _dst = struct.unpack("i4s4s", value[:12])
                local = socket.inet_ntoa(spec_dst)
        return data, peer, local

    def _serve(self, sock: socket.socket, root_fd: int) -> None:
        sock.settimeout(_POLL)
        while not self._stopping.is_set():
            try:
                data, peer, local = self._receive_request(sock)
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    break
                continue
            try:
                self._dispatch(sock, root_fd, data, peer, local)
            except Exception as exc:
                _log.exception("TFTP request dispatch failed")
                self._emit({"event": "error", "error": type(exc).__name__})

    def _dispatch(self, sock, root_fd, data, peer, local) -> None:
        if data[:2] in (b"\x00\x01", b"\x00\x02"):
            raw_name = data[2:2 + MAX_NAME_BYTES + 1].split(b"\x00", 1)[0]
            if len(raw_name) > MAX_NAME_BYTES:
                self._reply_error(sock, peer, _ACCESS_VIOLATION, "file name too long")
                return
        try:
            packet = decode(data)
        except TftpError:
            self._reply_error(sock, peer, _ILLEGAL, "illegal operation")
            return
        if packet.opcode not in (RRQ, WRQ):
            if packet.opcode != ERROR:
                self._reply_error(sock, peer, _ILLEGAL, "illegal operation")
            return
        with self._lock:
            busy = self._active >= self.max_transfers
            if not busy:
                self._active += 1
                # A fresh thread per transfer: the filesystem identity set in
                # it never carries over to another transfer.
                thread = threading.Thread(target=self._run_transfer,
                                          args=(root_fd, packet, peer, local),
                                          name="tftp-transfer", daemon=True)
                self._threads.add(thread)
        if busy:
            self._reply_error(sock, peer, _NOT_DEFINED, "server busy")
            self._emit({"event": "busy", "peer": f"{peer[0]}:{peer[1]}", "filename": packet.filename[:RECORD_NAME_CHARS]})
            return
        try:
            thread.start()
        except RuntimeError:
            self._release(thread)
            self._reply_error(sock, peer, _NOT_DEFINED, "server busy")

    @staticmethod
    def _reply_error(sock, peer, code: int, message: str) -> None:
        try:
            sock.sendto(encode_error(code, message), peer)
        except OSError:
            pass

    def _release(self, thread: threading.Thread) -> None:
        with self._lock:
            self._active -= 1
            self._threads.discard(thread)
            if self._active == 0 and self._close_root_when_idle and self._root_fd is not None:
                os.close(self._root_fd)
                self._root_fd = None
                self._close_root_when_idle = False

    # ------------------------------------------------------------ transfer

    def _run_transfer(self, root_fd: int, packet: Packet, peer, local: str) -> None:
        started = time.monotonic()
        record = {
            "peer": f"{peer[0]}:{peer[1]}",
            "filename": packet.filename[:RECORD_NAME_CHARS],
            "direction": "read" if packet.opcode == RRQ else "write",
            "bytes": 0,
            "seconds": 0.0,
            "result": "error",
        }
        sock = None
        channel = None
        try:
            sock = self._new_socket()
            try:
                sock.bind((local, 0))
            except OSError:
                sock.bind(("0.0.0.0", 0))
            channel = _Channel(sock, peer, self.idle_seconds, self._stopping)
            if self.file_uid is not None or self.file_gid is not None:
                if not _set_fs_identity(self.file_uid, self.file_gid):
                    raise TftpError(_ACCESS_VIOLATION, "cannot switch file identity")
            if not packet.filename:
                raise TftpError(_ILLEGAL, "empty file name")
            if packet.mode not in _MODES:
                raise TftpError(_ILLEGAL, "unsupported transfer mode")
            if packet.opcode == RRQ:
                self._serve_read(channel, root_fd, packet, record)
            else:
                self._serve_write(channel, root_fd, packet, record)
            record["result"] = "ok"
        except TftpError as exc:
            if channel is not None:
                channel.error(exc.code, exc.message or "error")
        except _Ended as ended:
            record["result"] = ended.result
        except Exception:
            _log.exception("TFTP transfer failed")
            if channel is not None:
                channel.error(_NOT_DEFINED, "internal error")
        finally:
            if sock is not None:
                sock.close()
            record["seconds"] = time.monotonic() - started
            with self._lock:
                self.transfers.append(record)
            self._emit({"event": "transfer", **record})
            self._release(threading.current_thread())

    def _negotiate(self, channel: _Channel, options: dict[str, str]) -> tuple[int, dict[str, str]]:
        accepted: dict[str, str] = {}
        blksize = 512
        if "blksize" in options:
            value = _number(options["blksize"])
            if value is not None and value >= MIN_BLKSIZE:
                blksize = min(value, MAX_BLKSIZE)
                accepted["blksize"] = str(blksize)
        if "timeout" in options:
            value = _number(options["timeout"])
            if value is not None and 1 <= value <= 255:
                channel.timeout = float(value)
                accepted["timeout"] = str(value)
        return blksize, accepted

    def _serve_read(self, channel: _Channel, root_fd: int, packet: Packet, record: dict) -> None:
        blksize, oack = self._negotiate(channel, packet.options)
        netascii = packet.mode == "netascii"
        limit = max_transfer_bytes(blksize, self.max_bytes)
        fd = open_beneath(root_fd, packet.filename, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise TftpError(_ACCESS_VIOLATION, "not a regular file")
            if info.st_size > limit:
                raise TftpError(_DISK_FULL, "file exceeds the transfer size limit")
            # The netascii size on the wire is not known in advance: tsize is
            # only answered for octet transfers.
            if "tsize" in packet.options and not netascii:
                oack["tsize"] = str(info.st_size)
            if oack:
                channel.exchange(encode_oack(oack), lambda p: p.opcode == ACK and p.block == 0)
            block = 1
            sent = 0
            buffer = b""
            eof = False
            while True:
                while len(buffer) < blksize and not eof:
                    chunk = os.read(fd, blksize)
                    if not chunk:
                        eof = True
                    else:
                        buffer += _netascii_encode(chunk) if netascii else chunk
                payload, buffer = buffer[:blksize], buffer[blksize:]
                if block > MAX_BLOCK or sent + len(payload) > limit:
                    raise TftpError(_DISK_FULL, "file exceeds the transfer size limit")
                channel.exchange(encode_data(block, payload),
                                 lambda p: p.opcode == ACK and p.block == block)
                sent += len(payload)
                record["bytes"] = sent
                if len(payload) < blksize:
                    return
                block += 1
        finally:
            os.close(fd)

    def _serve_write(self, channel: _Channel, root_fd: int, packet: Packet, record: dict) -> None:
        if not self.allow_write:
            raise TftpError(_ACCESS_VIOLATION, "uploads are disabled")
        blksize, oack = self._negotiate(channel, packet.options)
        limit = max_transfer_bytes(blksize, self.max_bytes)
        if "tsize" in packet.options:
            size = _number(packet.options["tsize"])
            if size is not None:
                if size > limit:
                    raise TftpError(_DISK_FULL, "file exceeds the transfer size limit")
                oack["tsize"] = str(size)
        dir_fd, leaf = open_parent(root_fd, packet.filename)
        try:
            if self.allow_overwrite:
                try:
                    existing = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    existing = None
                except OSError:
                    raise TftpError(_ACCESS_VIOLATION, "access violation") from None
                if existing is not None and not stat.S_ISREG(existing.st_mode):
                    raise TftpError(_ACCESS_VIOLATION, "not a regular file")
                target = f".{leaf}.part"
            else:
                target = leaf
            try:
                fd = open_leaf(dir_fd, target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOCTTY, 0o644)
            except TftpError as exc:
                if exc.code == _FILE_EXISTS and self.allow_overwrite:
                    raise TftpError(_FILE_EXISTS, "an upload of this file is in progress") from None
                raise
            complete = False
            try:
                first = encode_oack(oack) if oack else encode_ack(0)
                last_block = self._receive(channel, fd, first, blksize, limit,
                                           packet.mode == "netascii", record)
                os.fsync(fd)
                complete = True
            finally:
                os.close(fd)
                if not complete:
                    self._unlink(dir_fd, target)
            if self.allow_overwrite:
                try:
                    os.rename(target, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                except OSError:
                    self._unlink(dir_fd, target)
                    raise TftpError(_ACCESS_VIOLATION, "cannot replace the file") from None
            final_ack = encode_ack(last_block)
            channel.send(final_ack)
            self._wait_final_retransmission(channel, last_block, final_ack)
        finally:
            os.close(dir_fd)

    @staticmethod
    def _wait_final_retransmission(channel: _Channel, block: int, ack: bytes) -> None:
        """Keep the transfer socket alive for a lost final ACK."""
        deadline = time.monotonic() + min(channel.timeout + _POLL * 4, channel.idle_seconds)
        while not channel.stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            channel.sock.settimeout(min(_POLL, remaining))
            try:
                data, addr = channel.sock.recvfrom(_RECV_SIZE)
            except socket.timeout:
                continue
            except OSError:
                return
            if addr != channel.peer:
                try:
                    channel.sock.sendto(encode_error(_UNKNOWN_TID, "unknown transfer ID"), addr)
                except OSError:
                    pass
                continue
            try:
                packet = decode(data)
            except TftpError:
                continue
            if packet.opcode == DATA and packet.block == block:
                try:
                    channel.send(ack)
                except OSError:
                    return

    @staticmethod
    def _unlink(dir_fd: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except OSError:
            pass

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except OSError as exc:
                if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise TftpError(_DISK_FULL, "disk full") from None
                raise TftpError(_NOT_DEFINED, "write failed") from None
            view = view[written:]

    def _receive(self, channel, fd, first, blksize, limit, netascii, record) -> int:
        """Receive DATA blocks into ``fd``; return the last block number, not yet ACKed."""
        decoder = _NetasciiDecoder() if netascii else None
        expected = 1
        total = 0
        raw = first
        while True:
            packet = channel.exchange(
                raw,
                lambda p: p.opcode == DATA and p.block == expected,
                lambda p: p.opcode == DATA and p.block == expected - 1,
            )
            payload = packet.payload
            if len(payload) > blksize:
                raise TftpError(_ILLEGAL, "data block larger than the block size")
            total += len(payload)
            if total > limit:
                raise TftpError(_DISK_FULL, "file exceeds the transfer size limit")
            self._write_all(fd, decoder.feed(payload) if decoder else payload)
            record["bytes"] = total
            if len(payload) < blksize:
                if decoder:
                    self._write_all(fd, decoder.finish())
                return expected
            if expected >= MAX_BLOCK:
                raise TftpError(_DISK_FULL, "file exceeds the transfer size limit")
            raw = encode_ack(expected)
            expected += 1
