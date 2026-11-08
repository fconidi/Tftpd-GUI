"""Detect folders that hold private data before they are shared over TFTP."""

from __future__ import annotations

from pathlib import Path

_SECRET_DIRS = (".ssh", ".gnupg")


def sensitive_folder_reason(root: str | Path, home: Path | None = None) -> str | None:
    """``"home"`` if ``root`` is the home directory or one of its parents,
    ``"secrets"`` if it directly holds ``.ssh`` or ``.gnupg``, otherwise ``None``."""
    try:
        folder = Path(root).expanduser().resolve()
        home_dir = (home if home is not None else Path.home()).resolve()
    except (OSError, RuntimeError):
        return None
    if not folder.is_dir():
        return None
    if folder == home_dir or folder in home_dir.parents:
        return "home"
    if any((folder / name).is_dir() for name in _SECRET_DIRS):
        return "secrets"
    return None
