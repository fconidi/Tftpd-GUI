"""TFTP tab: server controls, transfer list and the TFTP client."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from ..core.exposure import sensitive_folder_reason  # noqa: E402
from ..core.ifaces import list_interfaces  # noqa: E402
from ..core.tftp_server import max_transfer_bytes  # noqa: E402
from ..i18n import translate as _  # noqa: E402
from . import style  # noqa: E402
from .common import chosen_interface, error_text, fill_interface_combo  # noqa: E402

MIB = 1024 ** 2
_BLOCKS = (512, 1428, 8192)


def format_size(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GiB"
    return f"{size / MIB:.2f} MiB"


def _grid() -> Gtk.Grid:
    grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=20)
    return grid


class TftpTab:
    def __init__(self, window) -> None:
        self.window = window
        self.running = False
        self.port: int | None = None
        grid = _grid()
        row = 0

        grid.attach(Gtk.Label(label=_("tftp_root"), xalign=0), 0, row, 1, 1)
        self.root_entry = Gtk.Entry(hexpand=True)
        grid.attach(self.root_entry, 1, row, 1, 1)
        browse = Gtk.Button(label="…")
        browse.connect("clicked", lambda *_a: self._browse(self.root_entry, folder=True))
        grid.attach(browse, 2, row, 1, 1)
        row += 1

        grid.attach(Gtk.Label(label=_("interface"), xalign=0), 0, row, 1, 1)
        self.iface_combo = Gtk.ComboBoxText()
        self.iface_combo.append_text(_("all_interfaces"))
        self.iface_combo.set_active(0)
        grid.attach(self.iface_combo, 1, row, 2, 1)
        row += 1

        grid.attach(Gtk.Label(label=_("tftp_allow_write"), xalign=0), 0, row, 1, 1)
        self.write_switch = Gtk.Switch(halign=Gtk.Align.START)
        grid.attach(self.write_switch, 1, row, 2, 1)
        row += 1

        grid.attach(Gtk.Label(label=_("tftp_max_size"), xalign=0), 0, row, 1, 1)
        self.max_spin = Gtk.SpinButton.new_with_range(1, 4096, 1)
        self.max_spin.set_value(256)
        self.max_spin.connect("value-changed", lambda *_a: self.update_limits())
        grid.attach(self.max_spin, 1, row, 2, 1)
        row += 1

        self.limit_label = style.add_class(Gtk.Label(xalign=0, wrap=True), "hint")
        grid.attach(self.limit_label, 0, row, 3, 1)
        row += 1
        grid.attach(style.add_class(Gtk.Label(label=_("tftp_block_note"), xalign=0, wrap=True), "hint"), 0, row, 3, 1)
        row += 1

        buttons = Gtk.Box(spacing=8)
        self.start_button = style.add_class(Gtk.Button(label=_("start")), "suggested-action")
        self.stop_button = style.add_class(Gtk.Button(label=_("stop"), sensitive=False), "destructive-action")
        self.start_button.connect("clicked", lambda *_a: self.start())
        self.stop_button.connect("clicked", lambda *_a: self.stop())
        buttons.pack_start(self.start_button, False, False, 0)
        buttons.pack_start(self.stop_button, False, False, 0)
        grid.attach(buttons, 0, row, 3, 1)
        row += 1

        grid.attach(style.add_class(Gtk.Label(label=_("tftp_transfers"), xalign=0), "section-title"), 0, row, 3, 1)
        row += 1
        self.transfers = Gtk.ListStore(str, str, str, str, str)
        view = Gtk.TreeView(model=self.transfers, hexpand=True, vexpand=True)
        for index, title in enumerate(("Peer", "File", "Dir", "Bytes", "Result")):
            view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=index))
        view.set_rules_hint(True)
        scroll = style.add_class(Gtk.ScrolledWindow(min_content_height=130), "list")
        scroll.add(view)
        grid.attach(scroll, 0, row, 3, 1)
        row += 1

        grid.attach(style.add_class(Gtk.Label(label=_("tftp_client"), xalign=0), "section-title"), 0, row, 3, 1)
        row += 1
        self.host_entry = Gtk.Entry(placeholder_text=_("client_host"))
        self.remote_entry = Gtk.Entry(placeholder_text=_("client_remote"))
        self.local_entry = Gtk.Entry(placeholder_text=_("client_local"), hexpand=True)
        grid.attach(self.host_entry, 0, row, 3, 1)
        row += 1
        grid.attach(self.remote_entry, 0, row, 3, 1)
        row += 1
        grid.attach(self.local_entry, 0, row, 2, 1)
        pick = Gtk.Button(label="…")
        pick.connect("clicked", lambda *_a: self._browse(self.local_entry, folder=False))
        grid.attach(pick, 2, row, 1, 1)
        row += 1
        client_buttons = Gtk.Box(spacing=8)
        self.get_button = style.add_class(Gtk.Button(label=_("client_get")), "suggested-action")
        self.put_button = Gtk.Button(label=_("client_put"))
        self.get_button.connect("clicked", lambda *_a: self.do_get())
        self.put_button.connect("clicked", lambda *_a: self.do_put())
        client_buttons.pack_start(self.get_button, False, False, 0)
        client_buttons.pack_start(self.put_button, False, False, 0)
        grid.attach(client_buttons, 0, row, 3, 1)
        row += 1
        self.client_status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        grid.attach(self.client_status, 0, row, 3, 1)

        scroller = Gtk.ScrolledWindow()
        scroller.add(grid)
        self.widget = scroller
        self.set_interfaces(list_interfaces(include_loopback=True))
        self.update_limits()

    # -- helpers ---------------------------------------------------------

    def _browse(self, entry: Gtk.Entry, *, folder: bool) -> None:
        action = Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN
        dialog = Gtk.FileChooserDialog(
            title=_("choose_folder" if folder else "choose_file"), transient_for=self.window, action=action)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        if dialog.run() == Gtk.ResponseType.OK:
            entry.set_text(dialog.get_filename())
        dialog.destroy()

    def set_interfaces(self, names: list[str]) -> None:
        fill_interface_combo(self.iface_combo, names)

    def interface(self) -> str | None:
        return chosen_interface(self.iface_combo)

    def max_bytes(self) -> int:
        return int(self.max_spin.get_value()) * MIB

    def update_limits(self) -> None:
        cap = self.max_bytes()
        sizes = {f"b{b}": format_size(max_transfer_bytes(b, cap)) for b in _BLOCKS}
        self.limit_label.set_text(_("tftp_limit_line", **sizes))

    def _set_running(self, running: bool, port: int | None = None) -> None:
        self.running, self.port = running, port
        self.start_button.set_sensitive(not running)
        self.stop_button.set_sensitive(running)
        self.window.update_status()

    # -- server ----------------------------------------------------------

    def start(self) -> None:
        root = self.root_entry.get_text().strip()
        if not root:
            self.window.notice(_("bad_arguments", detail=_("tftp_root")), "error")
            return
        reason = sensitive_folder_reason(root)
        if reason and not self.window.ask(_("tftp_exposed_title"), _(f"tftp_exposed_{reason}", folder=root)):
            return
        args = dict(root=root, port=69, interface=self.interface(),
                    allow_write=self.write_switch.get_active(), max_bytes=self.max_bytes())
        self.window.run_async(
            lambda: self.window.helper.call("start_tftp", **args),
            lambda result, error: self.window.notice(error_text(error), "error") if error
            else self._set_running(True, result.get("port")))

    def stop(self) -> None:
        self.window.run_async(
            lambda: self.window.helper.call("stop_tftp"),
            lambda result, error: self.window.notice(error_text(error), "error") if error
            else self._set_running(False))

    def on_event(self, data: dict) -> None:
        if data.get("event") == "transfer":
            self.transfers.append([data.get("peer", ""), data.get("filename", ""), data.get("direction", ""),
                                   str(data.get("bytes", 0)), str(data.get("result", ""))])

    # -- client: runs in this process, as the user -----------------------

    def _client_args(self):
        host = self.host_entry.get_text().strip()
        remote = self.remote_entry.get_text().strip()
        local = self.local_entry.get_text().strip()
        if not (host and remote and local):
            self.window.notice(_("bad_arguments", detail=_("client_host")), "error")
            return None
        return host, remote, Path(local).expanduser()

    def _finish_client(self, result, error) -> None:
        if error is not None:
            code = getattr(error, "code", None) or type(error).__name__
            message = getattr(error, "message", "")
            text = _("client_error", code=f"{message} ({code})" if message else code)
        else:
            text = _("client_done", bytes=result)
        self.client_status.set_text(text)
        self.window.set_client_busy(False)

    def do_get(self) -> None:
        args = self._client_args()
        if args:
            host, remote, local = args
            self.window.set_client_busy(True)
            self.window.run_async(lambda: self.window.tftp_get(host, remote, local), self._finish_client)

    def do_put(self) -> None:
        args = self._client_args()
        if args:
            host, remote, local = args
            self.window.set_client_busy(True)
            self.window.run_async(lambda: self.window.tftp_put(host, local, remote), self._finish_client)
