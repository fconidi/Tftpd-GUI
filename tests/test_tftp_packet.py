import random
import unittest

from netserve.core.tftp_packet import (
    OACK,
    RRQ,
    Packet,
    TftpError,
    decode,
    encode_ack,
    encode_data,
    encode_error,
    encode_oack,
    encode_request,
)


class TftpPacketTest(unittest.TestCase):
    def test_request_round_trip_keeps_options_lowercase(self):
        raw = encode_request(RRQ, "ios.bin", "octet", {"BLKSIZE": "1428", "tsize": "0"})
        packet = decode(raw)
        self.assertEqual((packet.opcode, packet.filename, packet.mode), (RRQ, "ios.bin", "octet"))
        self.assertEqual(packet.options, {"blksize": "1428", "tsize": "0"})

    def test_data_and_ack_bytes_are_exact(self):
        self.assertEqual(encode_data(1, b"xy"), b"\x00\x03\x00\x01xy")
        self.assertEqual(encode_ack(513), b"\x00\x04\x02\x01")

    def test_error_packet_is_nul_terminated(self):
        self.assertEqual(encode_error(2, "no"), b"\x00\x05\x00\x02no\x00")

    def test_truncated_packet_raises_illegal_operation(self):
        with self.assertRaises(TftpError) as caught:
            decode(b"\x00")
        self.assertEqual(caught.exception.code, 4)

    def test_unknown_opcode_raises_illegal_operation(self):
        with self.assertRaises(TftpError) as caught:
            decode(b"\x00\x09rest")
        self.assertEqual(caught.exception.code, 4)

    def test_request_without_terminating_nul_raises(self):
        with self.assertRaises(TftpError):
            decode(b"\x00\x01ios.bin")

    def _assert_decodes_or_tftp_error(self, data):
        try:
            result = decode(data)
        except TftpError:
            return
        except Exception as exc:  # pragma: no cover
            self.fail(f"decode({data!r}) raised {exc!r}")
        self.assertIsInstance(result, Packet)

    def test_decode_never_raises_other_exceptions(self):
        tails = [b"", b"\x00", b"a", b"\x00\x00", b"a\x00", b"\xff\xfe"]
        for opcode in range(8):
            head = opcode.to_bytes(2, "big")
            for tail in tails:
                self._assert_decodes_or_tftp_error(head + tail)
        for n in (1, 2, 3):
            for first in range(256):
                self._assert_decodes_or_tftp_error(bytes([first, 3, 0][:n]))
        rng = random.Random(1234)
        for _ in range(500):
            self._assert_decodes_or_tftp_error(rng.randbytes(rng.randint(0, 64)))

    def test_four_byte_error_packet(self):
        p = decode(b"\x00\x05\x00\x03")
        self.assertEqual((p.error_code, p.message), (3, ""))

    def test_block_extremes_decode(self):
        self.assertEqual(decode(b"\x00\x03\x00\x00").block, 0)
        self.assertEqual(decode(b"\x00\x03\xff\xffz").block, 65535)
        self.assertEqual(decode(b"\x00\x04\x00\x00").block, 0)
        self.assertEqual(decode(b"\x00\x04\xff\xff").block, 65535)

    def test_non_utf8_filename_decodes(self):
        p = decode(b"\x00\x01\xff\xfe\x00octet\x00")
        self.assertEqual(p.mode, "octet")
        self.assertIsInstance(p.filename, str)

    def test_bare_nul_oack_raises(self):
        with self.assertRaises(TftpError):
            decode(b"\x00\x06\x00")

    def test_encode_block_range(self):
        self.assertEqual(encode_data(0, b"x"), b"\x00\x03\x00\x00x")
        self.assertEqual(encode_data(65535, b""), b"\x00\x03\xff\xff")
        self.assertEqual(encode_ack(0), b"\x00\x04\x00\x00")
        self.assertEqual(encode_ack(65535), b"\x00\x04\xff\xff")
        for bad in (65536, -1):
            with self.assertRaises(ValueError):
                encode_data(bad, b"x")
            with self.assertRaises(ValueError):
                encode_ack(bad)

    def test_encode_error_code_range(self):
        self.assertEqual(encode_error(65535, "")[:4], b"\x00\x05\xff\xff")
        for bad in (70000, -1):
            with self.assertRaises(ValueError):
                encode_error(bad, "x")

    def test_data_ack_error_oack_decode(self):
        p = decode(b"\x00\x03\x00\x07abc")
        self.assertEqual((p.block, p.payload), (7, b"abc"))
        self.assertEqual(decode(encode_ack(9)).block, 9)
        e = decode(b"\x00\x05\x00\x01gone")
        self.assertEqual((e.error_code, e.message), (1, "gone"))
        o = decode(encode_oack({"Blksize": "512"}))
        self.assertEqual((o.opcode, o.options), (OACK, {"blksize": "512"}))

    def test_mode_lowercased_and_odd_options_rejected(self):
        self.assertEqual(decode(b"\x00\x02f\x00OCTET\x00").mode, "octet")
        with self.assertRaises(TftpError):
            decode(b"\x00\x01f\x00octet\x00blksize\x00")


if __name__ == "__main__":
    unittest.main()
