"""Local document-to-markdown conversion for TTS sources.

It covers what TTS jobs send: markdown and plain text, HTML pages, PDFs with a
text layer, and JSON/XML/CSV. Office documents and images are refused as
``unsupported_format``.

PDF text comes from poppler's ``pdftotext`` (reading order, no layout). Its
hard line wraps are joined back into paragraphs, because both the reader
model and the faithful speech path work on paragraphs, not printed lines.
"""

from __future__ import annotations

import codecs
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

CONVERTER_VERSION = "tg-documents-v1"
PDFTOTEXT_TIMEOUT_S = 300
MAIN_MIN_CHARS = 500

TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
FENCED_SUFFIXES = {".json": "json", ".xml": "xml", ".csv": "csv"}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}


class ConversionError(RuntimeError):
    def __init__(self, message: str, error_class: str = "extract_failed") -> None:
        super().__init__(message)
        self.error_class = error_class


def _clean(text: str) -> str:
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def _decode_text(data: bytes) -> str:
    return _clean(data.decode("utf-8-sig", errors="replace"))


# -- HTML ------------------------------------------------------------------------

_HIDDEN = {"script", "style", "noscript", "svg", "iframe", "canvas", "template", "nav", "footer", "aside"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_BLOCK = {"p", "div", "section", "article", "main", "header", "ul", "ol", "table", "tr", "blockquote",
          "pre", "dl", "dt", "dd", "figure", "figcaption", "address", "details", "summary"}
_MAIN = {"main", "article"}
_DESCRIPTION_NAMES = {"description", "og:description", "twitter:description"}


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.hidden: list[str] = []
        self.in_title = False
        self.in_head = False
        self.pre = 0
        self.main_depth = 0
        self.all: list[str] = []
        self.main: list[str] = []

    def _emit(self, text: str) -> None:
        self.all.append(text)
        if self.main_depth:
            self.main.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "head":
            self.in_head = True
        if tag == "meta":
            values = {k.lower(): (v or "") for k, v in attrs}
            name = (values.get("name") or values.get("property") or "").strip().lower()
            if name in _DESCRIPTION_NAMES and not self.description:
                self.description = " ".join(values.get("content", "").split())
            return
        if tag in _VOID:
            if tag in {"br", "hr"} and not self.hidden:
                self._emit("\n")
            return
        if tag == "title":
            self.in_title = True
            return
        if tag in _HIDDEN:
            self.hidden.append(tag)
            return
        if self.hidden:
            return
        if tag in _MAIN:
            self.main_depth += 1
        if tag == "pre":
            self.pre += 1
        if re.fullmatch(r"h[1-6]", tag):
            self._emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._emit("\n- ")
        elif tag in {"td", "th"}:
            self._emit(" | ")
        elif tag in _BLOCK:
            self._emit("\n\n" if tag in {"p", "blockquote", "pre", "table"} else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.in_head = False
        if tag == "title":
            self.in_title = False
            return
        if tag in _HIDDEN and tag in self.hidden:
            del self.hidden[len(self.hidden) - 1 - self.hidden[::-1].index(tag)]
            return
        if self.hidden:
            return
        if tag == "pre" and self.pre:
            self.pre -= 1
        if re.fullmatch(r"h[1-6]", tag) or tag in _BLOCK or tag == "li":
            self._emit("\n\n" if tag in {"p", "blockquote", "pre", "table"} or tag.startswith("h") else "\n")
        if tag in _MAIN and self.main_depth:
            self.main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title = " ".join((self.title + " " + data).split())
            return
        if self.hidden or self.in_head:
            return
        self._emit(data if self.pre else re.sub(r"\s+", " ", data))


def _tidy(text: str) -> str:
    # Cells become "a | b"; empty layout cells, list items and headings leave nothing behind.
    lines = [re.sub(r"^[|\s]+|[|\s]+$", "", re.sub(r"\|(\s*\|)+", "|", line)) for line in text.split("\n")]
    lines = [line for line in lines if not re.fullmatch(r"-|#{1,6}", line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _html_charset(data: bytes, mime: str) -> str:
    candidates = []
    match = re.search(r"charset=([\w.:-]+)", mime or "", re.I)
    if match:
        candidates.append(match.group(1))
    if data.startswith(codecs.BOM_UTF8):
        candidates.insert(0, "utf-8-sig")
    match = re.search(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", data[:4096], re.I)
    if match:
        candidates.append(match.group(1).decode("ascii", "ignore"))
    for name in candidates:
        try:
            codecs.lookup(name)
            return name
        except LookupError:
            continue
    return "utf-8"


def html_to_markdown(data: bytes, mime: str = "") -> tuple[str, str, str]:
    """Return (markdown, title, description): main/article content when it is
    substantial, otherwise the whole visible body; navigation chrome dropped."""
    parser = _HTMLText()
    parser.feed(_clean(data.decode(_html_charset(data, mime), errors="replace")))
    parser.close()
    main = _tidy("".join(parser.main))
    body = main if len(main) >= MAIN_MIN_CHARS else _tidy("".join(parser.all))
    head = []
    if parser.title:
        head += [f"# {parser.title}", ""]
    if parser.description:
        head += [f"> {parser.description}", ""]
    markdown = "\n".join(head + [body]).strip() if body else ""
    return markdown, parser.title, parser.description


# -- PDF -------------------------------------------------------------------------

def unwrap_pdf_text(text: str) -> str:
    """Join pdftotext's printed lines back into paragraphs; blank lines and
    page breaks separate paragraphs, and a line-end hyphen before a lowercase
    letter is a broken word."""
    paragraphs = []
    for block in re.split(r"\n\s*\n|\f", _clean(text)):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        joined = lines[0]
        for line in lines[1:]:
            if joined.endswith("-") and not joined.endswith(" -") and line[:1].islower():
                joined = joined[:-1] + line
            else:
                joined += " " + line
        paragraphs.append(joined)
    return "\n\n".join(paragraphs)


def pdf_to_text(path: Path, pdftotext: str) -> str:
    try:
        completed = subprocess.run([pdftotext, "-enc", "UTF-8", "-q", str(path), "-"],
                                   capture_output=True, timeout=PDFTOTEXT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(f"pdftotext timed out after {PDFTOTEXT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise ConversionError(f"pdftotext failed to run: {exc}", "service_configuration_failure") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[-500:]
        raise ConversionError(f"pdftotext exit {completed.returncode}: {detail or 'no output'}")
    return unwrap_pdf_text(completed.stdout.decode("utf-8", "replace"))


# -- entry point -------------------------------------------------------------------

def to_markdown(path: Path, *, mime: str, max_chars: int, pdftotext: str) -> tuple[str, dict[str, Any]]:
    """Convert one staged source file, routed by its effective suffix."""
    suffix = path.suffix.lower()
    title = description = ""
    if suffix in TEXT_SUFFIXES:
        markdown, canonical, method = _decode_text(path.read_bytes()).strip(), (
            "text/markdown" if suffix != ".txt" else "text/plain"), "native_text"
    elif suffix in FENCED_SUFFIXES:
        language = FENCED_SUFFIXES[suffix]
        body = _decode_text(path.read_bytes()).strip()
        markdown = f"```{language}\n{body}\n```" if body else ""
        canonical = {"json": "application/json", "xml": "application/xml", "csv": "text/csv"}[language]
        method = "native_text"
    elif suffix in HTML_SUFFIXES:
        markdown, title, description = html_to_markdown(path.read_bytes(), mime)
        canonical, method = "text/html", "native_html"
    elif suffix == ".pdf":
        markdown, canonical, method = pdf_to_text(path, pdftotext), "application/pdf", "pdftotext"
    else:
        raise ConversionError(f"unsupported document format: {suffix or 'unknown'}", "unsupported_format")
    if len(markdown) > max_chars:
        raise ConversionError(
            f"extraction is {len(markdown)} chars, over the {max_chars} cap", "extract_truncated")
    if not markdown.strip():
        raise ConversionError("extraction produced no text", "extract_insufficient_text")
    meta = {
        "mime_type": canonical,
        "title": title,
        "description": description,
        "extraction_method": method,
        "extraction_state": "extracted",
        "truncated": False,
        "converter_version": CONVERTER_VERSION,
        # Deterministic: the same bytes must give byte-identical meta, since
        # the publisher uploads it to an immutable key.
        "backend_attempts": [{"backend": method, "status": "ok"}],
        "warnings": [],
    }
    return markdown + "\n", meta
