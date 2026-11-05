import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tftpd_gui.core.safe_open import open_beneath, open_parent, open_root
from tftpd_gui.core.tftp_packet import TftpError


class OpenBeneathTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "root"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret").write_bytes(b"secret")
        (self.base / "secret").write_bytes(b"secret")
        self.root_fd = open_root(self.root)

    def tearDown(self):
        os.close(self.root_fd)
        shutil.rmtree(self.base)

    def read(self, name):
        fd = open_beneath(self.root_fd, name, os.O_RDONLY)
        try:
            return os.read(fd, 1024)
        finally:
            os.close(fd)

    def assertCode(self, code, name, flags=os.O_RDONLY):
        with self.assertRaises(TftpError) as ctx:
            fd = open_beneath(self.root_fd, name, flags)
            os.close(fd)
        self.assertEqual(ctx.exception.code, code, name)

    def test_plain_and_nested_names_open(self):
        (self.root / "file").write_bytes(b"plain")
        (self.root / "a" / "b").mkdir(parents=True)
        (self.root / "a" / "b" / "c").write_bytes(b"nested")
        self.assertEqual(self.read("file"), b"plain")
        self.assertEqual(self.read("a/b/c"), b"nested")
        parent_fd, leaf = open_parent(self.root_fd, "a/b/c")
        try:
            self.assertEqual(leaf, "c")
            self.assertTrue(os.path.samestat(os.fstat(parent_fd), os.stat(self.root / "a" / "b")))
        finally:
            os.close(parent_fd)

    def test_parent_traversal_is_code_2(self):
        (self.root / "a").mkdir()
        for name in ("../secret", "a/../../secret", "a/..", "..", "a/./x", "./x"):
            self.assertCode(2, name)

    def test_absolute_empty_and_nul_names_are_code_2(self):
        for name in ("", str(self.base / "secret"), "a\x00b", "a//b", "a/"):
            self.assertCode(2, name)

    def test_symlink_to_a_file_outside_the_root_is_code_2(self):
        os.symlink(self.outside / "secret", self.root / "link")
        self.assertCode(2, "link")

    def test_symlink_to_a_file_inside_the_root_is_also_code_2(self):
        (self.root / "real").write_bytes(b"x")
        os.symlink("real", self.root / "link")
        self.assertCode(2, "link")

    def test_symlinked_directory_component_is_code_2(self):
        (self.root / "realdir").mkdir()
        (self.root / "realdir" / "x").write_bytes(b"x")
        os.symlink("realdir", self.root / "dirlink")
        os.symlink(self.outside, self.root / "outlink")
        self.assertCode(2, "dirlink/x")
        self.assertCode(2, "outlink/secret")

    def test_directory_swapped_for_a_symlink_after_the_root_was_opened_is_code_2(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "x").write_bytes(b"inside")
        (self.outside / "x").write_bytes(b"outside")
        self.assertEqual(self.read("sub/x"), b"inside")
        shutil.rmtree(self.root / "sub")
        os.symlink(self.outside, self.root / "sub")
        self.assertCode(2, "sub/x")

    def test_missing_file_is_code_1(self):
        (self.root / "a").mkdir()
        self.assertCode(1, "missing")
        self.assertCode(1, "a/missing")
        self.assertCode(1, "nodir/missing")

    def test_leading_slash_is_refused_and_not_stripped(self):
        (self.root / "file").write_bytes(b"x")
        self.assertCode(2, "/file")

    def test_backslash_is_an_ordinary_filename_character(self):
        self.assertCode(1, "..\\..\\x")
        (self.root / "a\\b").write_bytes(b"backslash")
        self.assertEqual(self.read("a\\b"), b"backslash")

    def test_exclusive_create_of_an_existing_file_is_code_6(self):
        (self.root / "file").write_bytes(b"x")
        self.assertCode(6, "file", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        self.assertEqual((self.root / "file").read_bytes(), b"x")


if __name__ == "__main__":
    unittest.main()
