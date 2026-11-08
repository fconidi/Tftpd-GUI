"""Syslog tab: live list of received messages."""

from __future__ import annotations

import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from ..core.ifaces import list_interfaces  # noqa: E402
from ..core.syslog_server import save_messages  # noqa: E402
from ..i18n import translate as _  # noqa: E402
from . import style  # noqa: E402
from .common import chosen_interface, error_text, fill_interface_combo  # noqa: E402

SEVERITIES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")
MAX_ROWS = 10000


class SyslogTab:
    def __init__(self, window) -> None:
        self.window = window
        self.running = False
        self.messages: list[dict] = []

        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=20)
        grid.attach(Gtk.Label(label=_("interface"), xalign=0), 0, 0, 1, 1)
        self.iface_combo = Gtk.ComboBoxText()
        grid.attach(self.iface_combo, 1, 0, 3, 1)
        buttons = Gtk.Box(spacing=8)
        self.start_button = style.add_class(Gtk.Button(label=_("start")), "suggested-action")
        self.stop_button = style.add_class(Gtk.Button(label=_("stop"), sensitive=False), "destructive-action")
        self.tcp_check = Gtk.CheckButton(label=_("syslog_tcp"))
        self.save_button = Gtk.Button(label=_("syslog_save"))
        self.start_button.connect("clicked", lambda *_a: self.start())
        self.stop_button.connect("clicked", lambda *_a: self.stop())
        self.save_button.connect("clicked", lambda *_a: self._choose_and_save())
        for widget in (self.start_button, self.stop_button, self.tcp_check, self.save_button):
            buttons.pack_start(widget, False, False, 0)
        grid.attach(buttons, 0, 1, 4, 1)

        grid.attach(Gtk.Label(label=_("syslog_filter"), xalign=0), 0, 2, 1, 1)
        self.filter_entry = Gtk.Entry(hexpand=True)
        self.filter_entry.connect("changed", lambda *_a: self.refilter())
        grid.attach(self.filter_entry, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label=_("syslog_severity"), xalign=0), 2, 2, 1, 1)
        self.severity_combo = Gtk.ComboBoxText()
        self.severity_combo.append_text(_("syslog_any"))
        for name in SEVERITIES:
            self.severity_combo.append_text(name)
        self.severity_combo.set_active(0)
        self.severity_combo.connect("changed", lambda *_a: self.refilter())
        grid.attach(self.severity_combo, 3, 2, 1, 1)

        self.store = Gtk.ListStore(str, str, str, str)
        view = Gtk.TreeView(model=self.store, hexpand=True, vexpand=True)
        for index, key in enumerate(("syslog_col_time", "syslog_col_host", "syslog_col_severity",
                                     "syslog_col_message")):
            view.append_column(Gtk.TreeViewColumn(_(key), Gtk.CellRendererText(), text=index))
        style.add_class(view, "log-view")
        view.set_rules_hint(True)
        scroll = style.add_class(Gtk.ScrolledWindow(min_content_height=260), "list")
        scroll.add(view)
        grid.attach(scroll, 0, 3, 4, 1)
        self.widget = grid
        self.set_interfaces(list_interfaces(include_loopback=True))

    def set_interfaces(self, names: list[str]) -> None:
        fill_interface_combo(self.iface_combo, names)

    def interface(self) -> str | None:
        return chosen_interface(self.iface_combo)

    # -- filter ----------------------------------------------------------

    def min_severity(self) -> int | None:
        index = self.severity_combo.get_active()
        return None if index <= 0 else index - 1

    def _matches(self, message: dict) -> bool:
        needle = self.filter_entry.get_text().strip().lower()
        if needle and needle not in message["raw"].lower():
            return False
        limit = self.min_severity()
        if limit is not None:
            severity = message.get("severity")
            if severity is None or severity > limit:
                return False
        return True

    def visible_messages(self) -> list[dict]:
        return [m for m in self.messages if self._matches(m)]

    @staticmethod
    def _row(message: dict) -> list[str]:
        stamp = time.strftime("%H:%M:%S", time.localtime(message.get("received", 0)))
        severity = message.get("severity")
        name = SEVERITIES[severity] if isinstance(severity, int) and 0 <= severity < 8 else ""
        return [stamp, message.get("host") or message.get("source") or "", name, message.get("message", "")]

    def refilter(self) -> None:
        self.store.clear()
        for message in self.visible_messages():
            self.store.append(self._row(message))

    # -- service ---------------------------------------------------------

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.start_button.set_sensitive(not running)
        self.stop_button.set_sensitive(running)
        self.window.update_status()

    def start(self) -> None:
        tcp, interface = self.tcp_check.get_active(), self.interface()
        self.window.run_async(
            lambda: self.window.helper.call("start_syslog", port=514, tcp=tcp, interface=interface),
            lambda result, error: self.window.notice(error_text(error), "error") if error
            else self._set_running(True))

    def stop(self) -> None:
        self.window.run_async(
            lambda: self.window.helper.call("stop_syslog"),
            lambda result, error: self.window.notice(error_text(error), "error") if error
            else self._set_running(False))

    def on_event(self, message: dict) -> None:
        self.messages.append(message)
        if len(self.messages) > MAX_ROWS:
            del self.messages[: len(self.messages) - MAX_ROWS]
            self.refilter()
        elif self._matches(message):
            self.store.append(self._row(message))

    # -- saving: done here, as the user, never by the helper -------------

    def save(self, path) -> int:
        count = save_messages(self.visible_messages(), Path(path))
        self.window.notice(_("syslog_saved", count=count), "info")
        return count

    def _choose_and_save(self) -> None:
        dialog = Gtk.FileChooserDialog(title=_("syslog_save"), transient_for=self.window,
                                       action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        path = dialog.get_filename() if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        if path:
            try:
                self.save(path)
            except OSError as exc:
                self.window.notice(_("error_generic", code=exc.strerror or type(exc).__name__), "error")
