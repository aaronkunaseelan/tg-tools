"""Source materialisation: staged source bytes -> ``source.md`` + ``source.meta.json``.

Conversion is tg-tools' own (``convert``); URL acquisition belongs to
``typed_fetch``.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tg_tools_common.progress import tracked_stage

from tts_worker.document_tools import DocumentToolError, pdftotext_bin

from .chatgpt_share import materialise_share
from .convert import CONVERTER_VERSION, ConversionError, to_markdown
from .classify import CHATGPT_SHARE_RE

DEFAULT_MAX_CHARS = 300_000

_MIME_EXT = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/csv": ".csv",
}
_UNSUPPORTED_SUFFIXES = {".doc", ".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


class MaterialiseError(RuntimeError):
    """An extraction failure suitable for deterministic worker mapping."""

    def __init__(self, message: str, error_class: str = "extract_failed", *,
                 disposition: str = "error", reason_code: str = "") -> None:
        super().__init__(message)
        self.error_class = error_class
        self.disposition = disposition
        self.reason_code = reason_code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _max_chars() -> int:
    return int(os.environ.get("TTS_MAX_EXTRACT_CHARS", DEFAULT_MAX_CHARS))


def _is_url(ref: str) -> bool:
    return urlparse(ref).scheme in {"http", "https"}


def _canonical_mime(value: str) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _reject_unsupported(ref: str, mime: str) -> None:
    canonical = _canonical_mime(mime)
    suffix = Path(urlparse(ref).path if _is_url(ref) else ref).suffix.lower()
    if canonical == "application/msword" or canonical.startswith("image/") or suffix in _UNSUPPORTED_SUFFIXES:
        raise MaterialiseError(
            f"unsupported document format: {canonical or suffix or 'unknown'}",
            error_class="unsupported_format")


def _read_source(source: str | bytes | Path) -> tuple[bytes, str, str]:
    if isinstance(source, bytes):
        return source, "bytes", "text/plain"
    reference = str(source)
    if _is_url(reference):
        raise MaterialiseError(
            "materialise does not fetch URLs; acquire through typed_fetch first",
            error_class="unsupported_format")
    path = Path(reference)
    data = path.read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return data, reference, mime


def _extension_for(ref: str, mime: str, source_bytes: bytes) -> str:
    _reject_unsupported(ref, mime)
    extension = _MIME_EXT.get(_canonical_mime(mime))
    if extension:
        return extension
    ref_extension = Path(urlparse(ref).path if _is_url(ref) else ref).suffix
    if ref_extension:
        return ref_extension
    guessed = mimetypes.guess_extension(mime)
    if guessed:
        return guessed
    try:
        source_bytes.decode("utf-8")
        return ".txt"
    except UnicodeDecodeError:
        return ".bin"


def _convert(input_path: Path, markdown_path: Path, declared_mime: str) -> dict[str, Any]:
    try:
        markdown, meta = to_markdown(input_path, mime=declared_mime, max_chars=_max_chars(),
                                     pdftotext=pdftotext_bin())
    except DocumentToolError as exc:
        raise MaterialiseError(f"document conversion misconfigured: {exc}", "service_configuration_failure") from exc
    except ConversionError as exc:
        raise MaterialiseError(str(exc), exc.error_class) from exc
    markdown_path.write_text(markdown, encoding="utf-8")
    return meta


def _write_meta(out_dir: Path, conversion: dict[str, Any], *, source_ref: str,
                source_bytes: bytes, markdown: str, declared_mime: str,
                method: str) -> Path:
    """Record the conversion result with TTS acquisition provenance."""
    meta = dict(conversion)
    canonical_mime = _canonical_mime(str(meta["mime_type"]))
    meta.setdefault("backend_attempts", [])
    meta.setdefault("warnings", [])
    meta.update({
        "source_ref": source_ref,
        "source": source_ref,
        "mime_type": canonical_mime,
        "mime": canonical_mime,
        "declared_mime": declared_mime,
        "title": meta.get("title", ""),
        "extraction_method": meta.get("extraction_method", method),
        "method": method,
        "source_sha256": sha256_bytes(source_bytes),
        "markdown_sha256": sha256_bytes(markdown.encode("utf-8")),
        "markdown_chars": len(markdown),
        "char_counts": {"source": len(source_bytes), "markdown": len(markdown)},
        "converter_version": CONVERTER_VERSION,
    })
    path = out_dir / "source.meta.json"
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return path


@tracked_stage("extract_document")
def materialise(source: str | bytes | Path, out_dir: str | Path,
                mime: str | None = None, *, source_ref: str | None = None) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    source_ref = source_ref or str(source if not isinstance(source, bytes) else "bytes")

    if CHATGPT_SHARE_RE.search(source_ref):
        source_bytes, _, _ = _read_source(source)
        markdown_path, conversation_meta = materialise_share(source_ref, out, source_bytes)
        markdown = markdown_path.read_text(encoding="utf-8")
        final = out / "source.md"
        final.write_text(markdown, encoding="utf-8")
        conversation = json.loads(conversation_meta.read_text(encoding="utf-8"))
        side = _write_meta(
            out, {"title": conversation.get("title", ""), "mime_type": "text/html"},
            source_ref=source_ref, source_bytes=source_bytes, markdown=markdown,
            declared_mime="text/html", method="chatgpt-share")
        return {"markdown_path": final, "meta_path": side}

    source_bytes, reference, guessed_mime = _read_source(source)
    declared_mime = mime or guessed_mime
    _reject_unsupported(reference, declared_mime)
    source_path = Path(reference) if not _is_url(reference) and reference != "bytes" else None
    if source_path is not None and source_path.suffix and source_path.is_file():
        input_path = source_path
    else:
        input_path = out / ("input" + _extension_for(reference, declared_mime, source_bytes))
        input_path.write_bytes(source_bytes)
    markdown_path = out / "source.md"
    conversion = _convert(input_path, markdown_path, declared_mime)
    markdown = markdown_path.read_text(encoding="utf-8")
    side = _write_meta(out, conversion, source_ref=reference, source_bytes=source_bytes,
                       markdown=markdown, declared_mime=declared_mime,
                       method=conversion["extraction_method"])
    return {"markdown_path": markdown_path, "meta_path": side}
