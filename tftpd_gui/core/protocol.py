"""Line protocol between the window and the privileged helper.

One JSON object per line, UTF-8, on the helper's stdin and stdout.
"""

from __future__ import annotations

import json
from typing import IO, Iterator

MAX_LINE = 1 << 20


def encode_message(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8") + b"\n"


def iter_messages(stream: IO[bytes]) -> Iterator[dict]:
    """Yield each decoded object; a bad or oversized line yields ``{"error": ...}``."""
    while True:
        line = stream.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE and not line.endswith(b"\n"):
            # Discard the rest of the oversized line, then carry on.
            while True:
                rest = stream.readline(MAX_LINE)
                if not rest or rest.endswith(b"\n"):
                    break
            yield {"error": "line_too_long"}
            continue
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            yield {"error": "bad_json"}
            continue
        if not isinstance(obj, dict):
            yield {"error": "bad_json"}
            continue
        yield obj
