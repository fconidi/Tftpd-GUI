import io
import unittest

from tftpd_gui.core.protocol import MAX_LINE, encode_message, iter_messages


class ProtocolTest(unittest.TestCase):
    def test_messages_are_newline_delimited_json(self):
        raw = encode_message({"id": 1, "cmd": "status", "text": "è"})
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        stream = io.BytesIO(raw + encode_message({"id": 2}))
        self.assertEqual(list(iter_messages(stream)), [{"id": 1, "cmd": "status", "text": "è"}, {"id": 2}])

    def test_oversized_line_yields_an_error_and_iteration_continues(self):
        stream = io.BytesIO(b'{"a":"' + b"x" * (MAX_LINE + 10) + b'"}\n' + encode_message({"ok": 1}))
        self.assertEqual(list(iter_messages(stream)), [{"error": "line_too_long"}, {"ok": 1}])

    def test_bad_json_and_non_objects_yield_errors(self):
        stream = io.BytesIO(b"not json\n[1,2]\n\xff\xfe\n\n" + encode_message({"ok": 1}))
        self.assertEqual(list(iter_messages(stream)),
                         [{"error": "bad_json"}] * 3 + [{"ok": 1}])

    def test_last_line_without_newline_is_read(self):
        self.assertEqual(list(iter_messages(io.BytesIO(b'{"a":1}'))), [{"a": 1}])

    def test_non_serialisable_values_are_stringified(self):
        import pathlib
        raw = encode_message({"p": pathlib.PurePosixPath("/x")})
        self.assertEqual(raw, b'{"p":"/x"}' + b"\n")


if __name__ == "__main__":
    unittest.main()
