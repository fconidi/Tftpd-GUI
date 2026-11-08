import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from tftpd_gui.core.tftp_packet import TftpError  # noqa: E402
from tftpd_gui.core.tftp_server import max_transfer_bytes  # noqa: E402
from tftpd_gui.gui.helper_client import HelperError  # noqa: E402
from tftpd_gui.gui.tftp_tab import format_size  # noqa: E402
from tftpd_gui.gui.window import MainWindow  # noqa: E402
from tftpd_gui.i18n import translate  # noqa: E402


class FakeHelper:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.errors = {}
        self.events = []
        self.closed = False

    def call(self, cmd, **args):
        self.calls.append((cmd, args))
        if cmd in self.errors:
            raise self.errors[cmd]
        return self.results.get(cmd, {})

    def poll_events(self):
        events, self.events = self.events, []
        return events

    def close(self):
        self.closed = True

    def names(self):
        return [name for name, _ in self.calls]


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def sync(work, done):
    try:
        result, error = work(), None
    except Exception as exc:
        result, error = None, exc
    done(result, error)


class GuiSmokeTest(unittest.TestCase):
    def setUp(self):
        self.helper = FakeHelper()
        self.clock = Clock()
        self.gets, self.puts = [], []
        self.answers = []
        self.asked = []

        def ask(title, body):
            self.asked.append((title, body))
            return self.answers.pop(0) if self.answers else True

        def tftp_get(host, remote, local):
            self.gets.append((host, remote, local))
            return 123

        def tftp_put(host, local, remote):
            self.puts.append((host, local, remote))
            if remote == "boom":
                raise OSError("boom")
            if remote == "denied":
                raise TftpError(2, "uploads are disabled")
            return 456

        self.window = MainWindow(self.helper, clock=self.clock, tftp_get=tftp_get, tftp_put=tftp_put,
                                 run_async=sync, ask=ask, timers=False)
        self.addCleanup(self.window.destroy)
        self.tftp, self.dhcp, self.syslog = self.window.tftp_tab, self.window.dhcp_tab, self.window.syslog_tab
        self.dhcp.set_interfaces(["eth0", "eth1"])
        self.dhcp.pool_start.set_text("192.168.50.100")
        self.dhcp.pool_end.set_text("192.168.50.110")
        self.helper.results["probe_dhcp"] = {"status": "not_found", "servers": [], "reason": None}
        self.helper.results["start_dhcp"] = {"port": 67, "interface": "eth0",
                                             "server_ip": "192.168.50.1", "netmask": "255.255.255.0"}

    def notice_texts(self):
        return [text for _kind, text in self.window.notices]

    def test_window_builds_with_three_tabs(self):
        self.assertEqual(self.window.notebook.get_n_pages(), 3)
        self.assertEqual(self.window.status.get_text(), translate("status_idle"))

    def test_helper_exit_clears_all_service_states(self):
        self.tftp._set_running(True, 69)
        self.dhcp._set_running(True)
        self.syslog._set_running(True)
        self.helper.events = [{"event": "helper_exited"}]
        self.window.tick()
        self.assertFalse(self.tftp.running)
        self.assertFalse(self.dhcp.running)
        self.assertFalse(self.syslog.running)
        self.assertEqual(self.window.status.get_text(), translate("status_idle"))
        self.assertIn(translate("helper_exited"), self.notice_texts())
        self.assertTrue(self.helper.closed)
        self.assertFalse(self.tftp.start_button.get_sensitive())

    def test_tftp_start_sends_the_chosen_root_interface_write_flag_and_max_bytes(self):
        self.tftp.set_interfaces(["eth0"])
        self.tftp.iface_combo.set_active(1)
        self.tftp.root_entry.set_text("/srv/tftp")
        self.tftp.write_switch.set_active(True)
        self.tftp.max_spin.set_value(64)
        self.helper.results["start_tftp"] = {"port": 69}
        self.tftp.start()
        self.assertEqual(self.helper.calls[0], ("start_tftp", dict(
            root="/srv/tftp", port=69, interface="eth0", allow_write=True, max_bytes=64 * 1024 ** 2)))
        self.assertTrue(self.tftp.running)
        self.assertIn("TFTP 69", self.window.status.get_text())

    def test_tftp_interface_menu_offers_all_interfaces_then_loopback(self):
        names = [row[0] for row in self.tftp.iface_combo.get_model()]
        self.assertEqual(names[0], translate("all_interfaces"))
        self.assertEqual(names[1], "lo")

    def test_tftp_start_can_bind_to_the_loopback_interface(self):
        names = [row[0] for row in self.tftp.iface_combo.get_model()]
        self.tftp.iface_combo.set_active(names.index("lo"))
        self.tftp.root_entry.set_text("/srv/tftp")
        self.helper.results["start_tftp"] = {"port": 69}
        self.tftp.start()
        self.assertEqual(self.helper.calls[0][1]["interface"], "lo")

    def test_tftp_start_asks_before_sharing_the_home_directory(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            self.tftp.root_entry.set_text(home)
            self.answers = [False]
            self.tftp.start()
        self.assertEqual(len(self.asked), 1)
        self.assertIn(home, self.asked[0][1])
        self.assertEqual(self.helper.calls, [])
        self.assertFalse(self.tftp.running)

    def test_tftp_start_shares_the_home_directory_when_confirmed(self):
        self.helper.results["start_tftp"] = {"port": 69}
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            self.tftp.root_entry.set_text(home)
            self.answers = [True]
            self.tftp.start()
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.helper.names(), ["start_tftp"])
        self.assertTrue(self.tftp.running)

    def test_tftp_start_does_not_ask_for_an_ordinary_folder(self):
        self.helper.results["start_tftp"] = {"port": 69}
        self.tftp.root_entry.set_text("/srv/tftp")
        self.tftp.start()
        self.assertEqual(self.asked, [])

    def test_tftp_start_without_a_folder_calls_nothing(self):
        self.tftp.start()
        self.assertEqual(self.helper.calls, [])
        self.assertTrue(self.window.notices)

    def test_tftp_tab_shows_the_effective_limit_for_512_1428_and_8192_byte_blocks(self):
        self.tftp.max_spin.set_value(4096)
        text = self.tftp.limit_label.get_text()
        for block in (512, 1428, 8192):
            self.assertIn(format_size(max_transfer_bytes(block, 4096 * 1024 ** 2)), text)
        before = text
        self.tftp.max_spin.set_value(10)
        self.assertNotEqual(self.tftp.limit_label.get_text(), before)
        self.assertIn("10.00 MiB", self.tftp.limit_label.get_text())

    def test_tftp_stop_sends_stop_tftp(self):
        self.helper.results["start_tftp"] = {"port": 69}
        self.tftp.root_entry.set_text("/srv/tftp")
        self.tftp.start()
        self.tftp.stop()
        self.assertEqual(self.helper.names()[-1], "stop_tftp")
        self.assertFalse(self.tftp.running)

    def test_tftp_client_get_runs_in_the_window_process_not_in_the_helper(self):
        self.tftp.host_entry.set_text("192.168.50.2")
        self.tftp.remote_entry.set_text("cfg.txt")
        self.tftp.local_entry.set_text("/tmp/cfg.txt")
        self.tftp.do_get()
        self.assertEqual(self.gets, [("192.168.50.2", "cfg.txt", Path("/tmp/cfg.txt"))])
        self.assertEqual(self.helper.calls, [])
        self.assertIn("123", self.tftp.client_status.get_text())

    def test_tftp_client_put_runs_in_the_window_process_not_in_the_helper(self):
        self.tftp.host_entry.set_text("192.168.50.2")
        self.tftp.remote_entry.set_text("fw.bin")
        self.tftp.local_entry.set_text("/tmp/fw.bin")
        self.tftp.do_put()
        self.assertEqual(self.puts, [("192.168.50.2", Path("/tmp/fw.bin"), "fw.bin")])
        self.assertEqual(self.helper.calls, [])
        self.assertIn("456", self.tftp.client_status.get_text())
        self.assertTrue(self.tftp.put_button.get_sensitive())

    def test_tftp_client_error_is_shown_with_its_code(self):
        self.tftp.host_entry.set_text("h")
        self.tftp.remote_entry.set_text("boom")
        self.tftp.local_entry.set_text("/tmp/x")
        self.tftp.do_put()
        self.assertIn("OSError", self.tftp.client_status.get_text())

    def test_tftp_client_error_shows_the_server_message_and_code(self):
        self.tftp.host_entry.set_text("h")
        self.tftp.remote_entry.set_text("denied")
        self.tftp.local_entry.set_text("/tmp/x")
        self.tftp.do_put()
        text = self.tftp.client_status.get_text()
        self.assertIn("uploads are disabled", text)
        self.assertIn("2", text)

    def test_tftp_client_with_missing_fields_does_nothing(self):
        self.tftp.do_get()
        self.assertEqual(self.gets, [])

    def test_dhcp_start_without_confirmation_calls_nothing(self):
        self.answers = [False]
        self.dhcp.request_start()
        self.assertEqual(self.helper.calls, [])
        self.assertIn("eth0", self.asked[0][1])
        self.assertIn("192.168.50.100", self.asked[0][1])

    def test_dhcp_start_probes_the_segment_before_starting(self):
        self.dhcp.request_start()
        self.assertEqual(self.helper.names(), ["probe_dhcp", "start_dhcp"])
        self.assertTrue(self.dhcp.running)

    def test_dhcp_start_sends_the_form_values_and_never_the_server_address(self):
        self.dhcp.router.set_text("192.168.50.1")
        self.dhcp.dns.set_text("8.8.8.8, 1.1.1.1")
        self.dhcp.request_start()
        args = self.helper.calls[1][1]
        self.assertEqual(args["dns"], ["8.8.8.8", "1.1.1.1"])
        self.assertEqual(args["router"], "192.168.50.1")
        self.assertEqual(args["auto_stop_seconds"], 1800)
        self.assertNotIn("server_ip", args)
        self.assertNotIn("netmask", args)

    def test_dhcp_start_without_pool_calls_nothing(self):
        self.dhcp.pool_end.set_text("")
        self.dhcp.request_start()
        self.assertEqual(self.helper.calls, [])
        self.assertEqual(self.asked, [])

    def test_foreign_dhcp_server_requires_a_second_confirmation(self):
        self.helper.results["probe_dhcp"] = {"status": "found", "reason": None, "servers": [
            {"server_id": "192.168.50.254", "offered_ip": "192.168.50.50", "mac": None}]}
        self.answers = [True, False]
        self.dhcp.request_start()
        self.assertEqual(len(self.asked), 2)
        self.assertIn("192.168.50.254", self.asked[1][1])
        self.assertEqual(self.helper.names(), ["probe_dhcp"])
        self.assertFalse(self.dhcp.running)

    def test_foreign_server_confirmed_starts(self):
        self.helper.results["probe_dhcp"] = {"status": "found", "reason": None, "servers": [
            {"server_id": "1.2.3.4", "offered_ip": "1.2.3.5", "mac": None}]}
        self.dhcp.request_start()
        self.assertEqual(self.helper.names(), ["probe_dhcp", "start_dhcp"])

    def test_probe_error_does_not_start_and_offers_start_anyway(self):
        self.helper.results["probe_dhcp"] = {"status": "error", "servers": [], "reason": "port 68 busy"}
        self.answers = [True, False]
        self.dhcp.request_start()
        self.assertEqual(self.helper.names(), ["probe_dhcp"])
        self.assertIn("port 68 busy", self.asked[1][1])
        self.answers = [True, True]
        self.dhcp.request_start()
        self.assertEqual(self.helper.names()[-1], "start_dhcp")

    def test_probe_call_failure_is_treated_like_a_probe_error(self):
        self.helper.errors["probe_dhcp"] = HelperError("os_error", "denied")
        self.answers = [True, False]
        self.dhcp.request_start()
        self.assertEqual(self.helper.names(), ["probe_dhcp"])
        self.assertEqual(len(self.asked), 2)

    def test_probe_not_found_dialog_says_silence_does_not_prove_the_segment_is_free(self):
        self.dhcp.request_start()
        self.assertIn(translate("dhcp_probe_not_found"), self.notice_texts())
        self.assertIn("not prove", translate("dhcp_probe_not_found", "en"))

    def test_next_server_without_a_running_tftp_server_shows_a_warning(self):
        self.dhcp.next_server.set_text("192.168.50.1")
        self.dhcp.request_start()
        self.assertIn(translate("dhcp_next_server_warning"), self.notice_texts())

    def test_next_server_with_tftp_running_shows_no_warning(self):
        self.tftp.running = True
        self.dhcp.next_server.set_text("192.168.50.1")
        self.dhcp.request_start()
        self.assertNotIn(translate("dhcp_next_server_warning"), self.notice_texts())

    def test_dhcp_config_invalid_error_is_shown_with_a_readable_reason(self):
        self.helper.errors["start_dhcp"] = HelperError("config_invalid", "pool_outside_subnet")
        self.dhcp.request_start()
        self.assertIn(translate("dhcp_config_pool_outside_subnet"), self.notice_texts())
        self.assertFalse(self.dhcp.running)

    def test_dhcp_banner_shows_the_countdown_from_the_injected_clock(self):
        self.dhcp.request_start()
        self.assertIn("30:00", self.dhcp.banner.get_text())
        self.clock.now += 90
        self.window.tick()
        self.assertIn("28:30", self.dhcp.banner.get_text())
        self.assertIn("192.168.50.1", self.dhcp.address_label.get_text())

    def test_dhcp_stopped_event_clears_the_banner_and_warns(self):
        self.dhcp.request_start()
        self.helper.events = [{"event": "dhcp", "data": {"event": "stopped", "reason": "timeout"}}]
        self.window.tick()
        self.assertFalse(self.dhcp.running)
        self.assertIn(translate("dhcp_autostop"), self.notice_texts())
        self.assertEqual(self.helper.names()[-1], "stop_dhcp")

    def test_lease_table_is_filled_from_status(self):
        self.dhcp.request_start()
        self.helper.results["status"] = {"dhcp": {"leases": [
            {"ip": "192.168.50.100", "mac": "02:00:00:00:00:01", "hostname": "sw1", "expires": 1}]}}
        for _i in range(4):
            self.window.tick()
        self.assertEqual(len(self.dhcp.leases), 1)

    def test_syslog_events_append_rows_to_the_list(self):
        self.helper.events = [{"event": "syslog", "data": {
            "raw": "<131>Nov  5 10:00:00 sw1 link down", "message": "link down", "host": "sw1",
            "severity": 3, "source": "10.0.0.1", "received": 0}}]
        self.window.tick()
        self.assertEqual(len(self.syslog.store), 1)
        self.assertEqual(self.syslog.store[0][2], "err")

    def test_syslog_filter_and_severity_limit_the_rows(self):
        for n, sev in enumerate((3, 6)):
            self.syslog.on_event({"raw": f"msg{n}", "message": f"msg{n}", "host": "h", "severity": sev,
                                  "source": "s", "received": 0})
        self.syslog.severity_combo.set_active(4)  # err and worse
        self.assertEqual([m["raw"] for m in self.syslog.visible_messages()], ["msg0"])
        self.syslog.severity_combo.set_active(0)
        self.syslog.filter_entry.set_text("msg1")
        self.assertEqual(len(self.syslog.store), 1)

    def test_syslog_save_writes_the_file_from_the_window_process(self):
        for n in range(2):
            self.syslog.on_event({"raw": f"line {n}", "message": "m", "host": "h", "severity": 6,
                                  "source": "s", "received": 0})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.txt"
            self.assertEqual(self.syslog.save(path), 2)
            self.assertEqual(path.read_text(), "line 0\nline 1\n")
        self.assertEqual(self.helper.calls, [])

    def test_syslog_start_and_stop_call_the_helper(self):
        self.syslog.start()
        self.assertTrue(self.syslog.running)
        self.syslog.stop()
        self.assertEqual(self.helper.names(), ["start_syslog", "stop_syslog"])

    def test_port_busy_error_is_shown_with_its_port_number(self):
        self.helper.errors["start_tftp"] = HelperError("port_busy", 69)
        self.tftp.root_entry.set_text("/srv")
        self.tftp.start()
        self.assertIn(translate("port_busy", port=69), self.notice_texts())
        self.assertFalse(self.tftp.running)

    def test_closing_the_window_closes_the_helper(self):
        self.window.destroy()
        self.assertTrue(self.helper.closed)

    def test_tftp_transfer_events_fill_the_transfer_list(self):
        self.helper.events = [{"event": "tftp", "data": {
            "event": "transfer", "peer": "10.0.0.2:5000", "filename": "a.bin", "direction": "read",
            "bytes": 10, "result": "ok"}}]
        self.window.tick()
        self.assertEqual(list(self.tftp.transfers[0]), ["10.0.0.2:5000", "a.bin", "read", "10", "ok"])


if __name__ == "__main__":
    unittest.main()
