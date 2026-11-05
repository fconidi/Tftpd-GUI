import errno
import socket
import unittest

from tftpd_gui.core import dhcp_packet as dp
from tftpd_gui.core.dhcp_probe import probe_segment


class ProbeSocket:
    def __init__(self, offers=(), bind_error=None):
        self.offers = list(offers)
        self.bind_error = bind_error
        self.sent = []
        self.xid = None
        self.closed = False
        self.opts = []

    def setsockopt(self, level, name, value):
        self.opts.append((name, value))

    def bind(self, addr):
        if self.bind_error:
            raise self.bind_error

    def settimeout(self, value):
        pass

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        self.xid = dp.decode(data).xid

    def recvfrom(self, size):
        if not self.offers:
            raise socket.timeout
        server, ip, good_xid = self.offers.pop(0)
        offer = dp.DhcpPacket(
            op=2, htype=1, hlen=6, hops=0, xid=self.xid if good_xid else self.xid ^ 1, secs=0,
            flags=0, ciaddr="0.0.0.0", yiaddr=ip, siaddr="0.0.0.0", giaddr="0.0.0.0",
            chaddr=b"\x00" * 16,
            options={53: bytes([dp.OFFER]), 54: dp.ip_to_bytes(server)})
        return dp.encode(offer), (server, 67)

    def close(self):
        self.closed = True


class ProbeTest(unittest.TestCase):
    def run_probe(self, sock, timeout=0.2):
        return probe_segment("eth0", timeout=timeout, socket_factory=lambda *a: sock)

    def test_an_offer_from_another_server_is_status_found(self):
        sock = ProbeSocket([("192.168.50.254", "192.168.50.50", True)])
        result = self.run_probe(sock)
        self.assertEqual(result.status, "found")
        self.assertEqual(result.servers[0]["server_id"], "192.168.50.254")
        self.assertEqual(result.servers[0]["offered_ip"], "192.168.50.50")
        self.assertTrue(sock.closed)

    def test_duplicate_and_foreign_xid_offers_are_ignored(self):
        sock = ProbeSocket([("10.0.0.1", "10.0.0.9", False), ("10.0.0.2", "10.0.0.8", True),
                            ("10.0.0.2", "10.0.0.7", True)])
        result = self.run_probe(sock)
        self.assertEqual([s["server_id"] for s in result.servers], ["10.0.0.2"])

    def test_silence_is_status_not_found(self):
        result = self.run_probe(ProbeSocket())
        self.assertEqual((result.status, result.servers), ("not_found", []))

    def test_port_68_already_in_use_is_status_error_with_a_reason(self):
        sock = ProbeSocket(bind_error=OSError(errno.EADDRINUSE, "Address already in use"))
        result = self.run_probe(sock)
        self.assertEqual(result.status, "error")
        self.assertIn("68", result.reason)
        self.assertTrue(sock.closed)

    def test_probe_only_sends_discover_never_request(self):
        sock = ProbeSocket([("10.0.0.2", "10.0.0.8", True)])
        self.run_probe(sock)
        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(dp.message_type(dp.decode(sock.sent[0][0])), dp.DISCOVER)
        self.assertEqual(sock.sent[0][1], ("255.255.255.255", 67))
        self.assertNotIn(socket.SO_REUSEADDR, [n for n, _ in sock.opts])


if __name__ == "__main__":
    unittest.main()
