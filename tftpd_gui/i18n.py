"""Translations for every string shown to the user."""
import os

DEFAULT_LANGUAGE = "en"

CATALOG = {
    "en": {
        "app_title": "Tftpd-GUI",
        "tab_tftp": "TFTP",
        "tab_dhcp": "DHCP",
        "tab_syslog": "Syslog",
        "start": "Start",
        "stop": "Stop",
        "port_busy": "Port {port} is already in use by another program.",
        "access_denied": "Administrator authorization was denied.",
        "dhcp_confirm_title": "Start the DHCP server?",
        "dhcp_confirm_body": "The DHCP server will answer requests on {interface} and hand out addresses from {pool}. Make sure no other DHCP server is active on this network.",
        "dhcp_foreign_server": "Another DHCP server is already active on this network.",
        "dhcp_running_banner": "DHCP server running",
        "dhcp_autostop": "The DHCP server was stopped automatically.",
        "tftp_root": "Shared folder",
        "tftp_allow_write": "Allow clients to upload files",
        "syslog_save": "Save log to file",
    },
    "it": {
        "app_title": "Tftpd-GUI",
        "tab_tftp": "TFTP",
        "tab_dhcp": "DHCP",
        "tab_syslog": "Syslog",
        "start": "Avvia",
        "stop": "Ferma",
        "port_busy": "La porta {port} è già usata da un altro programma.",
        "access_denied": "L'autorizzazione di amministratore è stata negata.",
        "dhcp_confirm_title": "Avviare il server DHCP?",
        "dhcp_confirm_body": "Il server DHCP risponderà alle richieste su {interface} e assegnerà indirizzi dall'intervallo {pool}. Verifica che nessun altro server DHCP sia attivo su questa rete.",
        "dhcp_foreign_server": "Un altro server DHCP è già attivo su questa rete.",
        "dhcp_running_banner": "Server DHCP in funzione",
        "dhcp_autostop": "Il server DHCP è stato fermato automaticamente.",
        "tftp_root": "Cartella condivisa",
        "tftp_allow_write": "Consenti ai client di caricare file",
        "syslog_save": "Salva il registro su file",
    },
    "fr": {
        "app_title": "Tftpd-GUI",
        "tab_tftp": "TFTP",
        "tab_dhcp": "DHCP",
        "tab_syslog": "Syslog",
        "start": "Démarrer",
        "stop": "Arrêter",
        "port_busy": "Le port {port} est déjà utilisé par un autre programme.",
        "access_denied": "L'autorisation administrateur a été refusée.",
        "dhcp_confirm_title": "Démarrer le serveur DHCP ?",
        "dhcp_confirm_body": "Le serveur DHCP répondra aux requêtes sur {interface} et attribuera des adresses de la plage {pool}. Vérifiez qu'aucun autre serveur DHCP n'est actif sur ce réseau.",
        "dhcp_foreign_server": "Un autre serveur DHCP est déjà actif sur ce réseau.",
        "dhcp_running_banner": "Serveur DHCP en marche",
        "dhcp_autostop": "Le serveur DHCP a été arrêté automatiquement.",
        "tftp_root": "Dossier partagé",
        "tftp_allow_write": "Autoriser les clients à envoyer des fichiers",
        "syslog_save": "Enregistrer le journal dans un fichier",
    },
    "de": {
        "app_title": "Tftpd-GUI",
        "tab_tftp": "TFTP",
        "tab_dhcp": "DHCP",
        "tab_syslog": "Syslog",
        "start": "Starten",
        "stop": "Beenden",
        "port_busy": "Port {port} wird bereits von einem anderen Programm verwendet.",
        "access_denied": "Die Administratorberechtigung wurde verweigert.",
        "dhcp_confirm_title": "DHCP-Server starten?",
        "dhcp_confirm_body": "Der DHCP-Server beantwortet Anfragen auf {interface} und vergibt Adressen aus dem Bereich {pool}. Stellen Sie sicher, dass in diesem Netzwerk kein anderer DHCP-Server aktiv ist.",
        "dhcp_foreign_server": "In diesem Netzwerk ist bereits ein anderer DHCP-Server aktiv.",
        "dhcp_running_banner": "DHCP-Server läuft",
        "dhcp_autostop": "Der DHCP-Server wurde automatisch beendet.",
        "tftp_root": "Freigegebener Ordner",
        "tftp_allow_write": "Hochladen von Dateien durch Clients erlauben",
        "syslog_save": "Protokoll in Datei speichern",
    },
    "es": {
        "app_title": "Tftpd-GUI",
        "tab_tftp": "TFTP",
        "tab_dhcp": "DHCP",
        "tab_syslog": "Syslog",
        "start": "Iniciar",
        "stop": "Detener",
        "port_busy": "El puerto {port} ya está siendo usado por otro programa.",
        "access_denied": "Se denegó la autorización de administrador.",
        "dhcp_confirm_title": "¿Iniciar el servidor DHCP?",
        "dhcp_confirm_body": "El servidor DHCP responderá a las solicitudes en {interface} y asignará direcciones del rango {pool}. Comprueba que no haya otro servidor DHCP activo en esta red.",
        "dhcp_foreign_server": "Ya hay otro servidor DHCP activo en esta red.",
        "dhcp_running_banner": "Servidor DHCP en ejecución",
        "dhcp_autostop": "El servidor DHCP se detuvo automáticamente.",
        "tftp_root": "Carpeta compartida",
        "tftp_allow_write": "Permitir que los clientes suban archivos",
        "syslog_save": "Guardar el registro en un archivo",
    },
}


from .i18n_gui import EXTRA as _EXTRA  # noqa: E402

for _lang, _strings in _EXTRA.items():
    CATALOG[_lang].update(_strings)


def current_language() -> str:
    """Return the language code from the environment, or the default."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        for part in value.split(":"):
            code = part.split(".")[0].split("_")[0].split("@")[0].lower()
            if code in CATALOG:
                return code
            if code and code not in ("c", "posix"):
                break
    return DEFAULT_LANGUAGE


def translate(key: str, lang: str | None = None, **fields) -> str:
    """Return the text for key in lang, falling back to English, then to the key."""
    if lang is None:
        lang = current_language()
    text = CATALOG.get(lang, CATALOG[DEFAULT_LANGUAGE]).get(key)
    if text is None:
        text = CATALOG[DEFAULT_LANGUAGE].get(key, key)
    return text.format(**fields) if fields else text
