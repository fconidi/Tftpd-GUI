import errno
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tftpd_gui.core import tftp_client
from tftpd_gui.core.tftp_client import get, put
from tftpd_gui.core.tftp_packet import (
    ACK,
    DATA,
    ERROR,
    OACK,
    RRQ,
    WRQ,
    TftpError,
    decode,
    encode_ack,
    encode_data,
    encode_error,
    encode_oack,
)
from tftpd_gui.core.tftp_server import TftpServer


def free_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


class Stub:
    """A hand-written UDP peer: ``main`` takes the request, ``xfer`` answers it."""

    def __init__(self, script, xfer_ip="127.0.0.1"):
        self.main = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.main.bind(("127.0.0.1", 0))
        self.xfer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.xfer.bind((xfer_ip, 0))
        self.port = self.main.getsockname()[1]
        self.log = []
        self.client = None
        self.failure = None
        self._script = script
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._script(self)
        except BaseException as exc:  # reported by close()
            self.failure = exc

    def recv(self, sock=None, timeout=2.0):
        sock = sock or self.xfer
        sock.settimeout(timeout)
        data, addr = sock.recvfrom(70000)
        packet = decode(data)
        self.client = addr
        self.log.append(packet)
        return packet

    def send(self, raw, sock=None):
        (sock or self.xfer).sendto(raw, self.client)

    def recv_request(self):
        return self.recv(self.main)

    def close(self):
        self._thread.join(5)
        alive = self._thread.is_alive()
        self.main.close()
        self.xfer.close()
        assert not alive, "stub thread did not finish"
        if self.failure is not None:
            raise self.failure


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.server_root = self.tmp / "srv"
        self.server_root.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.stubs = []

    def tearDown(self):
        try:
            for stub in self.stubs:
                stub.close()
        finally:
            self._tmp.cleanup()

    def stub(self, script, xfer_ip="127.0.0.1"):
        stub = Stub(script, xfer_ip)
        self.stubs.append(stub)
        return stub

    def real_server(self, **kwargs):
        server = TftpServer(self.server_root, port=0, allow_write=True, **kwargs)
        port = server.start()
        self.addCleanup(server.stop)
        return port


class TftpClientTest(ClientTestCase):
    def test_get_writes_the_file_locally(self):
        (self.server_root / "a.txt").write_bytes(b"hello world")
        port = self.real_server()
        dest = self.work / "a.txt"
        self.assertEqual(get("127.0.0.1", "a.txt", dest, port=port), 11)
        self.assertEqual(dest.read_bytes(), b"hello world")
        self.assertEqual(dest.stat().st_mode & 0o777, 0o600)

    def test_get_of_a_missing_file_raises_error_1(self):
        port = self.real_server()
        with self.assertRaises(TftpError) as ctx:
            get("127.0.0.1", "nope", self.work / "nope", port=port)
        self.assertEqual(ctx.exception.code, 1)

    def test_put_uploads_to_a_writable_server(self):
        port = self.real_server()
        src = self.work / "fw.bin"
        src.write_bytes(os.urandom(1500))
        self.assertEqual(put("127.0.0.1", src, "fw.bin", port=port), 1500)
        self.assertEqual((self.server_root / "fw.bin").read_bytes(), src.read_bytes())

    def test_unreachable_server_raises_timeout_error(self):
        with self.assertRaises(TimeoutError):
            get("127.0.0.1", "x", self.work / "x", port=free_port(), timeout=0.2, retries=1)
        self.assertEqual(os.listdir(self.work), [])

    def test_blksize_negotiated_end_to_end(self):
        data = os.urandom(5000)
        (self.server_root / "big").write_bytes(data)
        port = self.real_server()
        dest = self.work / "big"
        self.assertEqual(get("127.0.0.1", "big", dest, port=port, blksize=1428), 5000)
        self.assertEqual(dest.read_bytes(), data)
        src = self.work / "up"
        src.write_bytes(data)
        self.assertEqual(put("127.0.0.1", src, "up", port=port, blksize=1428), 5000)
        self.assertEqual((self.server_root / "up").read_bytes(), data)

    def test_exact_multiple_of_block_size(self):
        data = os.urandom(1024)
        (self.server_root / "m").write_bytes(data)
        port = self.real_server()
        dest = self.work / "m"
        self.assertEqual(get("127.0.0.1", "m", dest, port=port), 1024)
        self.assertEqual(dest.read_bytes(), data)
        src = self.work / "m2"
        src.write_bytes(data)
        self.assertEqual(put("127.0.0.1", src, "m2", port=port), 1024)
        self.assertEqual((self.server_root / "m2").read_bytes(), data)

    def test_empty_files(self):
        (self.server_root / "e").write_bytes(b"")
        port = self.real_server()
        dest = self.work / "e"
        self.assertEqual(get("127.0.0.1", "e", dest, port=port), 0)
        self.assertEqual(dest.read_bytes(), b"")
        src = self.work / "e2"
        src.write_bytes(b"")
        self.assertEqual(put("127.0.0.1", src, "e2", port=port), 0)
        self.assertEqual((self.server_root / "e2").read_bytes(), b"")

    def test_failed_get_leaves_existing_dest_and_no_temp_file(self):
        port = self.real_server()
        dest = self.work / "keep"
        dest.write_bytes(b"precious")
        with self.assertRaises(TftpError):
            get("127.0.0.1", "missing", dest, port=port)
        self.assertEqual(dest.read_bytes(), b"precious")
        self.assertEqual(os.listdir(self.work), ["keep"])

    def test_failed_get_midway_removes_temp_and_keeps_dest(self):
        def script(s):
            s.recv_request()
            s.send(encode_data(1, b"x" * 512))
            s.recv()
            s.send(encode_error(2, "denied"))

        stub = self.stub(script)
        dest = self.work / "keep"
        dest.write_bytes(b"precious")
        with self.assertRaises(TftpError) as ctx:
            get("127.0.0.1", "f", dest, port=stub.port)
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(dest.read_bytes(), b"precious")
        self.assertEqual(os.listdir(self.work), ["keep"])

    def test_dropped_first_data_is_recovered_by_retransmitting_the_request(self):
        def script(s):
            s.recv_request()  # first request: pretend the answer was lost
            s.recv_request()  # retransmission
            s.send(encode_data(1, b"a" * 512))
            s.recv()
            s.send(encode_data(2, b"b" * 88))
            s.recv()

        stub = self.stub(script)
        dest = self.work / "f"
        self.assertEqual(get("127.0.0.1", "f", dest, port=stub.port, timeout=0.2), 600)
        self.assertEqual(dest.read_bytes(), b"a" * 512 + b"b" * 88)
        stub.close()
        self.assertEqual([p.opcode for p in stub.log[:2]], [RRQ, RRQ])

    def test_duplicate_data_is_reacknowledged(self):
        def script(s):
            s.recv_request()
            s.send(encode_data(1, b"a" * 512))
            s.recv()
            s.send(encode_data(1, b"a" * 512))  # duplicate
            s.recv()
            s.send(encode_data(2, b"b"))
            s.recv()

        stub = self.stub(script)
        dest = self.work / "f"
        self.assertEqual(get("127.0.0.1", "f", dest, port=stub.port), 513)
        self.assertEqual(dest.read_bytes(), b"a" * 512 + b"b")
        stub.close()
        self.assertEqual([p.block for p in stub.log[1:]], [1, 1, 2])

    def test_duplicate_ack_in_put_does_not_resend_data(self):
        def script(s):
            s.recv_request()
            s.send(encode_ack(0))
            s.send(encode_ack(0))  # duplicate
            s.recv()
            s.send(encode_ack(1))
            s.send(encode_ack(1))  # duplicate
            s.recv()
            s.send(encode_ack(2))
            try:
                s.recv(timeout=0.15)
            except socket.timeout:
                pass

        stub = self.stub(script)
        src = self.work / "src"
        src.write_bytes(b"z" * 1000)
        self.assertEqual(put("127.0.0.1", src, "src", port=stub.port), 1000)
        stub.close()
        self.assertEqual(
            [(p.opcode, p.block) for p in stub.log], [(WRQ, None), (DATA, 1), (DATA, 2)]
        )

    def test_packet_from_another_port_gets_error_5_and_transfer_completes(self):
        other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(other.close)
        other.bind(("127.0.0.1", 0))
        seen = []

        def script(s):
            s.recv_request()
            s.send(encode_data(1, b"a" * 512))
            s.recv()
            s.send(encode_data(2, b"intruder"), other)
            seen.append(s.recv(other))
            s.send(encode_data(2, b"b" * 10))
            s.recv()

        stub = self.stub(script)
        dest = self.work / "f"
        self.assertEqual(get("127.0.0.1", "f", dest, port=stub.port), 522)
        stub.close()
        self.assertEqual(seen[0].opcode, ERROR)
        self.assertEqual(seen[0].error_code, 5)
        self.assertEqual(dest.read_bytes(), b"a" * 512 + b"b" * 10)

    def test_oack_with_larger_blksize_than_requested_raises_error_8(self):
        def script(s):
            s.recv_request()
            s.send(encode_oack({"blksize": "2000"}))
            s.recv()

        stub = self.stub(script)
        with self.assertRaises(TftpError) as ctx:
            get("127.0.0.1", "f", self.work / "f", port=stub.port, blksize=1428)
        self.assertEqual(ctx.exception.code, 8)
        stub.close()
        self.assertEqual(stub.log[-1].opcode, ERROR)
        self.assertEqual(stub.log[-1].error_code, 8)

    def test_request_carries_blksize_and_tsize_options(self):
        def script(s):
            s.recv_request()
            s.send(encode_error(1, "no"))

        stub = self.stub(script)
        with self.assertRaises(TftpError):
            get("127.0.0.1", "f", self.work / "f", port=stub.port, blksize=1428)
        stub.close()
        self.assertEqual(stub.log[0].options, {"blksize": "1428", "tsize": "0"})
        self.assertEqual(stub.log[0].mode, "octet")

    def test_block_limit_helper(self):
        self.assertFalse(tftp_client._block_limit_reached(65534))
        self.assertTrue(tftp_client._block_limit_reached(65535))

    def test_get_stops_when_full_blocks_exhaust_the_block_numbers(self):
        # Same path as block 65535, with the limit lowered so the test stays fast.
        def script(s):
            s.recv_request()
            s.send(encode_oack({"blksize": "8"}))
            s.recv()
            for block in range(1, 100):
                s.send(encode_data(block, b"12345678"))
                if s.recv().opcode == ERROR:
                    return

        stub = self.stub(script)
        with mock.patch.object(tftp_client, "MAX_BLOCK", 5):
            with self.assertRaises(TftpError) as ctx:
                get("127.0.0.1", "f", self.work / "f", port=stub.port, blksize=8)
        self.assertEqual(ctx.exception.code, 3)
        stub.close()
        self.assertEqual(stub.log[-1].opcode, ERROR)
        self.assertEqual(stub.log[-1].error_code, 3)
        self.assertEqual(os.listdir(self.work), [])

    def test_put_of_a_file_over_the_limit_opens_no_socket(self):
        src = self.work / "huge"
        with open(src, "wb") as f:
            f.truncate(65535 * 512)  # one byte over what 65535 blocks can carry
        port = free_port()
        with mock.patch.object(tftp_client.socket, "socket", side_effect=AssertionError("socket opened")):
            with self.assertRaises(TftpError) as ctx:
                put("127.0.0.1", src, "huge", port=port)
        self.assertEqual(ctx.exception.code, 3)

    def test_put_ack0_fallback_to_512_enforces_block_limit(self):
        def script(s):
            s.recv_request()
            s.send(encode_ack(0))  # options ignored: 512-byte blocks
            s.recv()

        stub = self.stub(script)
        src = self.work / "huge"
        with open(src, "wb") as f:
            f.truncate(40 * 1024 * 1024)
        with self.assertRaises(TftpError) as ctx:
            put("127.0.0.1", src, "huge", port=stub.port, blksize=1428)
        self.assertEqual(ctx.exception.code, 3)
        stub.close()
        self.assertEqual(stub.log[-1].opcode, ERROR)
        self.assertEqual(stub.log[-1].error_code, 3)

    def test_get_cleans_up_when_socket_creation_fails(self):
        dest = self.work / "keep"
        dest.write_bytes(b"precious")
        with mock.patch.object(
            tftp_client.socket, "socket", side_effect=OSError(errno.EMFILE, "too many open files")
        ):
            with self.assertRaises(OSError):
                get("127.0.0.1", "f", dest, port=1)
        self.assertEqual(dest.read_bytes(), b"precious")
        self.assertEqual(os.listdir(self.work), ["keep"])

    def test_reply_from_a_different_ip_is_ignored(self):
        def script(s):
            s.recv_request()
            s.send(encode_data(1, b"x"))
            try:
                s.recv(timeout=0.5)
            except socket.timeout:
                pass

        stub = self.stub(script, xfer_ip="127.0.0.2")
        with self.assertRaises(TimeoutError):
            get("127.0.0.1", "f", self.work / "f", port=stub.port, timeout=0.2, retries=1)
        self.assertEqual(os.listdir(self.work), [])

    def test_garbage_datagram_is_ignored(self):
        def script(s):
            s.recv_request()
            s.send(b"\x00\x09junk")
            s.send(encode_data(1, b"hello"))
            s.recv()

        stub = self.stub(script)
        dest = self.work / "f"
        self.assertEqual(get("127.0.0.1", "f", dest, port=stub.port), 5)
        self.assertEqual(dest.read_bytes(), b"hello")

    def test_put_of_missing_or_non_regular_source(self):
        with self.assertRaises(FileNotFoundError):
            put("127.0.0.1", self.work / "none", "x", port=free_port())
        with self.assertRaises(IsADirectoryError):
            put("127.0.0.1", self.work, "x", port=free_port())

    def test_bad_filenames_raise_value_error(self):
        for name in ("", "a\0b"):
            with self.assertRaises(ValueError):
                get("127.0.0.1", name, self.work / "f", port=free_port())
            src = self.work / "s"
            src.write_bytes(b"x")
            with self.assertRaises(ValueError):
                put("127.0.0.1", src, name, port=free_port())

    def test_cancel_sends_error_and_raises_interrupted(self):
        def script(s):
            s.recv_request()
            block = 1
            while block < 100:
                s.send(encode_data(block, b"x" * 512))
                if s.recv().opcode == ERROR:
                    return
                block += 1

        stub = self.stub(script)
        dest = self.work / "f"
        with self.assertRaises(InterruptedError):
            get(
                "127.0.0.1", "f", dest, port=stub.port,
                should_cancel=lambda: len(stub.log) >= 2,
            )
        stub.close()
        self.assertEqual(stub.log[-1].opcode, ERROR)
        self.assertEqual(stub.log[-1].error_code, 0)
        self.assertEqual(os.listdir(self.work), [])

    def test_put_error_from_server_carries_its_code(self):
        def script(s):
            s.recv_request()
            s.send(encode_error(6, "exists"))

        stub = self.stub(script)
        src = self.work / "s"
        src.write_bytes(b"x")
        with self.assertRaises(TftpError) as ctx:
            put("127.0.0.1", src, "s", port=stub.port)
        self.assertEqual(ctx.exception.code, 6)


if __name__ == "__main__":
    unittest.main()
