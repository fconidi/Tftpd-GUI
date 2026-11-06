"""Blocking TFTP client (RFC 1350 with the RFC 2347/2348/2349 options)."""

from __future__ import annotations

import errno
import os
import socket
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable

from .tftp_packet import (
    ACK,
    DATA,
    ERROR,
    OACK,
    RRQ,
    WRQ,
    Packet,
    TftpError,
    decode,
    encode_ack,
    encode_data,
    encode_error,
    encode_request,
)
from .tftp_server import MAX_BLOCK, max_transfer_bytes

DEFAULT_BLKSIZE = 512
MIN_BLKSIZE = 8
_POLL = 0.25  # longest single wait, so a cancel request is seen promptly


def _check_name(filename: str) -> None:
    if not filename or "\0" in filename:
        raise ValueError("invalid remote file name")


def _resolve(host: str, port: int) -> tuple[str, int]:
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
    if not infos:
        raise OSError(errno.EHOSTUNREACH, "host not found", host)
    return infos[0][4][0], infos[0][4][1]


class _Session:
    """One transfer: socket, peer transfer ID and retransmission handling."""

    def __init__(self, sock, server, timeout, retries, should_cancel):
        self.sock = sock
        self.server = server
        self.tid: tuple[str, int] | None = None
        self.timeout = timeout
        self.retries = retries
        self.should_cancel = should_cancel

    def _peer(self):
        return self.tid or self.server

    def send_error(self, code: int, message: str) -> None:
        try:
            self.sock.sendto(encode_error(code, message), self._peer())
        except OSError:
            pass

    def _check_cancel(self) -> None:
        if self.should_cancel is not None and self.should_cancel():
            self.send_error(0, "cancelled")
            raise InterruptedError("transfer cancelled")

    def wait(self, raw: bytes, accept: Callable[[Packet], bool], dup_block: int | None = None) -> Packet:
        """Send ``raw`` and return the first acceptable reply, retransmitting on timeout.

        A DATA packet numbered ``dup_block`` is a retransmission by the peer:
        ``raw`` (our last ACK) is sent again.
        """
        attempts = 0
        self._check_cancel()
        self.sock.sendto(raw, self._peer())
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempts += 1
                if attempts > self.retries:
                    raise TimeoutError("no answer from the TFTP server")
                self._check_cancel()
                self.sock.sendto(raw, self._peer())
                deadline = time.monotonic() + self.timeout
                continue
            self.sock.settimeout(min(remaining, _POLL))
            try:
                data, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                self._check_cancel()
                continue
            self._check_cancel()
            if self.tid is None:
                if addr[0] != self.server[0]:
                    continue
            elif addr != self.tid:
                try:
                    self.sock.sendto(encode_error(5, "unknown transfer ID"), addr)
                except OSError:
                    pass
                continue
            try:
                packet = decode(data)
            except TftpError:
                continue
            if packet.opcode == ERROR:
                raise TftpError(packet.error_code or 0, packet.message or "")
            if accept(packet):
                self.tid = addr
                return packet
            if self.tid is not None and dup_block is not None and packet.opcode == DATA and packet.block == dup_block:
                self.sock.sendto(raw, self._peer())

    def negotiate(self, packet: Packet, requested: int) -> int:
        """Return the block size from an OACK, aborting on a protocol violation."""
        value = packet.options.get("blksize")
        if value is None:
            return DEFAULT_BLKSIZE
        try:
            size = int(value)
        except ValueError:
            size = -1
        if not MIN_BLKSIZE <= size <= requested:
            self.send_error(8, "option negotiation failed")
            raise TftpError(8, "server answered with an invalid blksize")
        return size


def _block_limit_reached(block: int) -> bool:
    """True when a full block numbered ``block`` leaves no number for the next one."""
    return block >= MAX_BLOCK


def _options(blksize: int, tsize: int) -> dict[str, str]:
    if blksize == DEFAULT_BLKSIZE:
        return {}
    if not MIN_BLKSIZE <= blksize <= 65464:
        raise ValueError("blksize must be between 8 and 65464")
    return {"blksize": str(blksize), "tsize": str(tsize)}


def get(
    host: str,
    filename: str,
    dest: Path,
    *,
    port: int = 69,
    blksize: int = DEFAULT_BLKSIZE,
    timeout: float = 3.0,
    retries: int = 5,
    should_cancel: Callable[[], bool] | None = None,
) -> int:
    """Download ``filename`` from ``host`` into ``dest``; return the bytes written."""
    _check_name(filename)
    options = _options(blksize, 0)
    dest = Path(dest)
    if dest.is_dir():
        raise IsADirectoryError(errno.EISDIR, "destination is a directory", str(dest))
    server = _resolve(host, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fd = -1
    tmp_name = None
    done = False
    try:
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".tftp-", suffix=".part")
        out = os.fdopen(fd, "wb")
        fd = -1
        with out:
            sock.bind(("0.0.0.0", 0))
            session = _Session(sock, server, timeout, retries, should_cancel)
            request = encode_request(RRQ, filename, "octet", options)

            def first(p: Packet) -> bool:
                return (p.opcode == DATA and p.block == 1) or (p.opcode == OACK and bool(options))

            packet = session.wait(request, first)
            size = DEFAULT_BLKSIZE
            if packet.opcode == OACK:
                size = session.negotiate(packet, blksize)
                packet = session.wait(encode_ack(0), lambda p: p.opcode == DATA and p.block == 1)
            total = 0
            block = 1
            while True:
                payload = packet.payload or b""
                if len(payload) > size:
                    session.send_error(4, "data block larger than the block size")
                    raise TftpError(4, "data block larger than the block size")
                out.write(payload)
                total += len(payload)
                ack = encode_ack(block)
                if len(payload) < size:
                    sock.sendto(ack, session.tid)
                    break
                if _block_limit_reached(block):
                    session.send_error(3, "file too large for 65535 blocks")
                    raise TftpError(3, "file too large for 65535 blocks")
                block += 1
                if block > MAX_BLOCK:
                    session.send_error(3, "file too large for 65535 blocks")
                    raise TftpError(3, "file too large for 65535 blocks")
                want = block
                packet = session.wait(
                    ack, lambda p, want=want: p.opcode == DATA and p.block == want, dup_block=block - 1
                )
        os.replace(tmp_name, dest)
        done = True
        return total
    finally:
        sock.close()
        if fd >= 0:
            os.close(fd)
        if not done and tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def put(
    host: str,
    source: Path,
    filename: str,
    *,
    port: int = 69,
    blksize: int = DEFAULT_BLKSIZE,
    timeout: float = 3.0,
    retries: int = 5,
    should_cancel: Callable[[], bool] | None = None,
) -> int:
    """Upload ``source`` to ``host`` as ``filename``; return the bytes sent."""
    _check_name(filename)

    source = Path(source)
    info = os.stat(source)
    if not stat.S_ISREG(info.st_mode):
        if stat.S_ISDIR(info.st_mode):
            raise IsADirectoryError(errno.EISDIR, "source is a directory", str(source))
        raise OSError(errno.EINVAL, "source is not a regular file", str(source))
    size_total = info.st_size
    options = _options(blksize, size_total)
    if size_total > max_transfer_bytes(blksize, 2 ** 63):
        raise TftpError(3, "file too large for 65535 blocks")
    server = _resolve(host, port)
    with open(source, "rb") as src:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", 0))
            session = _Session(sock, server, timeout, retries, should_cancel)
            request = encode_request(WRQ, filename, "octet", options)

            def first(p: Packet) -> bool:
                return (p.opcode == ACK and p.block == 0) or (p.opcode == OACK and bool(options))

            packet = session.wait(request, first)
            size = DEFAULT_BLKSIZE
            if packet.opcode == OACK:
                size = session.negotiate(packet, blksize)
            if size_total > max_transfer_bytes(size, 2 ** 63):
                session.send_error(3, "file too large for 65535 blocks")
                raise TftpError(3, "file too large for 65535 blocks")
            sent = 0
            block = 1
            while True:
                if block > MAX_BLOCK:
                    session.send_error(3, "file too large for 65535 blocks")
                    raise TftpError(3, "file too large for 65535 blocks")
                chunk = src.read(size)
                raw = encode_data(block, chunk)
                session.wait(raw, lambda p, want=block: p.opcode == ACK and p.block == want)
                sent += len(chunk)
                if len(chunk) < size:
                    return sent
                block += 1
        finally:
            sock.close()
