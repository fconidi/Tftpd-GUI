import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tftpd_gui.core.protocol import encode_message
from tftpd_gui.helper import daemon
from tftpd_gui.helper.daemon import EVENT_QUEUE_MAX, Daemon, main


class Pipe:
    """Blocking in-memory byte stream usable as helper stdin and as stdout."""

    def __init__(self):
        self.cond = threading.Condition()
        self.buf = bytearray()
        self.closed = False

    def write(self, data):
        with self.cond:
            self.buf += data
            self.cond.notify_all()
        return len(data)

    def flush(self):
        pass

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def readline(self, limit=-1):
        with self.cond:
            while True:
                i = self.buf.find(b"\n")
                if i >= 0:
                    n = i + 1
                    if 0 <= limit < n:
                        n = limit
                    line = bytes(self.buf[:n])
                    del self.buf[:n]
                    return line
                if self.closed:
                    line, self.buf = bytes(self.buf), bytearray()
                    return line
                if 0 <= limit <= len(self.buf):
                    line = bytes(self.buf[:limit])
                    del self.buf[:limit]
                    return line
                self.cond.wait(5)


class Session:
    def __init__(self, test, environ=None):
        self.stdin, self.stdout = Pipe(), Pipe()
        self.next_id = 0
        self.code = None
        self.thread = threading.Thread(
            target=lambda: setattr(self, "code", main(self.stdin, self.stdout, [], environ or {})))
        self.thread.start()
        test.addCleanup(self.end)

    def send_raw(self, data):
        self.stdin.write(data)

    def read(self):
        line = self.stdout.readline()
        return json.loads(line) if line else None

    def call(self, cmd, **args):
        self.next_id += 1
        self.stdin.write(encode_message({"id": self.next_id, "cmd": cmd, "args": args}))
        while True:
            msg = self.read()
            if msg is None or msg.get("id") == self.next_id:
                return msg

    def end(self):
        self.stdin.close()
        self.thread.join(5)


class HelperDaemonTest(unittest.TestCase):
    def test_dhcp_timeout_removes_the_service_from_status(self):
        helper = Daemon(io.BytesIO(), {})
        helper._dhcp = object()
        helper._pool = object()
        helper._dhcp_event({"event": "stopped", "reason": "timeout"})
        self.assertFalse(helper._status({})["dhcp"]["running"])
        self.assertIsNone(helper._pool)

    def tmpdir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_start_tftp_answers_with_the_bound_port(self):
        s = Session(self)
        reply = s.call("start_tftp", root=self.tmpdir(), port=0)
        self.assertTrue(reply["ok"])
        self.assertIsInstance(reply["result"]["port"], int)
        self.assertGreater(reply["result"]["port"], 0)

    def test_unknown_command_is_refused(self):
        reply = Session(self).call("format_disk")
        self.assertEqual((reply["ok"], reply["error"]), (False, "unknown_command"))

    def test_there_is_no_command_that_writes_a_file(self):
        s = Session(self)
        for cmd in ("save", "write_file", "save_log", "save_messages"):
            self.assertEqual(s.call(cmd, path="/tmp/x")["error"], "unknown_command")

    def test_busy_port_is_reported_as_port_busy(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(blocker.close)
        blocker.bind(("0.0.0.0", 0))
        port = blocker.getsockname()[1]
        reply = Session(self).call("start_tftp", root=self.tmpdir(), port=port)
        self.assertEqual((reply["error"], reply["detail"]), ("port_busy", port))

    def test_bad_arguments_are_reported(self):
        s = Session(self)
        self.assertEqual(s.call("start_tftp")["error"], "bad_arguments")
        self.assertEqual(s.call("start_tftp", root="/nonexistent-dir")["error"], "bad_arguments")
        self.assertEqual(s.call("start_tftp", root=self.tmpdir(), port="x")["error"], "bad_arguments")
        self.assertEqual(s.call("start_tftp", root=self.tmpdir(), port=True)["error"], "bad_arguments")

    def test_malformed_request_keeps_the_loop_alive(self):
        s = Session(self)
        s.send_raw(b"this is not json\n[]\n")
        self.assertEqual(s.read()["error"], "bad_json")
        self.assertEqual(s.read()["error"], "bad_json")
        self.assertTrue(s.call("status")["ok"])

    def test_second_start_is_refused(self):
        s = Session(self)
        root = self.tmpdir()
        self.assertTrue(s.call("start_tftp", root=root, port=0)["ok"])
        self.assertEqual(s.call("start_tftp", root=root, port=0)["error"], "already_running")

    def test_eof_on_stdin_stops_every_service(self):
        s = Session(self)
        port = s.call("start_tftp", root=self.tmpdir(), port=0)["result"]["port"]
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(probe.close)
        with self.assertRaises(OSError):
            probe.bind(("0.0.0.0", port))
        s.end()
        self.assertEqual(s.code, 0)
        probe.bind(("0.0.0.0", port))

    def test_stop_commands_free_the_ports(self):
        s = Session(self)
        port = s.call("start_syslog", port=0)["result"]["port"]
        self.assertTrue(s.call("stop_syslog")["ok"])
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(probe.close)
        probe.bind(("0.0.0.0", port))
        self.assertTrue(s.call("stop_tftp")["ok"])

    def test_status_lists_the_running_services(self):
        s = Session(self)
        self.assertEqual([s.call("status")["result"][k]["running"] for k in ("tftp", "dhcp", "syslog")],
                         [False] * 3)
        s.call("start_syslog", port=0)
        self.assertTrue(s.call("status")["result"]["syslog"]["running"])

    def test_syslog_messages_are_pushed_as_events(self):
        s = Session(self)
        port = s.call("start_syslog", port=0)["result"]["port"]
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        sender.sendto(b"<134>Nov  5 10:00:00 sw1 link up", ("127.0.0.1", port))
        while True:
            msg = s.read()
            if msg.get("event") == "syslog":
                break
        self.assertIn("link up", msg["data"]["raw"])

    def test_event_queue_is_bounded_and_reports_dropped_events(self):
        gate = threading.Event()
        written = []

        class Stalled:
            def write(self, data):
                gate.wait(5)
                written.append(data)

            def flush(self):
                pass

        d = Daemon(Stalled(), {})
        d._writer.start()
        self.addCleanup(lambda: (gate.set(), d._writer.join(2)))
        for i in range(EVENT_QUEUE_MAX * 3):
            d.emit("syslog", {"n": i})
            self.assertLessEqual(len(d.events), EVENT_QUEUE_MAX)
        gate.set()
        deadline = time.time() + 5
        while time.time() < deadline and not any(b'"dropped"' in w for w in written):
            time.sleep(0.05)
        dropped = [json.loads(w) for w in written if b'"dropped"' in w]
        self.assertTrue(dropped)
        self.assertGreater(dropped[0]["data"]["count"], 0)

    def test_start_dhcp_uses_the_interface_address_not_one_from_the_request(self):
        captured = {}

        class FakeServer:
            def __init__(self, pool, **kw):
                captured.update(kw, pool=pool)

            def start(self):
                return 67

            def stop(self):
                pass

        with mock.patch.object(daemon, "interface_ipv4", return_value=("192.168.50.1", "255.255.255.0")), \
                mock.patch.object(daemon, "DhcpServer", FakeServer):
            reply = Session(self).call("start_dhcp", interface="eth9", pool_start="192.168.50.100",
                                       pool_end="192.168.50.110", server_ip="10.9.9.9", netmask="255.0.0.0")
        self.assertTrue(reply["ok"])
        self.assertEqual((captured["server_ip"], captured["netmask"]), ("192.168.50.1", "255.255.255.0"))
        self.assertEqual(reply["result"]["server_ip"], "192.168.50.1")

    def test_start_dhcp_with_a_pool_outside_the_subnet_is_config_invalid(self):
        with mock.patch.object(daemon, "interface_ipv4", return_value=("192.168.50.1", "255.255.255.0")):
            reply = Session(self).call("start_dhcp", interface="eth9", pool_start="10.0.0.5",
                                       pool_end="10.0.0.9", port=0)
        self.assertEqual((reply["error"], reply["detail"]), ("config_invalid", "pool_outside_subnet"))

    def test_start_dhcp_on_an_interface_without_ipv4_is_config_invalid_no_ipv4(self):
        with mock.patch.object(daemon, "interface_ipv4", return_value=None):
            reply = Session(self).call("start_dhcp", interface="eth9", pool_start="192.168.50.100",
                                       pool_end="192.168.50.110")
        self.assertEqual((reply["error"], reply["detail"]), ("config_invalid", "no_ipv4"))

    def test_probe_dhcp_returns_the_three_state_result(self):
        from tftpd_gui.core.dhcp_probe import ProbeResult
        for result in (ProbeResult("found", [{"server_id": "1.2.3.4", "offered_ip": "1.2.3.5", "mac": None}], None),
                       ProbeResult("not_found", [], None), ProbeResult("error", [], "port 68 busy")):
            with mock.patch.object(daemon.dhcp_probe, "probe_segment", return_value=result):
                reply = Session(self).call("probe_dhcp", interface="eth0")
            self.assertEqual(reply["result"], {"status": result.status, "servers": result.servers,
                                               "reason": result.reason})

    @unittest.skipUnless(os.geteuid() == 0, "needs root to switch file identity")
    def test_tftp_runs_as_the_calling_user_when_pkexec_uid_is_set(self):
        root = self.tmpdir()
        os.chmod(root, 0o755)
        for name, mode in (("secret.txt", 0o600), ("public.txt", 0o644)):
            Path(root, name).write_text("data")
            os.chmod(Path(root, name), mode)
        s = Session(self, {"PKEXEC_UID": "65534"})
        port = s.call("start_tftp", root=root, port=0)["result"]["port"]
        from tftpd_gui.core import tftp_client
        out = Path(self.tmpdir())
        self.assertEqual(tftp_client.get("127.0.0.1", "public.txt", out / "p", port=port, timeout=1), 4)
        with self.assertRaises(Exception):
            tftp_client.get("127.0.0.1", "secret.txt", out / "s", port=port, timeout=1)

    def test_bad_pkexec_uid_is_refused(self):
        reply = Session(self, {"PKEXEC_UID": "abc"}).call("start_tftp", root=self.tmpdir(), port=0)
        self.assertEqual(reply["error"], "bad_pkexec_uid")


if __name__ == "__main__":
    unittest.main()
