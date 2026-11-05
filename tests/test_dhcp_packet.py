import unittest

from tftpd_gui.core.dhcp_packet import (
    ACK,
    DISCOVER,
    MAGIC,
    DhcpError,
    DhcpPacket,
    bytes_to_ip,
    decode,
    encode,
    encode_options,
    ip_to_bytes,
    message_type,
)

# A DHCPDISCOVER as a typical Linux client sends it (broadcast flag set).
_HEAD = "01010600" "3903f326" "0000" "8000" + "00" * 16
_CHADDR = "0050568a1b2c" + "00" * 10
_OPTIONS = "63825363" "350101" "3d0701" "0050568a1b2c" "370401030615" "ff"
DISCOVER_HEX = (_HEAD + _CHADDR + "00" * 192 + _OPTIONS).ljust(600, "0")


def _sample() -> DhcpPacket:
    return DhcpPacket(
        op=2, htype=1, hlen=6, hops=0, xid=0x3903F326, secs=0, flags=0x8000,
        ciaddr="0.0.0.0", yiaddr="192.168.50.20", siaddr="192.168.50.1",
        giaddr="0.0.0.0", chaddr=bytes.fromhex("0050568a1b2c") + b"\x00" * 10,
        options={53: bytes([ACK]), 54: ip_to_bytes("192.168.50.1"), 51: b"\x00\x00\x02\x58"},
    )


class DhcpPacketTest(unittest.TestCase):
    def test_discover_fixture_is_decoded(self):
        packet = decode(bytes.fromhex(DISCOVER_HEX))
        self.assertEqual(message_type(packet), DISCOVER)
        self.assertEqual(packet.chaddr[:6].hex(), "0050568a1b2c")
        self.assertEqual(packet.xid, 0x3903F326)
        self.assertEqual(packet.flags, 0x8000)
        self.assertEqual(packet.options[55], bytes([1, 3, 6, 21]))

    def test_bad_magic_cookie_raises(self):
        raw = bytearray(bytes.fromhex(DISCOVER_HEX))
        raw[236] ^= 0xFF
        with self.assertRaises(DhcpError):
            decode(bytes(raw))

    def test_truncated_option_list_raises(self):
        raw = encode(_sample())[:240] + bytes([12, 20]) + b"abc"
        with self.assertRaises(DhcpError):
            decode(raw)

    def test_option_without_length_raises(self):
        with self.assertRaises(DhcpError):
            decode(encode(_sample())[:240] + bytes([12]))

    def test_missing_end_option_raises(self):
        raw = encode(_sample())[:240] + bytes([53, 1, ACK])
        with self.assertRaises(DhcpError):
            decode(raw)

    def test_short_packet_raises(self):
        for size in (0, 100, 239):
            with self.assertRaises(DhcpError):
                decode(b"\x01" * size)

    def test_round_trip_preserves_fields_and_options(self):
        packet = _sample()
        self.assertEqual(decode(encode(packet)), packet)

    def test_encoded_packet_is_at_least_300_bytes(self):
        self.assertGreaterEqual(len(encode(_sample())), 300)

    def test_padding_option_is_skipped(self):
        raw = encode(_sample())[:240] + b"\x00\x00" + bytes([53, 1, ACK, 255])
        self.assertEqual(message_type(decode(raw)), ACK)

    def test_message_type_missing_or_malformed_is_none(self):
        packet = _sample()
        packet.options = {}
        self.assertIsNone(message_type(packet))
        packet.options = {53: b"\x01\x02"}
        self.assertIsNone(message_type(packet))

    def test_ip_helpers(self):
        self.assertEqual(bytes_to_ip(ip_to_bytes("10.0.0.7")), "10.0.0.7")
        for bad in ("10.0.0", "300.1.1.1", "", "a.b.c.d"):
            with self.assertRaises(DhcpError):
                ip_to_bytes(bad)
        with self.assertRaises(DhcpError):
            bytes_to_ip(b"\x01\x02")

    def test_encode_options_rejects_oversize_and_reserved(self):
        with self.assertRaises(DhcpError):
            encode_options({12: b"x" * 256})
        with self.assertRaises(DhcpError):
            encode_options({255: b""})
        self.assertEqual(encode_options({53: b"\x01"}), b"\x35\x01\x01\xff")

    def test_magic_constant(self):
        self.assertEqual(MAGIC, b"\x63\x82\x53\x63")


if __name__ == "__main__":
    unittest.main()
