from __future__ import annotations

import re
from pathlib import Path


def _safe_download_name(file_name: str | None, default: str = "telegram-audio") -> str:
    source = Path(file_name or default).name
    stem = Path(source).stem or default
    suffix = Path(source).suffix.lower()
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")[:80] or default
    if suffix and not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        suffix = ""
    return f"{safe_stem}{suffix}"
