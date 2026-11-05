import tempfile
import unittest
from pathlib import Path

from tftpd_gui.core.ifaces import interface_ipv4, list_interfaces


class IfacesTest(unittest.TestCase):
    def _sysfs(self, entries):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, has_device in entries.items():
            (root / name).mkdir()
            if has_device:
                (root / name / "device").symlink_to("../../devices/pci0000:00")
        return root

    def test_loopback_and_entries_without_a_device_link_are_skipped(self):
        root = self._sysfs({"lo": True, "docker0": False, "eth0": True})
        self.assertEqual(list_interfaces(root), ["eth0"])

    def test_interfaces_are_sorted(self):
        root = self._sysfs({"enp3s0": True, "eth1": True, "eno1": True})
        self.assertEqual(list_interfaces(root), ["eno1", "enp3s0", "eth1"])

    def test_missing_sysfs_gives_empty_list(self):
        self.assertEqual(list_interfaces(Path("/nonexistent/net")), [])

    def test_interface_ipv4_of_loopback(self):
        self.assertEqual(interface_ipv4("lo"), ("127.0.0.1", "255.0.0.0"))

    def test_interface_without_ipv4_returns_none(self):
        self.assertIsNone(interface_ipv4("nonexist0"))
