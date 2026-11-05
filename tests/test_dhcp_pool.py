import unittest

from tftpd_gui.core.dhcp_pool import LeasePool

A, B, C = b"\x02\x00\x00\x00\x00\x01", b"\x02\x00\x00\x00\x00\x02", b"\x02\x00\x00\x00\x00\x03"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LeasePoolTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    def pool(self, end="192.168.50.102"):
        return LeasePool("192.168.50.100", end, "255.255.255.0", lease_seconds=600, clock=self.clock)

    def test_same_mac_gets_the_same_address_twice(self):
        pool = self.pool()
        self.assertEqual(pool.acquire(A), pool.acquire(A))

    def test_requested_address_is_honoured_when_free(self):
        self.assertEqual(self.pool().acquire(A, "192.168.50.101"), "192.168.50.101")

    def test_requested_address_taken_or_outside_gives_another(self):
        pool = self.pool()
        pool.acquire(A, "192.168.50.101")
        self.assertEqual(pool.acquire(B, "192.168.50.101"), "192.168.50.100")
        self.assertEqual(pool.acquire(C, "10.0.0.5"), "192.168.50.102")

    def test_exhausted_pool_returns_none(self):
        pool = self.pool("192.168.50.101")
        pool.acquire(A)
        pool.acquire(B)
        self.assertIsNone(pool.acquire(C))

    def test_expired_lease_is_reused_for_another_mac(self):
        pool = self.pool("192.168.50.100")
        first = pool.acquire(A)
        self.assertIsNone(pool.acquire(B))
        self.clock.now += 601
        self.assertEqual(pool.acquire(B), first)

    def test_release_frees_the_address(self):
        pool = self.pool("192.168.50.100")
        ip = pool.acquire(A)
        pool.release(A)
        self.assertEqual(pool.acquire(B), ip)

    def test_decline_blocks_the_address_for_one_lease_time(self):
        pool = self.pool("192.168.50.100")
        ip = pool.acquire(A)
        pool.decline(ip)
        self.assertIsNone(pool.acquire(A))
        self.clock.now += 601
        self.assertEqual(pool.acquire(A), ip)

    def test_confirm_is_true_only_for_the_owner(self):
        pool = self.pool()
        ip = pool.acquire(A)
        self.assertTrue(pool.confirm(A, ip))
        self.assertFalse(pool.confirm(B, ip))
        self.assertFalse(pool.confirm(A, "192.168.50.102"))

    def test_confirm_extends_the_lease(self):
        pool = self.pool()
        ip = pool.acquire(A)
        self.clock.now += 400
        self.assertTrue(pool.confirm(A, ip))
        self.clock.now += 400
        self.assertEqual(pool.owner(ip), A)

    def test_owner_contains_and_leases(self):
        pool = self.pool()
        ip = pool.acquire(A, hostname="sw1")
        self.assertEqual(pool.owner(ip), A)
        self.assertIsNone(pool.owner("192.168.50.102"))
        self.assertTrue(pool.contains("192.168.50.101"))
        self.assertFalse(pool.contains("192.168.50.103"))
        self.assertFalse(pool.contains("garbage"))
        lease = pool.leases()[0]
        self.assertEqual((lease["mac"], lease["ip"], lease["hostname"]), ("02:00:00:00:00:01", ip, "sw1"))


if __name__ == "__main__":
    unittest.main()
