import inspect
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tftpd_gui.core import syslog_server
from tftpd_gui.core.errors import PortBusy
from tftpd_gui.core.syslog_server import SyslogCollector, parse_line, save_messages


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ParseLineTest(unittest.TestCase):
    def test_rfc3164_line_is_split(self):
        parsed = parse_line(b"<189>Nov  5 11:00:00 switch1 %LINK-3-UPDOWN: up")
        self.assertEqual((parsed["facility"], parsed["severity"]), (23, 5))
        self.assertEqual(parsed["host"], "switch1")
        self.assertEqual(parsed["tag"], "%LINK-3-UPDOWN")
        self.assertEqual(parsed["message"], "up")
        self.assertEqual(parsed["timestamp"], "Nov  5 11:00:00")

    def test_rfc3164_tag_with_pid(self):
        parsed = parse_line(b"<13>Nov  5 11:00:00 box sshd[42]: accepted")
        self.assertEqual(parsed["tag"], "sshd[42]")
        self.assertEqual(parsed["message"], "accepted")

    def test_cisco_sequence_and_star_timestamp(self):
        parsed = parse_line(b"<189>000123: *Nov  5 11:00:00.123: %LINK-3-UPDOWN: up")
        self.assertEqual(parsed["timestamp"], "Nov  5 11:00:00.123")
        self.assertEqual(parsed["tag"], "%LINK-3-UPDOWN")
        self.assertEqual(parsed["message"], "up")

    def test_rfc3164_without_tag_keeps_host_and_whole_text(self):
        parsed = parse_line(b"<189>Nov 5 11:00:00 sw1 link down on port 3: reason")
        self.assertEqual(parsed["timestamp"], "Nov 5 11:00:00")
        self.assertEqual(parsed["host"], "sw1")
        self.assertIsNone(parsed["tag"])
        self.assertEqual(parsed["message"], "link down on port 3: reason")

    def test_rfc3164_tag_with_pid_is_kept_whole(self):
        parsed = parse_line(b"<13>Nov  5 11:00:00 box sshd[123]: x")
        self.assertEqual((parsed["host"], parsed["tag"], parsed["message"]), ("box", "sshd[123]", "x"))

    def test_rfc5424_message_starting_with_bracket_is_not_structured_data(self):
        parsed = parse_line(b"<34>1 2026-11-05T11:00:00Z host app - - [ERROR] disk full")
        self.assertEqual(parsed["message"], "[ERROR] disk full")

    def test_rfc5424_structured_data_with_params_is_skipped(self):
        parsed = parse_line(b'<34>1 2026-11-05T11:00:00Z host app 1 ID [exampleSDID@32473 iut="3"] hello')
        self.assertEqual(parsed["message"], "hello")
        parsed = parse_line(b"<34>1 2026-11-05T11:00:00Z host app 1 ID - [ERROR] x")
        self.assertEqual(parsed["message"], "[ERROR] x")

    def test_rfc3164_unmatched_structure_keeps_text_after_priority(self):
        parsed = parse_line(b"<13>just some words")
        self.assertEqual(parsed["severity"], 5)
        self.assertEqual(parsed["message"], "just some words")
        self.assertIsNone(parsed["host"])

    def test_rfc5424_line_is_split(self):
        parsed = parse_line(b"<34>1 2026-11-05T11:00:00Z host app - - disk full")
        self.assertEqual((parsed["facility"], parsed["severity"]), (4, 2))
        self.assertEqual(parsed["message"], "disk full")
        self.assertEqual(parsed["host"], "host")
        self.assertEqual(parsed["tag"], "app")

    def test_rfc5424_full_line(self):
        parsed = parse_line(b"<34>1 2026-11-05T11:00:00Z host app 123 ID47 - disk full")
        self.assertEqual(parsed["message"], "disk full")
        self.assertEqual(parsed["timestamp"], "2026-11-05T11:00:00Z")

    def test_rfc5424_structured_data(self):
        parsed = parse_line(
            b'<34>1 2026-11-05T11:00:00Z host app 123 ID47 '
            b'[exampleSDID@32473 iut="3" eventSource="App\\] x"] hello')
        self.assertEqual(parsed["message"], "hello")

    def test_rfc5424_nil_fields_become_none(self):
        parsed = parse_line(b"<34>1 - - - - - - msg")
        self.assertIsNone(parsed["host"])
        self.assertIsNone(parsed["tag"])
        self.assertIsNone(parsed["timestamp"])
        self.assertEqual(parsed["message"], "msg")

    def test_line_without_priority_keeps_the_raw_text(self):
        parsed = parse_line(b"plain message")
        self.assertIsNone(parsed["severity"])
        self.assertEqual(parsed["message"], "plain message")

    def test_invalid_utf8_does_not_raise(self):
        parsed = parse_line(b"<13>bad \xff\xfe bytes")
        self.assertIn("bad", parsed["message"])

    def test_odd_inputs_do_not_raise(self):
        for data in (b"<999>x", b"<abc>x", b"", b"<>", b"<1234>x", b"<34>1 ", b"<34>1 a b",
                     b"x" * 70000, b"<34>1 a b c d e [unterminated"):
            parsed = parse_line(data)
            self.assertEqual(set(parsed), {"facility", "severity", "timestamp", "host",
                                           "tag", "message", "raw"})
        self.assertIsNone(parse_line(b"<999>x")["severity"])
        self.assertEqual(parse_line(b"<999>x")["message"], "<999>x")
        self.assertIsNone(parse_line(b"<abc>x")["facility"])
        self.assertEqual(parse_line(b"<191>x")["severity"], 7)

    def test_raw_has_no_trailing_newline(self):
        self.assertEqual(parse_line(b"hello\r\n")["raw"], "hello")


class CollectorCase(unittest.TestCase):
    def make(self, **kwargs):
        collector = SyslogCollector(port=0, **kwargs)
        self.addCleanup(collector.stop)
        return collector, collector.start()

    def udp_send(self, port, data):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(data, ("127.0.0.1", port))
        finally:
            sock.close()

    def tcp(self, port):
        sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.addCleanup(sock.close)
        return sock


class SyslogCollectorTest(CollectorCase):
    def test_udp_message_is_collected(self):
        collector, port = self.make()
        self.udp_send(port, b"<189>Nov  5 11:00:00 sw1 tag: hello")
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 1))
        message = collector.messages()[0]
        self.assertEqual(message["source"], "127.0.0.1")
        self.assertEqual(message["host"], "sw1")
        self.assertIsInstance(message["received"], float)

    def test_ring_buffer_drops_the_oldest_message(self):
        collector, port = self.make(capacity=2)
        for text in (b"one", b"two", b"three"):
            self.udp_send(port, text)
            self.assertTrue(wait_for(lambda t=text: collector.messages()[-1:] and
                                     collector.messages()[-1]["raw"] == t.decode()))
        self.assertEqual([m["raw"] for m in collector.messages()], ["two", "three"])

    def test_udp_datagram_longer_than_max_line_is_truncated_and_service_survives(self):
        collector, port = self.make(max_line=1024)
        self.udp_send(port, b"A" * 4096)
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 1))
        self.assertEqual(len(collector.messages()[0]["raw"]), 1024)
        self.udp_send(port, b"after")
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 2))
        self.assertEqual(collector.messages()[1]["raw"], "after")

    def test_tcp_line_longer_than_max_line_is_truncated_and_stream_survives(self):
        collector, port = self.make(tcp=True, max_line=1024)
        sock = self.tcp(port)
        sock.sendall(b"B" * (200 * 1024) + b"\nnext line\n")
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 2))
        raws = [m["raw"] for m in collector.messages()]
        self.assertEqual(raws, ["B" * 1024, "next line"])

    def test_filters_select_by_text_severity_and_host(self):
        collector, port = self.make()
        for line in (b"<189>Nov  5 11:00:00 sw1 t: Link UP", b"<187>Nov  5 11:00:00 sw2 t: link down",
                     b"<190>Nov  5 11:00:00 sw1 t: info"):
            self.udp_send(port, line)
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 3))
        self.assertEqual(len(collector.messages(text="LINK")), 2)
        self.assertEqual(len(collector.messages(min_severity=3)), 1)
        self.assertEqual(len(collector.messages(host="SW1")), 2)

    def test_min_severity_semantics_and_host_fallback(self):
        collector, port = self.make()
        self.udp_send(port, b"<0>emerg")
        self.udp_send(port, b"<7>debug")
        self.udp_send(port, b"no priority")
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 3))
        self.assertEqual(len(collector.messages(min_severity=0)), 1)
        self.assertEqual(len(collector.messages(min_severity=7)), 2)
        self.assertEqual(len(collector.messages(min_severity=None)), 3)
        self.assertEqual(len(collector.messages(host="127.0.0.1")), 3)
        self.assertEqual(len(collector.messages(host="other")), 0)

    def test_returned_messages_cannot_mutate_the_ring(self):
        collector, port = self.make()
        self.udp_send(port, b"keep")
        self.assertTrue(wait_for(lambda: collector.messages()))
        collector.messages()[0]["raw"] = "changed"
        self.assertEqual(collector.messages()[0]["raw"], "keep")

    def test_default_max_line_is_8192_and_longer_datagrams_are_cut(self):
        default = inspect.signature(SyslogCollector).parameters["max_line"].default
        self.assertEqual(default, 8192)
        collector, port = self.make()
        self.udp_send(port, b"C" * 9000)
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 1))
        self.assertEqual(len(collector.messages()[0]["raw"]), 8192)

    def test_total_stored_text_is_bounded_by_max_bytes(self):
        collector = SyslogCollector(port=0, max_bytes=200_000, max_line=65536)
        for i in range(50):
            collector._ingest(b"%02d" % i + b"x" * 59_998, "1.2.3.4")
        stored = collector.messages()
        total = sum(len(m["raw"]) + len(m["message"]) for m in stored)
        self.assertLessEqual(total, 200_000)
        self.assertTrue(stored[-1]["raw"].startswith("49"))

    def test_message_larger_than_max_bytes_is_not_stored(self):
        collector = SyslogCollector(port=0, max_bytes=1000)
        collector._ingest(b"y" * 2000, "1.2.3.4")
        self.assertEqual(collector.messages(), [])

    def test_listen_failure_closes_the_sockets(self):
        collector = SyslogCollector(port=0, tcp=True)
        with mock.patch.object(socket.socket, "listen", side_effect=OSError("no")):
            with self.assertRaises(OSError):
                collector.start()
        collector.stop()

    def test_messages_returns_a_copy(self):
        collector, port = self.make()
        self.udp_send(port, b"x")
        self.assertTrue(wait_for(lambda: collector.messages()))
        snapshot = collector.messages()
        snapshot.clear()
        self.assertEqual(len(collector.messages()), 1)

    def test_callback_that_raises_does_not_stop_collection(self):
        seen = []

        def callback(message):
            seen.append(message["raw"])
            raise ValueError("boom")

        collector, port = self.make(on_message=callback)
        with self.assertLogs(syslog_server._log, "WARNING"):
            self.udp_send(port, b"first")
            self.assertTrue(wait_for(lambda: len(seen) == 1))
            self.udp_send(port, b"second")
            self.assertTrue(wait_for(lambda: len(seen) == 2))
            time.sleep(0.05)
        self.assertEqual(len(collector.messages()), 2)

    def test_tcp_mode_accepts_lf_framed_lines(self):
        collector, port = self.make(tcp=True)
        sock = self.tcp(port)
        sock.sendall(b"<13>one\r\n\n<13>two\n")
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 2))
        self.assertEqual([m["raw"] for m in collector.messages()], ["<13>one", "<13>two"])

    def test_tcp_mode_accepts_octet_counting_frames(self):
        collector, port = self.make(tcp=True)
        sock = self.tcp(port)
        body = b"<34>1 x y z -"
        frame = str(len(body)).encode() + b" " + body
        sock.sendall(frame + frame)
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 2))

    def test_tcp_octet_counting_frame_split_across_sends(self):
        collector, port = self.make(tcp=True)
        sock = self.tcp(port)
        sock.sendall(b"1")
        time.sleep(0.05)
        sock.sendall(b"1 hello")
        time.sleep(0.05)
        sock.sendall(b" worl")
        time.sleep(0.05)
        self.assertEqual(collector.messages(), [])
        sock.sendall(b"d")
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 1))
        self.assertEqual(collector.messages()[0]["raw"], "hello world")

    def test_tcp_octet_counting_with_an_absurd_length_closes_that_connection_only(self):
        collector, port = self.make(tcp=True, max_line=1024)
        bad = self.tcp(port)
        good = self.tcp(port)
        bad.sendall(b"999999 x")
        bad.settimeout(3)
        self.assertEqual(bad.recv(10), b"")
        good.sendall(b"ok line\n")
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 1))

    def test_tcp_octet_counting_malformed_headers_close_the_connection(self):
        collector, port = self.make(tcp=True)
        for payload in (b"12345678 x", b"1x"):
            sock = self.tcp(port)
            sock.sendall(payload)
            self.assertEqual(sock.recv(10), b"")

    def test_connection_over_the_limit_is_closed_at_once(self):
        with mock.patch.object(syslog_server, "MAX_TCP_CONNECTIONS", 3):
            collector, port = self.make(tcp=True)
            held = [self.tcp(port) for _ in range(3)]
            for sock in held:
                sock.sendall(b"hi\n")
            self.assertTrue(wait_for(lambda: len(collector.messages()) == 3))
            extra = self.tcp(port)
            self.assertEqual(extra.recv(10), b"")
            held[0].sendall(b"still\n")
            self.assertTrue(wait_for(lambda: len(collector.messages()) == 4))

    def test_idle_connection_is_closed(self):
        with mock.patch.object(syslog_server, "TCP_IDLE_SECONDS", 0.3):
            collector, port = self.make(tcp=True)
            sock = self.tcp(port)
            self.assertEqual(sock.recv(10), b"")

    def test_tcp_shares_the_udp_port_number(self):
        collector, port = self.make(tcp=True)
        self.udp_send(port, b"via udp")
        self.tcp(port).sendall(b"via tcp\n")
        self.assertTrue(wait_for(lambda: len(collector.messages()) == 2))

    def test_busy_port_raises_port_busy(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(holder.close)
        holder.bind(("0.0.0.0", 0))
        port = holder.getsockname()[1]
        collector = SyslogCollector(port=port)
        with self.assertRaises(PortBusy) as ctx:
            collector.start()
        self.assertEqual(ctx.exception.port, port)
        collector.stop()

    def test_busy_tcp_port_closes_the_udp_socket(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(holder.close)
        holder.bind(("0.0.0.0", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        collector = SyslogCollector(port=port, tcp=True)
        with self.assertRaises(PortBusy):
            collector.start()
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(probe.close)
        probe.bind(("0.0.0.0", port))
        collector.stop()

    def test_stop_is_idempotent_and_restart_works(self):
        collector = SyslogCollector(port=0, tcp=True)
        collector.stop()
        collector.start()
        collector.stop()
        collector.stop()
        port = collector.start()
        self.assertGreater(port, 0)
        collector.stop()

    def test_stop_closes_open_connections(self):
        collector, port = self.make(tcp=True)
        sock = self.tcp(port)
        sock.sendall(b"x\n")
        self.assertTrue(wait_for(lambda: collector.messages()))
        collector.stop()
        self.assertEqual(sock.recv(10), b"")


class SaveMessagesTest(unittest.TestCase):
    def test_save_messages_writes_every_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.log"
            count = save_messages([parse_line(b"<13>a"), parse_line(b"<13>b \xc3\xa8")], path)
            self.assertEqual(count, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "<13>a\n<13>b è\n")
            self.assertEqual(os.listdir(tmp), ["out.log"])

    def test_save_messages_replaces_a_symlink_instead_of_following_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_text("precious")
            link = Path(tmp) / "link"
            link.symlink_to(target)
            save_messages([parse_line(b"x")], link)
            self.assertFalse(link.is_symlink())
            self.assertEqual(link.read_text(), "x\n")
            self.assertEqual(target.read_text(), "precious")

    def test_save_messages_failure_removes_the_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("os.replace", side_effect=OSError("no")):
                with self.assertRaises(OSError):
                    save_messages([parse_line(b"x")], Path(tmp) / "out")
            self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
