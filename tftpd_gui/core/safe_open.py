"""Open files strictly beneath a root directory file descriptor.

Every path component is opened with ``os.open(..., dir_fd=...)`` relative to
the previous directory descriptor and with ``O_NOFOLLOW``, so a symlink met
anywhere on the way, including one swapped in after the root was opened, is
refused instead of followed. Names are never normalised: a leading ``/`` is
refused, not stripped, and a backslash is an ordinary character.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .tftp_packet import TftpError

NOT_FOUND = 1
ACCESS_VIOLATION = 2
DISK_FULL = 3
FILE_EXISTS = 6

_DIR_FLAGS = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY | os.O_CLOEXEC


def open_root(path: Path) -> int:
    """Open the served root directory and return its descriptor."""
    return os.open(path, _DIR_FLAGS)


def _components(name: str) -> list[str]:
    if not name:
        raise TftpError(ACCESS_VIOLATION, "empty file name")
    if "\x00" in name:
        raise TftpError(ACCESS_VIOLATION, "file name contains NUL")
    if name.startswith("/"):
        raise TftpError(ACCESS_VIOLATION, "absolute file name")
    parts = name.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise TftpError(ACCESS_VIOLATION, "invalid path component")
    return parts


def _is_symlink(dir_fd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _translate(exc: OSError, dir_fd: int, name: str) -> TftpError:
    code = exc.errno
    if code == errno.ENOENT:
        return TftpError(NOT_FOUND, "file not found")
    if code == errno.EEXIST:
        return TftpError(FILE_EXISTS, "file already exists")
    if code == errno.ELOOP:
        return TftpError(ACCESS_VIOLATION, "symbolic link refused")
    if code == errno.ENOTDIR:
        # O_DIRECTORY | O_NOFOLLOW on a symlink reports ENOTDIR on Linux.
        if _is_symlink(dir_fd, name):
            return TftpError(ACCESS_VIOLATION, "symbolic link refused")
        return TftpError(NOT_FOUND, "file not found")
    if code in (errno.ENOSPC, errno.EDQUOT):
        return TftpError(DISK_FULL, "disk full")
    return TftpError(ACCESS_VIOLATION, "access violation")


def open_parent(root_fd: int, name: str) -> tuple[int, str]:
    """Walk the directory components of ``name`` beneath ``root_fd``.

    Returns a new descriptor of the parent directory and the leaf name. The
    caller closes the descriptor.
    """
    parts = _components(name)
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=current)
            except OSError as exc:
                raise _translate(exc, current, part) from None
            os.close(current)
            current = nxt
    except BaseException:
        os.close(current)
        raise
    return current, parts[-1]


def open_leaf(dir_fd: int, leaf: str, flags: int, mode: int = 0o644) -> int:
    """Open a single-component ``leaf`` inside ``dir_fd`` without following links."""
    if leaf in ("", ".", "..") or "/" in leaf or "\x00" in leaf:
        raise TftpError(ACCESS_VIOLATION, "invalid file name")
    try:
        return os.open(leaf, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dir_fd)
    except OSError as exc:
        raise _translate(exc, dir_fd, leaf) from None


def open_beneath(root_fd: int, name: str, flags: int, mode: int = 0o644) -> int:
    """Open ``name`` beneath ``root_fd`` and return the new descriptor."""
    parent_fd, leaf = open_parent(root_fd, name)
    try:
        return open_leaf(parent_fd, leaf, flags, mode)
    finally:
        os.close(parent_fd)
