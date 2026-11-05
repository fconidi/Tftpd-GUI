"""Isolated-network tests: two veth pairs, one namespace behind each.

Skipped unless the process is root and ``ip netns add`` works. Artifacts go to
a temporary directory under ~/.cache/tftpd-gui, never into the repository.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])
CACHE = Path.home() / ".cache" / "tftpd-gui"


def _can_use_netns() -> bool:
    if os.geteuid() != 0 or shutil.which("ip") is None:
        return False
    name = "tg-probe-" + uuid.uuid4().hex[:6]
    if subprocess.run(["ip", "netns", "add", name], capture_output=True).returncode != 0:
        return False
    subprocess.run(["ip", "netns", "del", name], capture_output=True)
    return True


CAN_USE_NETNS = _can_use_netns()
PRELUDE = f"import sys\nsys.path.insert(0, {REPO!r})\n"


def sh(*cmd):
    subprocess.run(cmd, check=True, capture_output=True)


@unittest.skipUnless(CAN_USE_NETNS, "needs root, ip and permission to create network namespaces")
class NetnsTest(unittest.TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex[:4]
        self.ns = {"a": f"tg-a-{suffix}", "b": f"tg-b-{suffix}"}
        self.host_if = {"a": f"tga0{suffix}", "b": f"tgb0{suffix}"}
        self.peer_if = {"a": f"tga1{suffix}", "b": f"tgb1{suffix}"}
        self.net = {"a": "192.168.50", "b": "192.168.60"}
        CACHE.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="netns-", dir=CACHE))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.addCleanup(self._teardown)
        for key in ("a", "b"):
            sh("ip", "netns", "add", self.ns[key])
            sh("ip", "link", "add", self.host_if[key], "type", "veth", "peer", "name", self.peer_if[key])
            sh("ip", "link", "set", self.peer_if[key], "netns", self.ns[key])
            sh("ip", "addr", "add", f"{self.net[key]}.1/24", "dev", self.host_if[key])
            sh("ip", "link", "set", self.host_if[key], "up")
            sh("ip", "-n", self.ns[key], "addr", "add", f"{self.net[key]}.2/24", "dev", self.peer_if[key])
            sh("ip", "-n", self.ns[key], "link", "set", self.peer_if[key], "up")
            sh("ip", "-n", self.ns[key], "link", "set", "lo", "up")

    def _teardown(self):
        for key in ("a", "b"):
            subprocess.run(["ip", "link", "del", self.host_if[key]], capture_output=True)
            subprocess.run(["ip", "netns", "del", self.ns[key]], capture_output=True)

    def in_ns(self, key, script, timeout=30):
        done = subprocess.run(
            ["ip", "netns", "exec", self.ns[key], sys.executable, "-I", "-"],
            input=PRELUDE + textwrap.dedent(script), capture_output=True, text=True, timeout=timeout)
        return done.stdout.strip(), done.stderr.strip()

    def discover_from(self, key, wait=3.0):
        out, err = self.in_ns(key, f"""
            import os, socket, time
            from tftpd_gui.core import dhcp_packet as dp
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, {self.peer_if[key]!r}.encode() + b"\\0")
            sock.bind(("0.0.0.0", 68))
            mac = bytes.fromhex("02aabbccdd01")
            pkt = dp.DhcpPacket(op=1, htype=1, hlen=6, hops=0, xid=77, secs=0, flags=0x8000,
                ciaddr="0.0.0.0", yiaddr="0.0.0.0", siaddr="0.0.0.0", giaddr="0.0.0.0",
                chaddr=mac.ljust(16, b"\\0"), options={{53: bytes([dp.DISCOVER])}})
            sock.sendto(dp.encode(pkt), ("255.255.255.255", 67))
            sock.settimeout({wait})
            try:
                reply = dp.decode(sock.recvfrom(4096)[0])
                print("OFFER", reply.yiaddr)
            except socket.timeout:
                print("NONE")
        """, timeout=30)
        self.assertNotIn("Traceback", err, err)
        return out

    def start_dhcp(self, key="a", server_id=1, first=100, last=110):
        from tftpd_gui.core.dhcp_pool import LeasePool
        from tftpd_gui.core.dhcp_server import DhcpServer
        pool = LeasePool(f"{self.net[key]}.{first}", f"{self.net[key]}.{last}", "255.255.255.0")
        server = DhcpServer(pool, interface=self.host_if[key], server_ip=f"{self.net[key]}.{server_id}",
                            netmask="255.255.255.0", auto_stop_seconds=None)
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_dhcp_offer_reaches_the_client_on_the_chosen_interface(self):
        self.start_dhcp()
        self.assertEqual(self.discover_from("a"), "OFFER 192.168.50.100")

    def test_dhcp_offer_does_not_reach_a_client_on_another_interface(self):
        self.start_dhcp()
        self.assertEqual(self.discover_from("b"), "NONE")

    def probe_from(self, key, timeout=3.0):
        # Runs in the namespace so a DHCP client holding port 68 on the host cannot interfere.
        out, err = self.in_ns(key, f"""
            from tftpd_gui.core.dhcp_probe import probe_segment
            result = probe_segment({self.peer_if[key]!r}, timeout={timeout})
            print(result.status, ",".join(s["server_id"] for s in result.servers))
        """)
        self.assertNotIn("Traceback", err, err)
        return out

    def test_probe_sees_a_second_dhcp_server_on_the_segment(self):
        self.start_dhcp()
        self.assertEqual(self.probe_from("a"), "found 192.168.50.1")
        self.assertEqual(self.probe_from("b", timeout=1.5), "not_found")

    def test_probe_reports_error_when_port_68_is_taken(self):
        out, err = self.in_ns("a", f"""
            import socket
            from tftpd_gui.core.dhcp_probe import probe_segment
            holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            holder.bind(("0.0.0.0", 68))
            result = probe_segment({self.peer_if["a"]!r}, timeout=1.0)
            print(result.status, result.reason)
        """)
        self.assertTrue(out.startswith("error cannot listen on UDP port 68"), out + err)

    def tftp_get_from(self, key, host, port):
        out, err = self.in_ns(key, f"""
            from pathlib import Path
            from tftpd_gui.core import tftp_client
            try:
                size = tftp_client.get({host!r}, "f.txt", Path({str(self.work / ("got-" + key))!r}),
                                       port={port}, timeout=1.0, retries=1)
                print("OK", size)
            except Exception as exc:
                print("FAIL", type(exc).__name__)
        """)
        self.assertNotIn("Traceback", err, err)
        return out

    def test_tftp_served_on_one_interface_is_not_reachable_on_the_other(self):
        from tftpd_gui.core.tftp_server import TftpServer
        root = self.work / "root"
        root.mkdir()
        (root / "f.txt").write_text("hello")
        server = TftpServer(root, port=0, bind_device=self.host_if["a"])
        port = server.start()
        self.addCleanup(server.stop)
        self.assertEqual(self.tftp_get_from("a", f"{self.net['a']}.1", port), "OK 5")
        self.assertTrue(self.tftp_get_from("b", f"{self.net['b']}.1", port).startswith("FAIL"))

    def test_helper_is_gone_after_its_client_is_killed(self):
        script = textwrap.dedent(f"""
            import sys, tempfile, time
            sys.path.insert(0, {REPO!r})
            from tftpd_gui.gui.helper_client import HelperClient
            c = HelperClient([sys.executable, "-m", "tftpd_gui.helper"])
            c.start()
            port = c.call("start_tftp", root=tempfile.mkdtemp(dir={str(self.work)!r}), port=0,
                          interface={self.host_if["a"]!r})["port"]
            print(c.pid, port, flush=True)
            time.sleep(60)
        """)
        parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, cwd=REPO)
        self.addCleanup(parent.stdout.close)
        self.addCleanup(parent.wait)
        pid, port = map(int, parent.stdout.readline().split())
        parent.send_signal(signal.SIGKILL)
        parent.wait()
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                with open(f"/proc/{pid}/stat") as handle:
                    if handle.read().split()[2] == "Z":
                        break
            except FileNotFoundError:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("helper survived its client")
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind(("0.0.0.0", port))


if __name__ == "__main__":
    unittest.main()
