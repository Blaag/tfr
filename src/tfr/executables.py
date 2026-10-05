from __future__ import annotations

import os
import shutil
from pathlib import Path


def find_uv() -> Path | None:
    discovered = shutil.which("uv")
    if discovered is not None:
        return Path(discovered)
    for candidate in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def require_uv() -> Path:
    uv = find_uv()
    if uv is None:
        raise FileNotFoundError(
            "uv is unavailable on this host; checked the process PATH, "
            "~/.local/bin/uv, and ~/.cargo/bin/uv"
        )
    return uv
