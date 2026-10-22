"""Secure typed source acquisition.

URL bytes come only through ``source_fetch`` (public addresses checked at the
connection, every redirect re-checked, no proxy, byte cap, no userinfo).
This module adds deterministic effective-type resolution:

1. strong supported byte signature;
2. specific non-generic declared MIME;
3. safe filename/URL suffix fallback;
4. clean text fallback only when no binary signature exists.

A binary claim (PDF/OOXML) carrying strong HTML bytes becomes
``source_access_blocked`` (known challenge shell) or ``source_type_mismatch``.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

from .source_fetch import FetchFailure, fetch
from .taxonomy import (
    EXTRACT_INSUFFICIENT_TEXT,
    SOURCE_ACCESS_BLOCKED,
    SOURCE_FETCH_TRANSIENT,
    SOURCE_TOO_LARGE,
    SOURCE_TYPE_MISMATCH,
    is_challenge_shell,
)

DEFAULT_MAX_SOURCE_BYTES = 20 * 1024 * 1024

_BINARY_CLAIM_EXTS = {"pdf", "docx", "pptx", "xlsx"}
_TEXT_EXTS = {"txt", "md", "markdown", "json", "xml", "csv", "html", "htm"}

_MIME_TO_EXT = {
    "application/pdf": "pdf",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/json": "json",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/csv": "csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
_GENERIC_MIMES = {"application/octet-stream", "binary/octet-stream", "", "application/binary"}

# source_fetch failure class -> frozen taxonomy class.
_FAILURE_MAP = {
    "blocked_fetch_target": SOURCE_ACCESS_BLOCKED,
    "invalid_url": SOURCE_ACCESS_BLOCKED,
    "http_forbidden": SOURCE_ACCESS_BLOCKED,
    "http_not_found": SOURCE_ACCESS_BLOCKED,
    "http_client_error": SOURCE_ACCESS_BLOCKED,
    "tls_error": SOURCE_ACCESS_BLOCKED,
    "too_many_redirects": SOURCE_ACCESS_BLOCKED,
    "byte_cap_exceeded": SOURCE_TOO_LARGE,
    "http_rate_limited": SOURCE_FETCH_TRANSIENT,
    "http_server_error": SOURCE_FETCH_TRANSIENT,
    "network_transient": SOURCE_FETCH_TRANSIENT,
    "timeout": SOURCE_FETCH_TRANSIENT,
}


class TypedFetchError(RuntimeError):
    def __init__(self, error_class: str, message: str, *, failure_class: str = "",
                 temporary: bool | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.failure_class = failure_class
        self.temporary = temporary


@dataclass(frozen=True)
class FetchedSource:
    data: bytes
    requested_url: str
    final_url: str
    declared_mime: str
    sha256: str
    filename: str


def max_source_bytes() -> int:
    return int(os.environ.get("TTS_MAX_SOURCE_BYTES", DEFAULT_MAX_SOURCE_BYTES))


# -- byte signatures ----------------------------------------------------------

def _looks_html(data: bytes) -> bool:
    head = data[:2048].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head[:512]


def _zip_content_kind(data: bytes) -> str | None:
    """Bounded OOXML detection: local header + central-directory names only.

    Never decompresses entries (ZIP-bomb safe); scans a bounded tail window
    for the end-of-central-directory record.
    """
    if not data.startswith(b"PK\x03\x04"):
        return None
    tail = data[-66000:]
    idx = tail.rfind(b"PK\x05\x06")
    if idx == -1:
        return "zip"
    try:
        cd_size, cd_offset = struct.unpack("<II", tail[idx + 12:idx + 20])
    except struct.error:
        return "zip"
    if cd_offset + cd_size > len(data) or cd_size > 10 * 1024 * 1024:
        return "zip"
    directory = data[cd_offset:cd_offset + cd_size]
    if b"word/document.xml" in directory:
        return "docx"
    if b"ppt/presentation.xml" in directory:
        return "pptx"
    if b"xl/workbook.xml" in directory:
        return "xlsx"
    if b"[Content_Types].xml" in directory:
        return "ooxml"
    return "zip"


def strong_signature(data: bytes) -> str | None:
    """Return a strong signature kind or None."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    zip_kind = _zip_content_kind(data)
    if zip_kind:
        return zip_kind
    if _looks_html(data):
        return "html"
    return None


def _is_clean_text(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text.strip():
        return False
    sample = text[:20000]
    control = sum(1 for c in sample if ord(c) < 32 and c not in "\n\r\t")
    replacement = sample.count("�")
    return control <= len(sample) * 0.01 and replacement <= len(sample) * 0.01


def effective_extension(*, data: bytes, declared_mime: str, name: str) -> str:
    """Deterministic effective type; raises on binary-claim/HTML conflicts."""
    declared = (declared_mime or "").split(";")[0].strip().lower()
    suffix = Path(name or "").suffix.lstrip(".").lower()
    claimed_binary = (
        _MIME_TO_EXT.get(declared) in _BINARY_CLAIM_EXTS
        or suffix in _BINARY_CLAIM_EXTS
    )
    sig = strong_signature(data)
    if sig in {"pdf", "docx", "pptx", "xlsx"}:
        return sig
    if sig in {"ooxml", "zip"}:
        if _MIME_TO_EXT.get(declared) in _BINARY_CLAIM_EXTS:
            return _MIME_TO_EXT[declared]
        if suffix in _BINARY_CLAIM_EXTS:
            return suffix
        raise TypedFetchError(SOURCE_TYPE_MISMATCH, "unrecognised zip container")
    if sig == "html":
        if claimed_binary:
            if is_challenge_shell(data):
                raise TypedFetchError(
                    SOURCE_ACCESS_BLOCKED,
                    "server returned an access-challenge page instead of the document; "
                    "download the file and upload it directly")
            raise TypedFetchError(
                SOURCE_TYPE_MISMATCH,
                "server returned an HTML page instead of the claimed binary document")
        return "html"
    # No strong binary signature.
    if declared not in _GENERIC_MIMES and declared in _MIME_TO_EXT:
        ext = _MIME_TO_EXT[declared]
        if ext in _BINARY_CLAIM_EXTS:
            raise TypedFetchError(
                SOURCE_TYPE_MISMATCH, f"declared {declared} but bytes lack its signature")
        if not _is_clean_text(data):
            raise TypedFetchError(
                SOURCE_TYPE_MISMATCH,
                f"declared textual {declared} but bytes are not clean UTF-8 text")
        return ext
    if suffix in _TEXT_EXTS and _is_clean_text(data):
        return "md" if suffix == "markdown" else suffix
    if suffix in _BINARY_CLAIM_EXTS:
        raise TypedFetchError(
            SOURCE_TYPE_MISMATCH, f"suffix .{suffix} claimed but bytes lack its signature")
    if _is_clean_text(data):
        return "txt"
    raise TypedFetchError(SOURCE_TYPE_MISMATCH, "unsupported binary content")


# -- extraction sufficiency ----------------------------------------------------

def check_extraction_sufficiency(markdown: str, *, min_chars: int | None = None) -> None:
    if min_chars is None:
        min_chars = int(os.environ.get("TTS_MIN_EXTRACT_CHARS", "200"))
    text = markdown.strip()
    if len(text) < min_chars:
        raise TypedFetchError(
            EXTRACT_INSUFFICIENT_TEXT,
            f"extraction produced only {len(text)} chars (minimum {min_chars})")
    sample = text[:50000]
    control = sum(1 for c in sample if ord(c) < 32 and c not in "\n\r\t")
    replacement = sample.count("�")
    if control > len(sample) * 0.02 or replacement > len(sample) * 0.02:
        raise TypedFetchError(
            EXTRACT_INSUFFICIENT_TEXT, "extraction is control/replacement-character heavy")
    if is_challenge_shell(text.encode("utf-8", errors="replace")):
        raise TypedFetchError(SOURCE_ACCESS_BLOCKED, "extraction is an access-challenge shell")


# -- URL fetch -------------------------------------------------------------------

def fetch_url(url: str, work_dir: Path, *, max_bytes: int | None = None) -> FetchedSource:
    cap = max_bytes if max_bytes is not None else max_source_bytes()
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = fetch(url, max_bytes=cap)
    except FetchFailure as exc:
        raise TypedFetchError(_FAILURE_MAP.get(exc.failure_class, "service_configuration_failure"),
                              f"{exc.failure_class}: {exc}", failure_class=exc.failure_class,
                              temporary=exc.temporary) from exc
    (work_dir / "fetched-original.bin").write_bytes(result.data)
    return FetchedSource(
        data=result.data,
        requested_url=url,
        final_url=result.final_url,
        declared_mime=result.content_type,
        sha256=result.sha256,
        filename=result.filename,
    )
