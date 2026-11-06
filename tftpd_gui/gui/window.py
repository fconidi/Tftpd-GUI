"""Main window: three tabs, a notice bar and a status line."""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..core import tftp_client  # noqa: E402
from ..i18n import translate as _  # noqa: E402
from .common import thread_runner  # noqa: E402
from .dhcp_tab import DhcpTab  # noqa: E402
from . import style  # noqa: E402
from .syslog_tab import SyslogTab  # noqa: E402
from .tftp_tab import TftpTab  # noqa: E402

_MESSAGE_TYPES = {"info": Gtk.MessageType.INFO, "warning": Gtk.MessageType.WARNING,
                  "error": Gtk.MessageType.ERROR}
_TICK_MS = 500
_LEASE_EVERY = 4  # ticks


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, helper, *, clock=time.monotonic, tftp_get=tftp_client.get,
                 tftp_put=tftp_client.put, run_async=None, ask=None, timers=True) -> None:
        super().__init__(title=_("app_title"), default_width=760, default_height=640)
        self.helper = helper
        self.clock = clock
        self.tftp_get = tftp_get
        self.tftp_put = tftp_put
        self.run_async = run_async or thread_runner
        if ask is not None:
            self.ask = ask
        self.notices: list[tuple[str, str]] = []
        self._helper_closed = False
        self._ticks = 0

        style.apply()
        self.set_icon_name("tftpd-gui")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)
        self.infobar = Gtk.InfoBar(no_show_all=True, show_close_button=True)
        self.notice_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.infobar.get_content_area().add(self.notice_label)
        self.infobar.connect("response", lambda bar, _r: bar.hide())
        box.pack_start(self.infobar, False, False, 0)

        self.notebook = Gtk.Notebook()
        self.tftp_tab = TftpTab(self)
        self.dhcp_tab = DhcpTab(self)
        self.syslog_tab = SyslogTab(self)
        for tab, key in ((self.tftp_tab, "tab_tftp"), (self.dhcp_tab, "tab_dhcp"), (self.syslog_tab, "tab_syslog")):
            self.notebook.append_page(tab.widget, Gtk.Label(label=_(key)))
        box.pack_start(self.notebook, True, True, 0)
        bar = Gtk.Box(valign=Gtk.Align.CENTER)
        style.add_class(bar, "status-bar")
        self.status_dot = Gtk.Box(valign=Gtk.Align.CENTER)
        style.add_class(self.status_dot, "status-dot")
        self.status = Gtk.Label(xalign=0)
        bar.pack_start(self.status_dot, False, False, 0)
        bar.pack_start(self.status, True, True, 0)
        box.pack_start(bar, False, False, 0)

        self.connect("destroy", lambda *_a: self.close_helper())
        self.update_status()
        if timers:
            GLib.timeout_add(_TICK_MS, self._on_timer)

    # -- services for the tabs ------------------------------------------

    def ask(self, title: str, body: str) -> bool:
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.OK_CANCEL, text=title)
        dialog.format_secondary_text(body)
        answer = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        return answer

    def notice(self, text: str, kind: str = "info") -> None:
        self.notices.append((kind, text))
        self.infobar.set_message_type(_MESSAGE_TYPES.get(kind, Gtk.MessageType.INFO))
        self.notice_label.set_text(text)
        self.infobar.show()
        self.notice_label.show()

    def set_client_busy(self, busy: bool) -> None:
        self.tftp_tab.get_button.set_sensitive(not busy)
        self.tftp_tab.put_button.set_sensitive(not busy)

    def update_status(self) -> None:
        names = []
        if self.tftp_tab.running:
            names.append(f"TFTP {self.tftp_tab.port}")
        if self.dhcp_tab.running:
            names.append(f"DHCP {self.dhcp_tab.interface_name}")
        if self.syslog_tab.running:
            names.append("Syslog 514")
        self.status.set_text(_("status_listening", services=", ".join(names)) if names else _("status_idle"))
        dot = self.status_dot.get_style_context()
        if names:
            dot.add_class("on")
        else:
            dot.remove_class("on")

    # -- periodic work ---------------------------------------------------

    def _on_timer(self) -> bool:
        self.tick()
        return not self._helper_closed

    def tick(self) -> None:
        for event in self.helper.poll_events():
            data = event.get("data") or {}
            kind = event.get("event")
            if kind == "tftp":
                self.tftp_tab.on_event(data)
            elif kind == "dhcp":
                self.dhcp_tab.on_event(data)
            elif kind == "syslog":
                self.syslog_tab.on_event(data)
            elif kind == "dropped":
                self.notice(_("error_generic", code=f"dropped {data.get('count', '?')}"), "warning")
            elif kind == "helper_exited" and not self._helper_closed:
                self.tftp_tab._set_running(False)
                self.dhcp_tab._set_running(False)
                self.syslog_tab._set_running(False)
                self.notice(_("helper_exited"), "error")
                for tab in (self.tftp_tab, self.dhcp_tab, self.syslog_tab):
                    tab.start_button.set_sensitive(False)
                self.close_helper()
        self.dhcp_tab.update_banner()
        self._ticks += 1
        if self.dhcp_tab.running and self._ticks % _LEASE_EVERY == 0:
            self.run_async(lambda: self.helper.call("status"), self._show_leases)

    def _show_leases(self, result, error) -> None:
        if error is None:
            self.dhcp_tab.show_leases(result.get("dhcp", {}).get("leases", []))

    def close_helper(self) -> None:
        if not self._helper_closed:
            self._helper_closed = True
            self.helper.close()
