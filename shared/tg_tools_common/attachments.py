from __future__ import annotations

import re
from pathlib import Path


DIRECT_TEXT_DOCUMENT_SUFFIXES = (".txt", ".md", ".markdown")
DIRECT_TEXT_DOCUMENT_MIME_TYPES = {
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/markdown",
}
MARKIT_DOCUMENT_SUFFIXES = (
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".html",
    ".htm",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
)
MARKIT_DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/html",
    "application/xhtml+xml",
    "text/csv",
    "text/tab-separated-values",
    "application/json",
    "application/yaml",
    "application/x-yaml",
    "text/yaml",
    "text/x-yaml",
    "application/xml",
    "text/xml",
}
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def classify_prompt_document(file_name: str | None, mime_type: str | None) -> str | None:
    suffix = Path(file_name or "").suffix.lower()
    normalized_mime = (mime_type or "").lower()
    if suffix in DIRECT_TEXT_DOCUMENT_SUFFIXES or normalized_mime in DIRECT_TEXT_DOCUMENT_MIME_TYPES:
        return "text_document"
    if suffix in MARKIT_DOCUMENT_SUFFIXES or normalized_mime in MARKIT_DOCUMENT_MIME_TYPES:
        return "markit_document"
    return None


def sanitize_attachment_name(file_name: str | None, *, fallback_stem: str, default_suffix: str) -> str:
    raw_name = Path(file_name or "").name
    raw_path = Path(raw_name) if raw_name else None
    stem = (raw_path.stem if raw_path else fallback_stem).strip() or fallback_stem
    suffix = raw_path.suffix if raw_path else default_suffix
    stem = _FILENAME_RE.sub("-", stem).strip("._-") or fallback_stem
    suffix = _FILENAME_RE.sub("", suffix)
    if not suffix:
        suffix = default_suffix
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    return f"{stem}{suffix}"


def validate_text_attachment_bytes(payload: bytes, max_bytes: int) -> str:
    if not payload:
        raise ValueError("Text attachment is empty.")
    if len(payload) > max_bytes:
        raise ValueError(f"Text attachment is too large ({len(payload)} bytes > {max_bytes} bytes).")
    if b"\x00" in payload:
        raise ValueError("Text attachment looks binary.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Text attachment must be UTF-8 encoded.") from exc
    if not text.strip():
        raise ValueError("Text attachment is empty.")
    return text


def audio_suffix_from_mime(mime_type: str | None) -> str:
    normalized = (mime_type or "").lower()
    return {
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/flac": ".flac",
        "audio/aac": ".aac",
        "audio/webm": ".webm",
    }.get(normalized, ".ogg")


def text_suffix_for_attachment(file_name: str, mime_type: str | None) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return suffix
    if (mime_type or "").lower() in {"text/markdown", "text/x-markdown", "application/markdown"}:
        return ".md"
    return ".txt"
