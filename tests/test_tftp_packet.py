import unittest

from netserve.core.tftp_packet import (
    RRQ,
    TftpError,
    decode,
    encode_ack,
    encode_data,
    encode_error,
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

    def test_decode_never_raises_other_exceptions(self):
        for data in (b"", b"\x00\x03", b"\x00\x04\x01", b"\x00\x05\x00", b"\x00\x06a\x00",
                     b"\x00\x01a\x00octet\x00blksize\x00", b"\x00\x01\xff\xfe\x00octet\x00"):
            try:
                decode(data)
            except TftpError:
                pass

    def test_data_ack_error_oack_decode(self):
        p = decode(b"\x00\x03\x00\x07abc")
        self.assertEqual((p.block, p.payload), (7, b"abc"))
        self.assertEqual(decode(encode_ack(9)).block, 9)
        e = decode(b"\x00\x05\x00\x01gone")
        self.assertEqual((e.error_code, e.message), (1, "gone"))
        from netserve.core.tftp_packet import encode_oack, OACK
        o = decode(encode_oack({"Blksize": "512"}))
        self.assertEqual((o.opcode, o.options), (OACK, {"blksize": "512"}))

    def test_mode_lowercased_and_odd_options_rejected(self):
        self.assertEqual(decode(b"\x00\x02f\x00OCTET\x00").mode, "octet")
        with self.assertRaises(TftpError):
            decode(b"\x00\x01f\x00octet\x00blksize\x00")


if __name__ == "__main__":
    unittest.main()
