import tempfile
import unittest
from pathlib import Path

from tftpd_gui.core.exposure import sensitive_folder_reason


class ExposureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.home = self.base / "home" / "user"
        self.home.mkdir(parents=True)

    def test_the_home_directory_is_flagged(self):
        self.assertEqual(sensitive_folder_reason(self.home, self.home), "home")

    def test_a_trailing_slash_does_not_hide_the_home_directory(self):
        self.assertEqual(sensitive_folder_reason(str(self.home) + "/", self.home), "home")

    def test_a_parent_of_the_home_directory_is_flagged(self):
        self.assertEqual(sensitive_folder_reason(self.home.parent, self.home), "home")
        self.assertEqual(sensitive_folder_reason("/", self.home), "home")

    def test_a_symlink_to_the_home_directory_is_flagged(self):
        link = self.base / "share"
        link.symlink_to(self.home)
        self.assertEqual(sensitive_folder_reason(link, self.home), "home")

    def test_a_folder_holding_ssh_keys_is_flagged(self):
        folder = self.base / "backup"
        (folder / ".ssh").mkdir(parents=True)
        self.assertEqual(sensitive_folder_reason(folder, self.home), "secrets")

    def test_a_folder_holding_gnupg_is_flagged(self):
        folder = self.base / "backup"
        (folder / ".gnupg").mkdir(parents=True)
        self.assertEqual(sensitive_folder_reason(folder, self.home), "secrets")

    def test_an_ordinary_folder_is_not_flagged(self):
        folder = self.base / "tftp"
        folder.mkdir()
        self.assertIsNone(sensitive_folder_reason(folder, self.home))

    def test_a_subfolder_of_home_is_not_flagged(self):
        folder = self.home / "tftp"
        folder.mkdir()
        self.assertIsNone(sensitive_folder_reason(folder, self.home))

    def test_a_missing_folder_is_not_flagged(self):
        self.assertIsNone(sensitive_folder_reason(self.base / "missing", self.home))


if __name__ == "__main__":
    unittest.main()
