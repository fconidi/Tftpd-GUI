import os
import shutil
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tftpd_gui.core import tftp_server
from tftpd_gui.core.errors import PortBusy
from tftpd_gui.core.tftp_packet import (
    ACK,
    DATA,
    ERROR,
    OACK,
    RRQ,
    WRQ,
    decode,
    encode_ack,
    encode_data,
    encode_error,
    encode_request,
)
from tftpd_gui.core.tftp_server import TftpServer, max_transfer_bytes


def free_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("0.0.0.0", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


class Client:
    def __init__(self, port):
        self.server = ("127.0.0.1", port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(3)
        self.tid = None

    def close(self):
        self.sock.close()

    def request(self, opcode, name, mode="octet", options=None):
        self.sock.sendto(encode_request(opcode, name, mode, options), self.server)

    def send(self, raw):
        self.sock.sendto(raw, self.tid)

    def recv(self):
        data, addr = self.sock.recvfrom(70000)
        self.tid = addr
        return decode(data)

    def read_all(self, name, mode="octet", options=None):
        self.request(RRQ, name, mode, options)
        blksize = int((options or {}).get("blksize", 512))
        out = b""
        sizes = []
        packet = self.recv()
        if packet.opcode == OACK:
            self.oack = packet.options
            self.send(encode_ack(0))
            packet = self.recv()
        while True:
            if packet.opcode == ERROR:
                return packet
            assert packet.opcode == DATA
            out += packet.payload
            sizes.append(len(packet.payload))
            self.send(encode_ack(packet.block))
            if len(packet.payload) < blksize:
                self.sizes = sizes
                return out
            packet = self.recv()

    def write_all(self, name, content, mode="octet", blksize=512):
        self.request(WRQ, name, mode)
        packet = self.recv()
        if packet.opcode == ERROR:
            return packet
        assert (packet.opcode, packet.block) == (ACK, 0)
        block = 1
        offset = 0
        while True:
            chunk = content[offset:offset + blksize]
            self.send(encode_data(block, chunk))
            packet = self.recv()
            if packet.opcode == ERROR:
                return packet
            assert (packet.opcode, packet.block) == (ACK, block)
            offset += blksize
            block += 1
            if len(chunk) < blksize:
                return None


class TftpServerTest(unittest.TestCase):
    # every server in this class starts with port=0 and talks over 127.0.0.1
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "root"
        self.root.mkdir()
        self.servers = []
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        for server in self.servers:
            server.stop()
        shutil.rmtree(self.base)

    def start(self, **kwargs):
        kwargs.setdefault("port", 0)
        server = TftpServer(self.root, **kwargs)
        self.servers.append(server)
        port = server.start()
        return server, port

    def client(self, port):
        client = Client(port)
        self.clients.append(client)
        return client

    def wait_transfers(self, server, count=1, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(server.transfers) >= count:
                return server.transfers
            time.sleep(0.02)
        self.fail(f"no transfer recorded within {timeout} s: {server.transfers}")

    def assertError(self, packet, code):
        self.assertEqual(packet.opcode, ERROR, packet)
        self.assertEqual(packet.error_code, code, packet)

    def test_read_returns_the_file_contents(self):
        content = os.urandom(1300)
        (self.root / "ios.bin").write_bytes(content)
        (self.root / "dir").mkdir()
        (self.root / "dir" / "exact").write_bytes(content[:1024])
        _, port = self.start()
        self.assertEqual(self.client(port).read_all("ios.bin"), content)
        exact = self.client(port)
        self.assertEqual(exact.read_all("dir/exact"), content[:1024])
        self.assertEqual(exact.sizes, [512, 512, 0])

    def test_missing_file_answers_error_1(self):
        _, port = self.start()
        self.assertError(self.client(port).read_all("missing"), 1)

    def test_symlink_inside_the_root_is_refused_with_error_2(self):
        (self.root / "real").write_bytes(b"x")
        os.symlink("real", self.root / "link")
        _, port = self.start()
        self.assertError(self.client(port).read_all("link"), 2)
        self.assertError(self.client(port).read_all("../root/real"), 2)
        self.assertError(self.client(port).read_all(str(self.root / "real")), 2)

    def test_write_is_refused_when_not_allowed(self):
        _, port = self.start()
        self.assertError(self.client(port).write_all("new", b"data"), 2)
        self.assertFalse((self.root / "new").exists())

    def test_write_stores_the_file_when_allowed(self):
        content = os.urandom(2000)
        server, port = self.start(allow_write=True)
        self.assertIsNone(self.client(port).write_all("new.cfg", content))
        self.wait_transfers(server)
        self.assertEqual((self.root / "new.cfg").read_bytes(), content)

    def test_duplicate_final_data_receives_the_final_ack_again(self):
        _, port = self.start(allow_write=True)
        client = self.client(port)
        client.request(WRQ, "retry.cfg")
        initial = client.recv()
        self.assertEqual((initial.opcode, initial.block), (ACK, 0))
        final_data = encode_data(1, b"configuration")
        client.send(final_data)
        first = client.recv()
        self.assertEqual((first.opcode, first.block), (ACK, 1))
        time.sleep(tftp_server.DEFAULT_TIMEOUT + 0.1)
        client.send(final_data)
        second = client.recv()
        self.assertEqual((second.opcode, second.block), (ACK, 1))
        self.assertEqual((self.root / "retry.cfg").read_bytes(), b"configuration")

    def test_existing_file_is_not_overwritten_by_default(self):
        (self.root / "cfg").write_bytes(b"old")
        _, port = self.start(allow_write=True)
        self.assertError(self.client(port).write_all("cfg", b"new"), 6)
        self.assertEqual((self.root / "cfg").read_bytes(), b"old")

    def test_overwrite_is_atomic(self):
        (self.root / "cfg").write_bytes(b"old")
        server, port = self.start(allow_write=True, allow_overwrite=True, idle_seconds=0.3)
        new = os.urandom(1500)
        self.assertIsNone(self.client(port).write_all("cfg", new))
        self.wait_transfers(server, 1)
        self.assertEqual((self.root / "cfg").read_bytes(), new)
        self.assertEqual(sorted(os.listdir(self.root)), ["cfg"])

        # abandoned with an error packet
        client = self.client(port)
        client.request(WRQ, "cfg", "octet")
        self.assertEqual(client.recv().block, 0)
        client.send(encode_data(1, b"x" * 512))
        self.assertEqual(client.recv().block, 1)
        self.assertIn(".cfg.part", os.listdir(self.root))
        client.send(encode_error(0, "cancelled"))
        self.wait_transfers(server, 2)
        # abandoned by going silent
        client = self.client(port)
        client.request(WRQ, "cfg", "octet")
        self.assertEqual(client.recv().block, 0)
        client.send(encode_data(1, b"y" * 512))
        self.assertEqual(client.recv().block, 1)
        self.wait_transfers(server, 3)
        self.assertEqual([t["result"] for t in server.transfers], ["ok", "error", "timeout"])
        self.assertEqual((self.root / "cfg").read_bytes(), new)
        self.assertEqual(sorted(os.listdir(self.root)), ["cfg"])

    @unittest.skipUnless(os.geteuid() == 0, "needs root")
    def test_root_only_file_is_refused_when_serving_as_another_user(self):
        os.chmod(self.base, 0o755)
        os.chmod(self.root, 0o755)
        (self.root / "private").write_bytes(b"secret")
        os.chown(self.root / "private", 0, 0)
        os.chmod(self.root / "private", 0o600)
        (self.root / "public").write_bytes(b"public")
        os.chmod(self.root / "public", 0o644)
        _, port = self.start(file_uid=65534, file_gid=65534)
        self.assertError(self.client(port).read_all("private"), 2)
        self.assertEqual(self.client(port).read_all("public"), b"public")
        # the identity switch stays in the transfer thread
        with open("/proc/self/status") as status:
            fields = {line.split(":")[0]: line.split()[1:] for line in status}
        self.assertEqual((fields["Uid"][3], fields["Gid"][3]), ("0", "0"))

    @unittest.skipUnless(os.geteuid() == 0, "needs root")
    def test_group_readable_file_of_a_server_group_is_refused_when_serving_as_another_user(self):
        os.chmod(self.base, 0o755)
        os.chmod(self.root, 0o755)
        (self.root / "grouponly").write_bytes(b"secret")
        os.chown(self.root / "grouponly", 0, os.getgid())
        os.chmod(self.root / "grouponly", 0o640)
        groups = os.getgroups()
        _, port = self.start(file_uid=65534, file_gid=65534)
        self.assertError(self.client(port).read_all("grouponly"), 2)
        # the supplementary groups are dropped in the transfer thread only
        self.assertEqual(os.getgroups(), groups)

    def test_identity_change_that_does_not_take_effect_answers_error_2(self):
        (self.root / "public").write_bytes(b"public")
        # the switch made by start() to open the root succeeds, the transfer's does not
        with mock.patch.object(tftp_server, "_set_fs_identity", side_effect=[True, False]):
            _, port = self.start(file_uid=65534, file_gid=65534)
        with mock.patch.object(tftp_server, "_set_fs_identity", return_value=False), \
                mock.patch.object(tftp_server, "open_beneath") as opened:
            self.assertError(self.client(port).read_all("public"), 2)
            opened.assert_not_called()

    @unittest.skipUnless(os.geteuid() == 0, "needs root")
    def test_root_behind_a_private_parent_is_refused_when_serving_as_another_user(self):
        os.chmod(self.base, 0o700)
        os.chmod(self.root, 0o755)
        port = free_port()
        server = TftpServer(self.root, port=port, file_uid=65534, file_gid=65534)
        self.servers.append(server)
        with self.assertRaises(PermissionError):
            server.start()
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("0.0.0.0", port))
        finally:
            probe.close()

    @unittest.skipUnless(os.geteuid() == 0, "needs root")
    def test_root_behind_a_searchable_parent_starts_when_serving_as_another_user(self):
        os.chmod(self.base, 0o755)
        os.chmod(self.root, 0o755)
        (self.root / "public").write_bytes(b"public")
        os.chmod(self.root / "public", 0o644)
        _, port = self.start(file_uid=65534, file_gid=65534)
        self.assertEqual(self.client(port).read_all("public"), b"public")

    def test_file_uid_and_file_gid_must_be_given_together(self):
        for uid, gid in ((65534, None), (None, 65534)):
            with self.assertRaises(ValueError):
                TftpServer(self.root, port=0, file_uid=uid, file_gid=gid)

    def test_groups_are_not_dropped_by_a_32_bit_process(self):
        with mock.patch.object(tftp_server, "_pointer_size", return_value=4):
            self.assertFalse(tftp_server._drop_thread_groups())

    def test_overlong_filename_answers_error_2_without_a_record(self):
        server, port = self.start()
        client = self.client(port)
        client.request(RRQ, "a" * 5000, "octet")
        self.assertError(client.recv(), 2)
        self.assertError(self.client(port).read_all("missing"), 1)
        records = self.wait_transfers(server, 1)
        time.sleep(0.1)
        self.assertEqual([t["filename"] for t in server.transfers], ["missing"])

    def test_recorded_filename_is_truncated_to_255_characters(self):
        events = []
        server, port = self.start(on_event=events.append)
        packet = self.client(port).read_all("b" * 4000)
        self.assertEqual(packet.opcode, ERROR)
        record = self.wait_transfers(server, 1)[0]
        self.assertLessEqual(len(record["filename"]), 255)
        self.assertTrue(record["filename"].startswith("bbb"))
        self.assertTrue(all(len(e.get("filename") or "") <= 255 for e in events))

    def test_transfer_records_are_bounded_by_max_records(self):
        events = []
        server, port = self.start(max_records=5, on_event=events.append)
        for _ in range(8):
            self.assertError(self.client(port).read_all("missing"), 1)
        deadline = time.monotonic() + 3
        while len(events) < 8 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(events), 8)
        self.assertEqual(len(server.transfers), 5)
        self.assertEqual(len(list(server.transfers)), 5)

    def test_listener_survives_an_unexpected_dispatch_error(self):
        (self.root / "f").write_bytes(b"hello")
        events = []
        server, port = self.start(on_event=events.append)
        original = server._dispatch
        calls = []

        def flaky(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return original(*args)

        server._dispatch = flaky
        first = self.client(port)
        first.sock.settimeout(0.3)
        with self.assertLogs("tftpd_gui.core.tftp_server", "ERROR"):
            first.request(RRQ, "f", "octet")
            with self.assertRaises(socket.timeout):
                first.recv()
        self.assertEqual(self.client(port).read_all("f"), b"hello")
        self.assertIn({"event": "error", "error": "RuntimeError"}, events)

    def test_blksize_option_is_acknowledged_in_oack(self):
        content = os.urandom(3000)
        (self.root / "big").write_bytes(content)
        _, port = self.start()
        client = self.client(port)
        self.assertEqual(client.read_all("big", options={"blksize": "1428", "tsize": "0"}), content)
        self.assertEqual(client.oack, {"blksize": "1428", "tsize": "3000"})
        self.assertEqual(client.sizes, [1428, 1428, 144])

    def test_write_past_the_size_cap_is_aborted(self):
        server, port = self.start(allow_write=True, max_bytes=1024)
        self.assertError(self.client(port).write_all("big", os.urandom(4096)), 3)
        self.wait_transfers(server)
        self.assertEqual(server.transfers[0]["result"], "error")
        self.assertEqual(os.listdir(self.root), [])

    def test_effective_limit_is_65535_blocks_minus_one_byte(self):
        self.assertEqual(max_transfer_bytes(512, 10 ** 9), 33553919)
        self.assertEqual(max_transfer_bytes(1428, 10 ** 9), 93583979)
        self.assertEqual(max_transfer_bytes(8192, 10 ** 9), 536862719)
        self.assertEqual(max_transfer_bytes(512, 1000), 1000)

    def test_read_of_exactly_65535_blocks_is_refused_with_error_3(self):
        with open(self.root / "sparse", "wb") as f:
            f.truncate(65535 * 512)
        _, port = self.start(max_bytes=10 ** 9)
        client = self.client(port)
        client.request(RRQ, "sparse", "octet", {"blksize": "512"})
        self.assertError(client.recv(), 3)

    def test_read_one_byte_below_the_limit_starts_transferring(self):
        with open(self.root / "sparse", "wb") as f:
            f.truncate(65535 * 512 - 1)
        server, port = self.start(max_bytes=10 ** 9)
        client = self.client(port)
        client.request(RRQ, "sparse", "octet")
        packet = client.recv()
        self.assertEqual((packet.opcode, packet.block, len(packet.payload)), (DATA, 1, 512))
        client.send(encode_error(0, "enough"))
        self.wait_transfers(server)

    def test_read_larger_than_max_bytes_is_refused_with_error_3(self):
        (self.root / "big").write_bytes(os.urandom(4096))
        _, port = self.start(max_bytes=1024)
        self.assertError(self.client(port).read_all("big"), 3)

    @unittest.skipUnless(os.geteuid() == 0, "SO_BINDTODEVICE on lo")
    def test_bind_device_is_applied_to_listening_and_transfer_sockets(self):
        calls = []

        class RecordingSocket(socket.socket):
            def setsockopt(self, level, option, value, *rest):
                calls.append((id(self), level, option, value))
                return super().setsockopt(level, option, value, *rest)

        (self.root / "f").write_bytes(b"hello")
        server, port = self.start(bind_device="lo", socket_factory=RecordingSocket)
        self.assertEqual(self.client(port).read_all("f"), b"hello")
        self.wait_transfers(server)
        bound = {sock for sock, level, option, value in calls
                 if (level, option) == (socket.SOL_SOCKET, socket.SO_BINDTODEVICE)
                 and value.rstrip(b"\x00") == b"lo"}
        self.assertEqual(len(bound), 2)

    def test_requests_beyond_max_transfers_get_error_0(self):
        (self.root / "f").write_bytes(b"x" * 600)
        _, port = self.start(max_transfers=1)
        first = self.client(port)
        first.request(RRQ, "f", "octet")
        self.assertEqual(first.recv().opcode, DATA)
        second = self.client(port)
        second.request(RRQ, "f", "octet")
        packet = second.recv()
        self.assertError(packet, 0)
        self.assertEqual(packet.message, "server busy")

    def test_stalled_transfer_is_dropped_after_idle_seconds(self):
        (self.root / "f").write_bytes(b"x" * 600)
        server, port = self.start(idle_seconds=0.5)
        client = self.client(port)
        client.request(RRQ, "f", "octet")
        self.assertEqual(client.recv().opcode, DATA)
        self.wait_transfers(server, timeout=3)
        self.assertEqual(server.transfers[0]["result"], "timeout")

    def test_second_server_on_the_same_port_raises_port_busy(self):
        _, port = self.start()
        second = TftpServer(self.root, port=port)
        self.servers.append(second)
        with self.assertRaises(PortBusy) as ctx:
            second.start()
        self.assertEqual(ctx.exception.port, port)
        second.stop()

    def test_transfer_is_recorded_with_its_result(self):
        (self.root / "f").write_bytes(b"x" * 700)
        events = []
        server, port = self.start(on_event=events.append)
        self.assertEqual(self.client(port).read_all("f"), b"x" * 700)
        self.assertError(self.client(port).read_all("missing"), 1)
        records = self.wait_transfers(server, 2)
        by_name = {t["filename"]: t for t in records}
        ok = by_name["f"]
        self.assertEqual(set(ok), {"peer", "filename", "direction", "bytes", "seconds", "result"})
        self.assertEqual((ok["direction"], ok["bytes"], ok["result"]), ("read", 700, "ok"))
        self.assertGreaterEqual(ok["seconds"], 0)
        self.assertEqual(by_name["missing"]["result"], "error")
        self.assertTrue(any(e.get("filename") == "f" for e in events))

    def test_unsupported_mode_and_empty_name_answer_error_4(self):
        (self.root / "f").write_bytes(b"x")
        _, port = self.start()
        self.assertError(self.client(port).read_all("f", mode="mail"), 4)
        self.assertError(self.client(port).read_all("", mode="octet"), 4)

    def test_netascii_converts_line_endings(self):
        (self.root / "text").write_bytes(b"a\nb\rc\n")
        server, port = self.start(allow_write=True)
        self.assertEqual(self.client(port).read_all("text", mode="NETASCII"), b"a\r\nb\r\x00c\r\n")
        wire = b"x\r\ny\r\x00z" + b"q" * 504 + b"\r" + b"\nend\r\n"
        self.assertIsNone(self.client(port).write_all("up", wire, mode="netascii"))
        self.wait_transfers(server, 2)
        self.assertEqual((self.root / "up").read_bytes(), b"x\ny\rz" + b"q" * 504 + b"\nend\n")


if __name__ == "__main__":
    unittest.main()
