import importlib.util
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_build():
    spec = importlib.util.spec_from_file_location("build_deb", ROOT / "build-deb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackagingTest(unittest.TestCase):
    def test_every_python_module_is_listed_in_the_build_script(self):
        listed = set(_load_build().MODULES)
        found = {str(p.relative_to(ROOT / "tftpd_gui")) for p in (ROOT / "tftpd_gui").rglob("*.py")}
        self.assertEqual(listed, found)

    def test_control_version_placeholder_and_package_version(self):
        control = (ROOT / "packaging/control").read_text()
        self.assertIn("Version: @VERSION@", control)
        self.assertRegex((ROOT / "tftpd_gui/__init__.py").read_text(), r'__version__ = "\d+\.\d+\.\d+"')

    def test_control_depends_on_python3_gi_gtk3_and_pkexec(self):
        depends = re.search(r"^Depends: (.*)$", (ROOT / "packaging/control").read_text(), re.M).group(1)
        for needed in ("python3", "python3-gi", "gir1.2-gtk-3.0", "pkexec"):
            self.assertIn(needed, depends)

    def test_policy_exec_path_matches_the_installed_helper(self):
        policy = (ROOT / "packaging/com.syslinuxos.tftpd-gui.serve.policy").read_text()
        self.assertIn(">/usr/lib/tftpd-gui/tftpd-gui-helper<", policy)
        self.assertIn("<allow_active>auth_admin</allow_active>", policy)
        self.assertIn('id="com.syslinuxos.tftpd-gui.serve"', policy)
        from tftpd_gui.gui.helper_client import DEFAULT_COMMAND
        self.assertEqual(DEFAULT_COMMAND, ["pkexec", "/usr/lib/tftpd-gui/tftpd-gui-helper"])

    def test_desktop_entry_points_at_the_installed_launcher(self):
        desktop = (ROOT / "packaging/tftpd-gui.desktop").read_text()
        self.assertIn("Exec=tftpd-gui\n", desktop)
        self.assertIn("Icon=tftpd-gui\n", desktop)
        self.assertIn("<Filename>tftpd-gui.desktop</Filename>", (ROOT / "packaging/tftpd-gui.menu").read_text())

    def test_helper_entry_point_runs_isolated(self):
        first = (ROOT / "packaging/tftpd-gui-helper").read_text().splitlines()[0]
        self.assertEqual(first, "#!/usr/bin/python3 -I")

    def test_launchers_compile(self):
        for name in ("tftpd-gui", "tftpd-gui-helper"):
            compile((ROOT / "packaging" / name).read_text(), name, "exec")

    def test_user_documents_contain_no_internal_notes(self):
        forbidden = re.compile(r"claude|codex|agent|nextcloud|handoff|\bNUC\b|unit ?test|\d+ tests?\b|"
                               r"~/work|\.cache", re.I)
        for name in ("README.md", "CHANGELOG.md"):
            self.assertIsNone(forbidden.search((ROOT / name).read_text()), name)

    def test_tracked_files_do_not_include_internal_documents(self):
        listing = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
        if listing.returncode != 0:
            self.skipTest("not a git checkout")
        banned = re.compile(r"(^|/)(AGENTS|CLAUDE|HANDOFF|MEMORY|SPEC)\.md$|^\.(claude|codex|agents|remember)/")
        self.assertEqual([f for f in listing.stdout.split() if banned.search(f)], [])


if __name__ == "__main__":
    unittest.main()
