#!/usr/bin/python3
"""Build the tftpd-gui Debian package from an explicit file list."""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

MODULES = (
    "__init__.py", "__main__.py", "i18n.py", "i18n_gui.py",
    "core/__init__.py", "core/dhcp_packet.py", "core/dhcp_pool.py", "core/dhcp_probe.py",
    "core/dhcp_server.py", "core/errors.py", "core/ifaces.py", "core/protocol.py",
    "core/safe_open.py", "core/syslog_server.py", "core/tftp_client.py",
    "core/tftp_packet.py", "core/tftp_server.py",
    "gui/__init__.py", "gui/common.py", "gui/dhcp_tab.py", "gui/helper_client.py",
    "gui/syslog_tab.py", "gui/tftp_tab.py", "gui/window.py",
    "helper/__init__.py", "helper/__main__.py", "helper/daemon.py",
)
LIB = "usr/lib/tftpd-gui"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build Tftpd-GUI for Debian")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent
    package = source / "tftpd_gui"
    match = re.search(r'__version__ = "([^"]+)"', (package / "__init__.py").read_text(encoding="utf-8"))
    if match is None:
        raise ValueError("package version is missing")
    version = match.group(1)
    with tempfile.TemporaryDirectory(prefix="package-", dir=output) as tmp:
        root = Path(tmp)

        def install(origin: Path, relative: str, executable: bool = False):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
            target.chmod(0o755 if executable else 0o644)

        for name in MODULES:
            install(package / name, f"{LIB}/tftpd_gui/{name}")
        install(source / "packaging/tftpd-gui", "usr/bin/tftpd-gui", True)
        install(source / "packaging/tftpd-gui-helper", f"{LIB}/tftpd-gui-helper", True)
        install(source / "packaging/com.syslinuxos.tftpd-gui.serve.policy",
                "usr/share/polkit-1/actions/com.syslinuxos.tftpd-gui.serve.policy")
        install(source / "packaging/tftpd-gui.desktop", "usr/share/applications/tftpd-gui.desktop")
        install(source / "packaging/tftpd-gui.menu", "etc/xdg/menus/applications-merged/tftpd-gui.menu")
        install(source / "data/tftpd-gui.svg", "usr/share/icons/hicolor/scalable/apps/tftpd-gui.svg")
        for name in ("README.md", "CHANGELOG.md", "LICENSE"):
            install(source / name, f"usr/share/doc/tftpd-gui/{name}")
        control = root / "DEBIAN"
        control.mkdir()
        text = (source / "packaging/control").read_text(encoding="utf-8").replace("@VERSION@", version)
        (control / "control").write_text(text, encoding="utf-8")
        root.chmod(0o755)
        for directory in root.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o755)
        artifact = output / f"tftpd-gui_{version}_all.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(root), str(artifact)], check=True)
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
