import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from tftpd_gui.gui.helper_client import HelperClient, HelperError

COMMAND = [sys.executable, "-m", "tftpd_gui.helper"]


def port_is_free(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


class HelperClientTest(unittest.TestCase):
    def client(self):
        client = HelperClient(COMMAND)
        client.start()
        self.addCleanup(client.close)
        return client

    def test_round_trip_start_and_stop_tftp(self):
        client = self.client()
        with tempfile.TemporaryDirectory() as root:
            port = client.call("start_tftp", root=root, port=0)["port"]
            self.assertFalse(port_is_free(port))
            self.assertTrue(client.call("status")["tftp"]["running"])
            client.call("stop_tftp")
            self.assertTrue(port_is_free(port))

    def test_error_from_the_helper_raises_helper_error_with_its_code(self):
        client = self.client()
        with self.assertRaises(HelperError) as ctx:
            client.call("nonsense")
        self.assertEqual(ctx.exception.code, "unknown_command")

    def test_events_are_delivered_by_poll_events(self):
        client = self.client()
        port = client.call("start_syslog", port=0)["port"]
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        sender.sendto(b"<134>Nov  5 10:00:00 sw1 hello", ("127.0.0.1", port))
        deadline = time.time() + 5
        events = []
        while time.time() < deadline and not events:
            events = client.poll_events()
            time.sleep(0.05)
        self.assertEqual(events[0]["event"], "syslog")

    def test_close_leaves_no_child_process_and_frees_the_port(self):
        client = HelperClient(COMMAND)
        client.start()
        proc = client._proc
        with tempfile.TemporaryDirectory() as root:
            port = client.call("start_tftp", root=root, port=0)["port"]
            client.close()
        self.assertIsNotNone(proc.poll())
        self.assertTrue(port_is_free(port))
        self.assertIsNone(client.pid)

    def test_call_after_the_helper_died_raises(self):
        client = self.client()
        os.kill(client.pid, signal.SIGKILL)
        client._proc.wait()
        with self.assertRaises(HelperError) as ctx:
            client.call("status")
        self.assertIn(ctx.exception.code, ("helper_exited", "access_denied"))

    def test_missing_command_raises_helper_unavailable(self):
        with self.assertRaises(HelperError) as ctx:
            HelperClient(["/nonexistent/helper"]).start()
        self.assertEqual(ctx.exception.code, "helper_unavailable")

    def test_exit_code_126_maps_to_access_denied(self):
        client = HelperClient([sys.executable, "-c", "import sys; sys.exit(126)"])
        client.start()
        self.addCleanup(client.close)
        with self.assertRaises(HelperError) as ctx:
            client.call("status")
        self.assertEqual(ctx.exception.code, "access_denied")

    def test_killing_the_client_process_makes_the_helper_exit(self):
        script = textwrap.dedent("""
            import sys, tempfile, time
            from tftpd_gui.gui.helper_client import HelperClient
            c = HelperClient([sys.executable, "-m", "tftpd_gui.helper"])
            c.start()
            port = c.call("start_tftp", root=tempfile.mkdtemp(), port=0)["port"]
            print(c.pid, port, flush=True)
            time.sleep(60)
        """)
        parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(parent.stdout.close)
        self.addCleanup(parent.wait)
        helper_pid, port = map(int, parent.stdout.readline().split())
        parent.kill()
        parent.wait()
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                os.kill(helper_pid, 0)
                with open(f"/proc/{helper_pid}/stat") as handle:
                    if handle.read().split()[2] == "Z":
                        break
            except (ProcessLookupError, FileNotFoundError):
                break
            time.sleep(0.05)
        else:
            os.kill(helper_pid, signal.SIGKILL)
            self.fail("helper survived its client")
        self.assertTrue(port_is_free(port))


if __name__ == "__main__":
    unittest.main()
