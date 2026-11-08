# Changelog

## 0.1.2

- The TFTP and Syslog tabs now list the network interfaces, loopback included, so a server can be limited to one of them or to this computer only. Syslog used to listen on every interface with no way to choose.
- Ask for confirmation before sharing the home folder, one of its parent folders or a folder that holds `.ssh` or `.gnupg`.
- Show the server's own message in a failed TFTP transfer, for example "file not found (1)", instead of the bare error code.

## 0.1.1

- Reject DHCP address pools that contain the gateway and avoid consuming a different lease when a requested address is unavailable.
- Close the DHCP socket when automatic stop expires and update the window if the service helper exits.
- Keep TFTP downloads private to the local user and acknowledge a retransmitted final upload block.

## 0.1.0

- First version.
- TFTP server with read and optional write, symlink-safe access to the shared folder, per-interface binding and a configurable maximum file size.
- TFTP client to download and upload files from the same window.
- Staging DHCP server limited to one chosen interface, with a check for other DHCP servers before it starts, an on-screen countdown and automatic stop.
- Syslog collector (UDP and optional TCP) with filter, severity limit and save to file.
- Network services run in a small helper authorized through polkit; it exits when the window closes.
- Interface in English, Italian, French, German and Spanish.
- Menu entry in SysLinuxOS Tools, with its own icon.
