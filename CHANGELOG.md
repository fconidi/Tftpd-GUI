# Changelog

## 0.1.0

- First version.
- TFTP server with read and optional write, symlink-safe access to the shared folder, per-interface binding and a configurable maximum file size.
- TFTP client to download and upload files from the same window.
- Staging DHCP server limited to one chosen interface, with a check for other DHCP servers before it starts, an on-screen countdown and automatic stop.
- Syslog collector (UDP and optional TCP) with filter, severity limit and save to file.
- Network services run in a small helper authorized through polkit; it exits when the window closes.
- Interface in English, Italian, French, German and Spanish.
- Menu entry in SysLinuxOS Tools, with its own icon.
