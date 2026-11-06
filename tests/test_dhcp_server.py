import errno
import socket
import unittest

from tftpd_gui.core import dhcp_packet as dp
from tftpd_gui.core.dhcp_packet import DhcpPacket, ip_to_bytes
from tftpd_gui.core.dhcp_pool import LeasePool
from tftpd_gui.core.dhcp_server import DhcpServer, validate_config
from tftpd_gui.core.errors import DhcpConfigError, PortBusy

SERVER = "192.168.50.1"
OTHER = "192.168.50.2"
MAC1 = bytes.fromhex("020000000001")
MAC2 = bytes.fromhex("020000000002")
ZERO = "0.0.0.0"


def request(kind, mac=MAC1, ciaddr=ZERO, giaddr=ZERO, flags=0x8000, **options):
    opts = {53: bytes([kind])}
    for key, value in options.items():
        opts[int(key[1:])] = value
    return DhcpPacket(op=1, htype=1, hlen=6, hops=0, xid=0x1234, secs=0, flags=flags,
                      ciaddr=ciaddr, yiaddr=ZERO, siaddr=ZERO, giaddr=giaddr,
                      chaddr=mac.ljust(16, b"\x00"), options=opts)


def ip(text):
    return ip_to_bytes(text)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class FakeSocket:
    def __init__(self, bind_error=None):
        self.opts = []
        self.bound = None
        self.sent = []
        self.bind_error = bind_error
        self.closed = False

    def setsockopt(self, level, name, value):
        self.opts.append((level, name, value))

    def bind(self, addr):
        if self.bind_error:
            raise self.bind_error
        self.bound = addr

    def getsockname(self):
        return self.bound

    def settimeout(self, value):
        pass

    def recvfrom(self, size):
        raise socket.timeout

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        self.closed = True


class DhcpServerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.pool = LeasePool("192.168.50.100", "192.168.50.110", "255.255.255.0", clock=self.clock)
        self.server = self.make()

    def make(self, **kw):
        args = dict(interface="eth0", server_ip=SERVER, netmask="255.255.255.0",
                    router=SERVER, dns=("8.8.8.8",), on_event=self.events.append, clock=self.clock)
        args.update(kw)
        return DhcpServer(self.pool, **args)

    def reply(self, packet):
        raw = self.server.build_reply(packet)
        return None if raw is None else dp.decode(raw)

    def offer_for(self, mac=MAC1):
        offer = self.reply(request(dp.DISCOVER, mac))
        return dp.bytes_to_ip(offer.options[50]) if 50 in offer.options else offer.yiaddr

    def test_discover_is_answered_with_an_offer(self):
        reply = self.reply(request(dp.DISCOVER))
        self.assertEqual(dp.message_type(reply), dp.OFFER)
        self.assertEqual(reply.yiaddr, "192.168.50.100")
        self.assertEqual(reply.options[54], ip(SERVER))
        self.assertEqual(int.from_bytes(reply.options[51], "big"), 600)
        self.assertEqual(reply.options[1], ip("255.255.255.0"))
        self.assertEqual(reply.options[3], ip(SERVER))
        self.assertEqual(reply.options[6], ip("8.8.8.8"))
        self.assertEqual((reply.op, reply.xid, reply.chaddr[:6]), (2, 0x1234, MAC1))

    def test_selecting_request_for_us_is_acknowledged(self):
        wanted = self.offer_for()
        reply = self.reply(request(dp.REQUEST, o54=ip(SERVER), o50=ip(wanted)))
        self.assertEqual(dp.message_type(reply), dp.ACK)
        self.assertEqual(reply.yiaddr, wanted)

    def test_selecting_request_naming_another_server_gets_no_reply(self):
        wanted = self.offer_for()
        self.assertIsNone(self.reply(request(dp.REQUEST, o54=ip(OTHER), o50=ip(wanted))))
        self.assertIsNone(self.pool.owner(wanted))

    def test_init_reboot_request_for_our_lease_is_acknowledged(self):
        wanted = self.offer_for()
        reply = self.reply(request(dp.REQUEST, o50=ip(wanted)))
        self.assertEqual(dp.message_type(reply), dp.ACK)

    def test_init_reboot_for_a_free_address_in_the_pool_is_acknowledged(self):
        reply = self.reply(request(dp.REQUEST, o50=ip("192.168.50.105")))
        self.assertEqual(dp.message_type(reply), dp.ACK)
        self.assertEqual(self.pool.owner("192.168.50.105"), MAC1)

    def test_init_reboot_request_for_an_address_taken_by_another_client_is_nakked(self):
        taken = self.offer_for(MAC2)
        reply = self.reply(request(dp.REQUEST, MAC1, o50=ip(taken)))
        self.assertEqual(dp.message_type(reply), dp.NAK)
        self.assertEqual(reply.yiaddr, ZERO)

    def test_init_reboot_request_for_an_address_outside_our_pool_is_ignored(self):
        self.assertIsNone(self.reply(request(dp.REQUEST, o50=ip("10.1.1.1"))))
        self.assertIsNone(self.reply(request(dp.REQUEST, o50=ip("192.168.50.200"))))

    def test_renewing_request_with_ciaddr_for_our_lease_is_acknowledged(self):
        wanted = self.offer_for()
        packet = request(dp.REQUEST, ciaddr=wanted, flags=0)
        raw = self.server._reply(packet)
        self.assertEqual(dp.message_type(dp.decode(raw[0])), dp.ACK)
        self.assertEqual(raw[1], wanted)  # unicast to the client

    def test_renewing_request_for_an_address_outside_our_pool_is_ignored(self):
        self.assertIsNone(self.reply(request(dp.REQUEST, ciaddr="10.1.1.1")))

    def test_request_without_any_address_is_ignored(self):
        self.assertIsNone(self.reply(request(dp.REQUEST)))

    def test_selecting_request_with_ciaddr_set_is_ignored(self):
        self.assertIsNone(self.reply(request(dp.REQUEST, ciaddr="192.168.50.100",
                                             o54=ip(SERVER), o50=ip("192.168.50.100"))))

    def test_relayed_packet_with_giaddr_is_ignored(self):
        self.assertIsNone(self.reply(request(dp.DISCOVER, giaddr="10.9.9.9")))

    def test_bootreply_from_another_server_is_ignored(self):
        packet = request(dp.DISCOVER)
        packet.op = 2
        self.assertIsNone(self.reply(packet))

    def test_decline_marks_the_address_unavailable(self):
        wanted = self.offer_for()
        self.assertIsNone(self.reply(request(dp.DECLINE, o54=ip(SERVER), o50=ip(wanted))))
        self.assertNotEqual(self.pool.acquire(MAC1), wanted)

    def test_request_for_declined_address_does_not_consume_another_lease(self):
        wanted = self.offer_for()
        self.reply(request(dp.DECLINE, o54=ip(SERVER), o50=ip(wanted)))
        for mac in (MAC1, MAC2):
            reply = self.reply(request(dp.REQUEST, mac, o54=ip(SERVER), o50=ip(wanted)))
            self.assertEqual(dp.message_type(reply), dp.NAK)
        self.assertEqual(self.pool.leases(), [])

    def test_decline_naming_another_server_is_ignored(self):
        wanted = self.offer_for()
        self.reply(request(dp.DECLINE, o54=ip(OTHER), o50=ip(wanted)))
        self.assertEqual(self.pool.owner(wanted), MAC1)

    def test_release_frees_the_lease(self):
        wanted = self.offer_for()
        self.reply(request(dp.RELEASE, ciaddr=wanted, o54=ip(SERVER)))
        self.assertIsNone(self.pool.owner(wanted))

    def test_release_by_another_client_is_ignored(self):
        wanted = self.offer_for()
        self.reply(request(dp.RELEASE, MAC2, ciaddr=wanted, o54=ip(SERVER)))
        self.assertEqual(self.pool.owner(wanted), MAC1)

    def test_inform_is_acknowledged_without_yiaddr(self):
        reply = self.reply(request(dp.INFORM, ciaddr="192.168.50.77"))
        self.assertEqual(dp.message_type(reply), dp.ACK)
        self.assertEqual(reply.yiaddr, ZERO)
        self.assertNotIn(51, reply.options)

    def test_unknown_message_type_is_ignored(self):
        self.assertIsNone(self.reply(request(99)))
        packet = request(dp.DISCOVER)
        packet.options = {}
        self.assertIsNone(self.reply(packet))

    def test_garbage_datagram_is_dropped_without_an_exception(self):
        sock = FakeSocket()
        for data in (b"", b"\x01" * 50, b"\xff" * 400, bytes(range(256)) * 3):
            self.server._handle(data, sock)
        self.assertEqual(sock.sent, [])

    def test_valid_datagram_is_sent_to_broadcast_on_the_client_port(self):
        sock = FakeSocket()
        self.server._handle(dp.encode(request(dp.DISCOVER)), sock)
        self.assertEqual(sock.sent[0][1], ("255.255.255.255", 68))

    def test_exhausted_pool_emits_an_event(self):
        pool = LeasePool("192.168.50.100", "192.168.50.100", "255.255.255.0", clock=self.clock)
        server = DhcpServer(pool, interface="eth0", server_ip=SERVER, netmask="255.255.255.0",
                            on_event=self.events.append, clock=self.clock)
        server.build_reply(request(dp.DISCOVER, MAC1))
        self.assertIsNone(server.build_reply(request(dp.DISCOVER, MAC2)))
        self.assertIn("pool_exhausted", [e["event"] for e in self.events])

    def test_offer_includes_next_server_and_bootfile_when_configured(self):
        self.server = self.make(next_server="192.168.50.1", bootfile="pxelinux.0")
        reply = self.reply(request(dp.DISCOVER))
        self.assertEqual(reply.siaddr, "192.168.50.1")
        self.assertEqual(reply.options[66], b"192.168.50.1")
        self.assertEqual(reply.options[67], b"pxelinux.0")

    def test_auto_stop_fires_when_the_clock_passes_the_limit(self):
        self.server = self.make(auto_stop_seconds=60)
        self.server.tick()
        self.assertEqual(self.events, [])
        self.clock.now = 61
        self.server.tick()
        self.server.tick()
        self.assertEqual(self.events, [{"event": "stopped", "reason": "timeout"}])

    def test_auto_stop_closes_the_listening_socket(self):
        fake = FakeSocket()
        self.server = self.make(auto_stop_seconds=60, socket_factory=lambda *_a: fake)
        self.server.start()
        self.clock.now = 61
        self.server.tick()
        self.assertTrue(fake.closed)
        self.assertIsNone(self.server._sock)

    def test_auto_stop_can_be_disabled(self):
        self.server = self.make(auto_stop_seconds=None)
        self.clock.now = 10 ** 6
        self.server.tick()
        self.assertEqual(self.events, [])

    def test_socket_is_bound_to_the_named_device_and_not_reuseaddr(self):
        fake = FakeSocket()
        self.server = self.make(socket_factory=lambda *a: fake, port=6767)
        self.assertEqual(self.server.start(), 6767)
        self.addCleanup(self.server.stop)
        self.assertIn((socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"eth0\0"), fake.opts)
        self.assertNotIn(socket.SO_REUSEADDR, [name for _, name, _ in fake.opts])
        self.assertEqual(fake.bound, ("0.0.0.0", 6767))

    def test_busy_port_raises_port_busy(self):
        fake = FakeSocket(bind_error=OSError(errno.EADDRINUSE, "in use"))
        self.server = self.make(socket_factory=lambda *a: fake, port=67)
        with self.assertRaises(PortBusy) as ctx:
            self.server.start()
        self.assertEqual(ctx.exception.port, 67)
        self.assertTrue(fake.closed)

    def test_start_refuses_an_invalid_configuration(self):
        fake = FakeSocket()
        self.server = self.make(socket_factory=lambda *a: fake, server_ip="")
        with self.assertRaises(DhcpConfigError) as ctx:
            self.server.start()
        self.assertEqual(ctx.exception.code, "no_ipv4")
        self.assertIsNone(fake.bound)

    def test_start_binds_a_real_socket_and_stop_closes_it(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(probe.close)
        probe.bind(("127.0.0.1", 0))
        probe.settimeout(2)
        server = self.make(port=0, client_port=probe.getsockname()[1],
                           socket_factory=lambda *a: _LoopbackSocket(), server_ip=SERVER)
        port = server.start()
        self.addCleanup(server.stop)
        self.assertGreater(port, 0)


class _LoopbackSocket(socket.socket):
    """Real socket where SO_BINDTODEVICE is skipped (needs an interface name)."""

    def __init__(self):
        super().__init__(socket.AF_INET, socket.SOCK_DGRAM)

    def setsockopt(self, level, name, value):
        if name == socket.SO_BINDTODEVICE:
            return
        super().setsockopt(level, name, value)


class ValidateConfigTest(unittest.TestCase):
    ARGS = ("192.168.50.1", "255.255.255.0", "192.168.50.100", "192.168.50.110", "192.168.50.1", ("8.8.8.8",))

    def check(self, code, **changes):
        names = ["server_ip", "netmask", "start", "end", "router", "dns"]
        args = dict(zip(names, self.ARGS))
        args.update(changes)
        with self.assertRaises(DhcpConfigError) as ctx:
            validate_config(**args)
        self.assertEqual(ctx.exception.code, code)

    def test_a_valid_configuration_passes(self):
        validate_config(*self.ARGS)
        validate_config("192.168.50.1", "255.255.255.0", "192.168.50.100", "192.168.50.110", None, ())

    def test_no_ipv4(self):
        self.check("no_ipv4", server_ip="")

    def test_pool_problems_are_refused(self):
        self.check("pool_reversed", start="192.168.50.110", end="192.168.50.100")
        self.check("pool_outside_subnet", end="192.168.51.5")
        self.check("pool_outside_subnet", end="192.168.50.255")
        self.check("pool_outside_subnet", start="192.168.50.0")
        self.check("pool_outside_subnet", start="nonsense")
        self.check("pool_contains_server", start="192.168.50.1", end="192.168.50.10")

    def test_gateway_outside_the_subnet_and_ipv6_dns_are_refused(self):
        self.check("gateway_outside_subnet", router="10.0.0.1")
        self.check("gateway_outside_subnet", router="bad")
        self.check("dns_invalid", dns=("2001:db8::1",))
        self.check("dns_invalid", dns=("8.8.8.8", "x"))

    def test_gateway_inside_pool_is_refused(self):
        self.check("pool_contains_gateway", router="192.168.50.105")

    def test_invalid_netmask_is_refused(self):
        self.check("mask_invalid", netmask="255.0.255.0")
        self.check("mask_invalid", netmask="garbage")
        self.check("mask_invalid", netmask="255.255.255.255")


if __name__ == "__main__":
    unittest.main()
