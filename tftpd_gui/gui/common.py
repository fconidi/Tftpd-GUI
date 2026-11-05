"""Helpers shared by the window and its tabs."""

from __future__ import annotations

import threading
from typing import Callable

from ..i18n import translate
from .helper_client import HelperError


def error_text(exc: Exception) -> str:
    """A readable, translated sentence for an error from the helper."""
    if isinstance(exc, HelperError):
        code, detail = exc.code, exc.detail
        if code == "port_busy":
            return translate("port_busy", port=detail)
        if code == "access_denied":
            return translate("access_denied")
        if code == "config_invalid":
            return translate(f"dhcp_config_{detail}")
        if code in ("helper_exited", "already_running"):
            return translate(code)
        if code == "bad_arguments":
            return translate("bad_arguments", detail=detail)
        return translate("error_generic", code=code if detail is None else f"{code} ({detail})")
    return translate("error_generic", code=type(exc).__name__)


def thread_runner(work: Callable[[], object], done: Callable[[object, Exception | None], None]) -> None:
    """Run ``work`` on a thread and deliver ``done(result, error)`` on the GTK main loop."""
    from gi.repository import GLib

    def target() -> None:
        result, error = None, None
        try:
            result = work()
        except Exception as exc:  # reported to the user by ``done``
            error = exc
        GLib.idle_add(lambda: (done(result, error), False)[1])

    threading.Thread(target=target, daemon=True).start()
