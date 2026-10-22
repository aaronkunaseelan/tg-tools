"""Local document-conversion prerequisites.

Conversion and URL fetching are tg-tools' own code (``tts_common.convert`` and
``tts_common.source_fetch``); the only external binary is poppler's
``pdftotext``. Validation runs its version command and nothing else.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

DEFAULT_PDFTOTEXT_BIN = "pdftotext"


class DocumentToolError(RuntimeError):
    """A service configuration error, never a source-fetch result."""


def pdftotext_bin(env: Mapping[str, str] | None = None) -> str:
    """Resolved pdftotext path (``TTS_PDFTOTEXT_BIN`` or PATH)."""
    values = os.environ if env is None else env
    binary = str(values.get("TTS_PDFTOTEXT_BIN") or "").strip() or DEFAULT_PDFTOTEXT_BIN
    if Path(binary).parent != Path("."):
        path = Path(binary).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise DocumentToolError(f"pdftotext missing or not executable: {binary}")
    resolved = shutil.which(binary, path=values.get("PATH", os.defpath))
    if not resolved:
        raise DocumentToolError(f"pdftotext missing or not executable: {binary}")
    return resolved


def validate_document_tools(env: Mapping[str, str] | None = None) -> dict[str, str]:
    binary = pdftotext_bin(env)
    try:
        completed = subprocess.run([binary, "-v"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocumentToolError(f"pdftotext version probe failed: {exc}") from exc
    version = (completed.stderr or completed.stdout).strip().splitlines()
    if completed.returncode != 0 or not version or not version[0].startswith("pdftotext version "):
        raise DocumentToolError("pdftotext has invalid version identity")
    return {"path": binary, "version": version[0]}


def startup_check(settings: Any | None = None) -> tuple[list[str], list[str]]:
    """Run no-registration startup validation for CLI and service callers."""
    from .startup import validate_startup

    if settings is None:
        from .config import load_settings
        settings = load_settings()
    return validate_startup(settings)
