"""Window-side handle on the privileged helper process."""

from __future__ import annotations

import itertools
import queue
import subprocess
import threading

from ..core.protocol import encode_message, iter_messages

EVENT_QUEUE_MAX = 1000
DEFAULT_COMMAND = ["pkexec", "/usr/lib/tftpd-gui/tftpd-gui-helper"]
_CALL_TIMEOUT = 30.0
_DENIED_CODES = (126, 127)


class HelperError(Exception):
    def __init__(self, code: str, detail=None) -> None:
        super().__init__(f"{code}: {detail}" if detail is not None else code)
        self.code = code
        self.detail = detail


class HelperClient:
    def __init__(self, command: list[str] | None = None) -> None:
        self.command = list(command) if command else list(DEFAULT_COMMAND)
        self._proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._events: queue.Queue = queue.Queue(maxsize=EVENT_QUEUE_MAX)
        self._reader: threading.Thread | None = None
        self._ended = threading.Event()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("helper already started")
        try:
            self._proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True,
            )
        except OSError as exc:
            raise HelperError("helper_unavailable", str(exc)) from exc
        self._reader = threading.Thread(target=self._read, name="helper-reader", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        proc = self._proc
        try:
            for message in iter_messages(proc.stdout):
                if "event" in message:
                    self._push_event(message)
                elif isinstance(message.get("id"), int):
                    with self._lock:
                        waiter = self._pending.pop(message["id"], None)
                    if waiter is not None:
                        waiter.put(message)
        finally:
            self._ended.set()
            with self._lock:
                waiters, self._pending = list(self._pending.values()), {}
            for waiter in waiters:
                waiter.put(None)

    def _push_event(self, event: dict) -> None:
        while True:
            try:
                self._events.put_nowait(event)
                return
            except queue.Full:
                try:
                    self._events.get_nowait()  # drop the oldest
                except queue.Empty:
                    pass

    def _exit_error(self) -> HelperError:
        try:
            code = self._proc.wait(2.0)
        except subprocess.TimeoutExpired:
            code = None
        if code in _DENIED_CODES:
            return HelperError("access_denied")
        return HelperError("helper_exited", code)

    def call(self, cmd: str, **args) -> dict:
        if self._proc is None:
            raise HelperError("helper_not_started")
        ident = next(self._ids)
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            if self._ended.is_set():
                raise self._exit_error()
            self._pending[ident] = waiter
        try:
            self._proc.stdin.write(encode_message({"id": ident, "cmd": cmd, "args": args}))
            self._proc.stdin.flush()
        except (OSError, ValueError):
            with self._lock:
                self._pending.pop(ident, None)
            raise self._exit_error() from None
        try:
            reply = waiter.get(timeout=_CALL_TIMEOUT)
        except queue.Empty:
            with self._lock:
                self._pending.pop(ident, None)
            raise HelperError("timeout", cmd) from None
        if reply is None:
            raise self._exit_error()
        if not reply.get("ok"):
            raise HelperError(str(reply.get("error", "error")), reply.get("detail"))
        return reply.get("result") or {}

    def poll_events(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if self._reader is not None:
            self._reader.join(2.0)
        if proc.stdout is not None:
            proc.stdout.close()
