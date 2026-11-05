import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .gui.common import error_text  # noqa: E402
from .gui.helper_client import HelperClient, HelperError  # noqa: E402
from .gui.window import MainWindow  # noqa: E402


def main() -> int:
    helper = HelperClient()
    try:
        helper.start()
        helper.call("status")  # waits for the polkit prompt and proves the helper runs
    except HelperError as exc:
        helper.close()
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                   text=error_text(exc))
        dialog.run()
        return 1
    Gtk.Window.set_default_icon_name("tftpd-gui")
    window = MainWindow(helper)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    window.infobar.hide()
    window.dhcp_tab.update_banner()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
