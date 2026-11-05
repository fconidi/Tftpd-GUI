# Tftpd-GUI

![Tftpd-GUI icon](data/tftpd-gui.svg)

**Tftpd-GUI** is the Linux alternative to Tftpd64: a TFTP server and client, a staging DHCP server and a syslog collector in one GTK window. It is made for technicians who configure switches, routers and access points from a laptop: copy a configuration or firmware to or from a device, hand an address to a device fresh out of the box, and watch its syslog messages arrive.

The interface is available in English, Italian, French, German and Spanish.

## The three services

- **TFTP.** Shares one folder over UDP port 69. Clients can read files; writing is off until you enable it, and an upload never overwrites an existing file. Access to the folder never follows symbolic links, and `..` or absolute names are refused. Choose the interface to listen on and the maximum file size. The window shows the largest file each block size can carry (a TFTP transfer has at most 65535 blocks); block sizes above 1428 bytes fragment on ordinary Ethernet. A client section downloads or uploads a file to another TFTP server, with your own permissions.
- **DHCP.** A temporary server for bench work. It is off by default, runs on **one interface you choose**, and takes the address and mask from that interface. Before it starts you confirm the interface and the address range, and the program looks for other DHCP servers on the segment. If one answers you must confirm a second time. If the check cannot run, you must choose to start anyway. When no server answers, the program tells you that silence does not prove the network has none: never start it on a production network. A red banner with a countdown stays on screen while it runs, and it stops by itself after the time you set (30 minutes by default). It does not answer requests meant for another server, and it never refuses an address that may belong to another server.
- **Syslog.** Listens on UDP port 514, optionally TCP as well, and lists messages as they arrive. Filter by text, limit by severity and save the visible lines to a file.

Ports below 1024 need administrator rights. Tftpd-GUI asks for them through polkit when it starts and runs the services in a small helper process. The helper exits as soon as the window closes, so no service stays active. The helper never writes files for the window, and each TFTP transfer uses the permissions of the user who authorized it.

## Install

Tftpd-GUI requires Debian 12 or newer, or a compatible SysLinuxOS release, with GTK 3, Python 3.11 or newer and polkit.

### From the SysLinuxOS APT repository

Add the SysLinuxOS-Tools repository, then:

```bash
sudo apt install tftpd-gui
```

### From a .deb file

```bash
sudo apt install ./tftpd-gui_0.1.0_all.deb
```

Start **Tftpd-GUI** from the SysLinuxOS Tools menu or with `tftpd-gui`.

## Limits

- IPv4 only; no IPv6 or DHCPv6.
- No DNS, FTP or HTTP server, no DHCP relay and no full PXE menu.
- A TFTP transfer carries at most `65535 × block size − 1` bytes, further limited by the maximum size you set (256 MiB by default).

## Italiano

**Tftpd-GUI** è l'alternativa Linux a Tftpd64: server e client TFTP, server DHCP temporaneo e raccolta syslog in una sola finestra, pensata per chi configura switch, router e access point da un portatile.

- **TFTP**: condivide una cartella sulla porta UDP 69. La scrittura è disattivata finché non la abiliti e non sovrascrive file esistenti. I collegamenti simbolici non vengono mai seguiti. Puoi scegliere l'interfaccia e la dimensione massima del file; la finestra mostra il file più grande per ogni dimensione di blocco.
- **DHCP**: spento per difetto, funziona su una sola interfaccia scelta da te. Prima dell'avvio confermi interfaccia e intervallo di indirizzi e il programma cerca altri server DHCP sulla rete; se non ne trova, il testo avvisa che il silenzio non prova che non ce ne siano. Un banner rosso con il conto alla rovescia resta visibile e il server si ferma da solo. Non usarlo su una rete di produzione.
- **Syslog**: UDP 514 e, a scelta, TCP; filtro per testo e gravità, salvataggio su file.

Le porte basse richiedono i privilegi di amministratore: il programma li chiede con polkit e li usa solo in un piccolo processo di servizio che termina alla chiusura della finestra.

Installazione: `sudo apt install tftpd-gui` dal repository SysLinuxOS-Tools, oppure `sudo apt install ./tftpd-gui_0.1.0_all.deb`.

## Licence

GNU General Public License, version 3. See `LICENSE`.
