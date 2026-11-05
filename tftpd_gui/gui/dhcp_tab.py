"""DHCP tab: the one service that can disturb a network, so it asks first."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from ..core.ifaces import list_interfaces  # noqa: E402
from ..i18n import translate as _  # noqa: E402
from . import style  # noqa: E402
from .common import error_text  # noqa: E402


def format_remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class DhcpTab:
    def __init__(self, window) -> None:
        self.window = window
        self.running = False
        self.interface_name: str | None = None
        self.started_at = 0.0
        self.auto_stop_seconds = 0
        self.server_ip = self.netmask = ""

        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=20)
        self.banner = Gtk.Label(xalign=0, wrap=True, no_show_all=True)
        style.add_class(self.banner, "dhcp-banner")
        grid.attach(self.banner, 0, 0, 2, 1)

        def add(row, label, widget):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
            return widget

        self.iface_combo = add(1, _("interface"), Gtk.ComboBoxText())
        self.pool_start = add(2, _("dhcp_pool_start"), Gtk.Entry())
        self.pool_end = add(3, _("dhcp_pool_end"), Gtk.Entry())
        self.router = add(4, _("dhcp_router"), Gtk.Entry())
        self.dns = add(5, _("dhcp_dns"), Gtk.Entry())
        self.next_server = add(6, _("dhcp_next_server"), Gtk.Entry())
        self.bootfile = add(7, _("dhcp_bootfile"), Gtk.Entry())
        self.lease_spin = add(8, _("dhcp_lease"), Gtk.SpinButton.new_with_range(60, 86400, 60))
        self.lease_spin.set_value(600)
        self.autostop_spin = add(9, _("dhcp_autostop_minutes"), Gtk.SpinButton.new_with_range(0, 1440, 5))
        self.autostop_spin.set_value(30)
        self.address_label = Gtk.Label(xalign=0, wrap=True)
        style.add_class(self.address_label, "hint")
        grid.attach(self.address_label, 0, 10, 2, 1)

        buttons = Gtk.Box(spacing=8)
        self.start_button = style.add_class(Gtk.Button(label=_("start")), "suggested-action")
        self.stop_button = style.add_class(Gtk.Button(label=_("stop"), sensitive=False), "destructive-action")
        self.start_button.connect("clicked", lambda *_a: self.request_start())
        self.stop_button.connect("clicked", lambda *_a: self.stop())
        buttons.pack_start(self.start_button, False, False, 0)
        buttons.pack_start(self.stop_button, False, False, 0)
        grid.attach(buttons, 0, 11, 2, 1)

        grid.attach(style.add_class(Gtk.Label(label=_("dhcp_leases"), xalign=0), "section-title"), 0, 12, 2, 1)
        self.leases = Gtk.ListStore(str, str, str)
        view = Gtk.TreeView(model=self.leases, hexpand=True, vexpand=True)
        for index, title in enumerate(("IP", "MAC", "Host")):
            view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=index))
        view.set_rules_hint(True)
        scroll = style.add_class(Gtk.ScrolledWindow(min_content_height=140), "list")
        scroll.add(view)
        grid.attach(scroll, 0, 13, 2, 1)

        scroller = Gtk.ScrolledWindow()
        scroller.add(grid)
        self.widget = scroller
        self.set_interfaces(list_interfaces())

    # -- inputs ----------------------------------------------------------

    def set_interfaces(self, names: list[str]) -> None:
        self.iface_combo.remove_all()
        for name in names:
            self.iface_combo.append_text(name)
        if names:
            self.iface_combo.set_active(0)

    def interface(self) -> str | None:
        return self.iface_combo.get_active_text()

    def _args(self) -> dict:
        def optional(entry):
            return entry.get_text().strip() or None

        dns = [part.strip() for part in self.dns.get_text().split(",") if part.strip()]
        return dict(
            interface=self.interface(), pool_start=self.pool_start.get_text().strip(),
            pool_end=self.pool_end.get_text().strip(), router=optional(self.router), dns=dns,
            next_server=optional(self.next_server), bootfile=optional(self.bootfile),
            lease_seconds=int(self.lease_spin.get_value()),
            auto_stop_seconds=int(self.autostop_spin.get_value()) * 60,
        )

    # -- start flow: confirm, probe, second confirmation, start ---------

    def request_start(self) -> None:
        if self.running:
            return
        args = self._args()
        if not (args["interface"] and args["pool_start"] and args["pool_end"]):
            self.window.notice(_("bad_arguments", detail=_("dhcp_pool_start")), "error")
            return
        pool = f"{args['pool_start']} - {args['pool_end']}"
        if not self.window.ask(_("dhcp_confirm_title"),
                               _("dhcp_confirm_body", interface=args["interface"], pool=pool)):
            return
        self.window.run_async(
            lambda: self.window.helper.call("probe_dhcp", interface=args["interface"]),
            lambda result, error: self._after_probe(args, result, error))

    def _after_probe(self, args: dict, result, error) -> None:
        if error is not None:
            if not self.window.ask(_("dhcp_confirm_title"),
                                   _("dhcp_probe_error", reason=error_text(error)) + "\n\n" + _("dhcp_start_anyway")):
                return
        elif result["status"] == "found":
            names = ", ".join(f"{s['server_id']} ({s['offered_ip']})" for s in result["servers"])
            if not self.window.ask(_("dhcp_foreign_server"),
                                   _("dhcp_found_list", servers=names) + "\n\n" + _("dhcp_foreign_confirm")):
                return
        elif result["status"] == "error":
            if not self.window.ask(_("dhcp_confirm_title"),
                                   _("dhcp_probe_error", reason=result.get("reason") or "?")
                                   + "\n\n" + _("dhcp_start_anyway")):
                return
        else:
            self.window.notice(_("dhcp_probe_not_found"), "info")
        self._start(args)

    def _start(self, args: dict) -> None:
        self.window.run_async(
            lambda: self.window.helper.call("start_dhcp", **args),
            lambda result, error: self._started(args, result, error))

    def _started(self, args: dict, result, error) -> None:
        if error is not None:
            self.window.notice(error_text(error), "error")
            return
        self.server_ip, self.netmask = result.get("server_ip", ""), result.get("netmask", "")
        self.address_label.set_text(_("dhcp_server_address", ip=self.server_ip, mask=self.netmask))
        self.interface_name = args["interface"]
        self.auto_stop_seconds = args["auto_stop_seconds"]
        self.started_at = self.window.clock()
        self._set_running(True)
        if args["next_server"] and not self.window.tftp_tab.running:
            self.window.notice(_("dhcp_next_server_warning"), "warning")

    def stop(self) -> None:
        self.window.run_async(
            lambda: self.window.helper.call("stop_dhcp"),
            lambda result, error: self.window.notice(error_text(error), "error") if error
            else self._set_running(False))

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.start_button.set_sensitive(not running)
        self.stop_button.set_sensitive(running)
        self.update_banner()
        self.window.update_status()

    def update_banner(self) -> None:
        if not self.running:
            self.banner.hide()
            self.banner.set_text("")
            return
        if self.auto_stop_seconds:
            remaining = self.auto_stop_seconds - (self.window.clock() - self.started_at)
            text = _("dhcp_countdown", interface=self.interface_name, time=format_remaining(remaining))
        else:
            text = _("dhcp_running_no_limit", interface=self.interface_name)
        self.banner.set_text(text)
        self.banner.show()

    def on_event(self, data: dict) -> None:
        if data.get("event") == "stopped" and self.running:
            self._set_running(False)
            self.window.notice(_("dhcp_autostop"), "warning")
            self.window.run_async(lambda: self.window.helper.call("stop_dhcp"), lambda *_a: None)

    def show_leases(self, leases: list[dict]) -> None:
        self.leases.clear()
        for lease in leases:
            self.leases.append([lease["ip"], lease["mac"], lease.get("hostname") or ""])
