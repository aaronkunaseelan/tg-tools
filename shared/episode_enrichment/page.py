"""One readable HTML page per episode: its notes, then its script (``<prefix>[.v<n>].page.html``).

The page is built from the notes *markdown* and the script, not from the analysis structure: episodes
that already have notes keep only ``notes.md``, so one definition serves the notes job and the
no-model page path alike. ``render.render_notes_markdown`` emits a fixed subset, and this converts
exactly that: ``#``/``##``/``###`` headings, paragraphs, ``- `` items with one nested ``  - `` level,
``**bold**`` and ``[text](url)`` links. Every paragraph reference (``p00012``) links to the script
paragraph with that id; a paragraph shows its start time when the timed transcript belongs to the
script.

Deterministic bytes (no clock, fixed CSS): the same notes and script always give the same page.
The page is derived, so the notes job uploads it allowing overwrite. Stdlib only. All text is HTML-escaped; only http(s) links are kept. The page
needs no JavaScript and no external resource (rss-edge serves it under CSP ``sandbox``): inline CSS,
system fonts, links only.
"""
from __future__ import annotations

import html
import json
import re

from .paragraphs import build_stream

# The page job key carries this, so a markup change can build every page again (bump it with the markup).
PAGE_LAYOUT = 2

CSS = (
    "body{font:17px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:46rem;"
    "margin:2rem auto;padding:0 1rem;color:#1d1d1f;background:#fff}"
    "h1{font-size:1.6rem;line-height:1.25}h2{margin-top:2.2rem;border-bottom:1px solid #ddd}"
    "h3{margin-top:1.6rem}a{color:#0b57d0}li{margin:.25rem 0}"
    ".script p{margin:.7rem 0}.t{font:13px ui-monospace,monospace;color:#666;margin-right:.5rem;"
    "text-decoration:none}"
    "@media (prefers-color-scheme:dark){body{background:#111;color:#e6e6e6}a{color:#8ab4f8}h2{border-color:#333}}"
)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_REF = re.compile(r"(?<![\w/#.-])(p\d{5})(?![\w-])")


def _inline(text: str) -> str:
    """One line of the notes subset as HTML: escaped, then links, bold and paragraph references."""
    out, last = [], 0
    for match in _LINK.finditer(text):
        out.append(_plain(text[last:match.start()]))
        label, url = match.group(1), match.group(2)
        if url.startswith(("https://", "http://")):
            out.append(f'<a href="{html.escape(url, quote=True)}">{_plain(label)}</a>')
        else:
            out.append(_plain(match.group(0)))
        last = match.end()
    out.append(_plain(text[last:]))
    return "".join(out)


def _plain(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    return _REF.sub(r'<a href="#\1">\1</a>', escaped)


def notes_html(markdown: str) -> str:
    """The notes subset as HTML block elements."""
    out: list[str] = []
    paragraph: list[str] = []
    depth = 0   # open list levels

    def close_lists(to: int = 0) -> None:
        nonlocal depth
        while depth > to:
            out.append("</li></ul>")
            depth -= 1

    def flush() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    for line in markdown.splitlines():
        heading = re.match(r"^(#{1,3}) (.*)$", line)
        item = re.match(r"^( *)- (.*)$", line)
        if not line.strip():
            flush()
            close_lists()
        elif heading:
            flush()
            close_lists()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif item:
            flush()
            level = min(len(item.group(1)) // 2 + 1, 2)
            if level > depth:
                while depth < level:
                    out.append("<ul><li>")
                    depth += 1
            else:
                close_lists(level)
                out.append("</li><li>")
            out.append(_inline(item.group(2)))
        else:
            close_lists()
            paragraph.append(line.strip())
    flush()
    close_lists()
    return "\n".join(out).replace("<ul><li>\n", "<ul><li>").replace("</li><li>\n", "</li><li>")


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def script_html(script_text: str, timed: str | dict | None = None) -> str:
    """The script's paragraphs, each with its id (and its start time when the timings are its own)."""
    stream = build_stream(script_text)
    starts: dict[str, float] = {}
    data = json.loads(timed) if isinstance(timed, str) else timed
    if data and len(data.get("paragraphs") or []) == len(stream.paragraphs):
        starts = {p["id"]: float(p["start"]) for p in data["paragraphs"]}
    rows = []
    for paragraph in stream.paragraphs:
        start = starts.get(paragraph.id)
        stamp = (f'<a class="t" href="#{paragraph.id}">{_clock(start)}</a>' if start is not None
                 else f'<a class="t" href="#{paragraph.id}">{paragraph.id}</a>')
        rows.append(f'<p id="{paragraph.id}">{stamp}{html.escape(paragraph.text, quote=False)}</p>')
    return "\n".join(rows)


def render_page(*, notes_markdown: str, script_text: str, timed: str | dict | None = None) -> str:
    """The episode page: the notes, then the script. Deterministic bytes."""
    first = next((line[2:].strip() for line in notes_markdown.splitlines() if line.startswith("# ")), "Episode")
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(first, quote=False)}</title><style>{CSS}</style></head><body>\n"
        # One article: a reader's readability stage keeps a single article, so notes and script as
        # siblings lost the script (the reading lane's reader kept 15,873 of 45,570 words, 4 Nov).
        f'<main><article><section class="notes">\n{notes_html(notes_markdown)}\n</section>\n'
        f'<section class="script"><h2 id="script">Script</h2>\n{script_html(script_text, timed)}\n</section></article></main>\n'
        "</body></html>\n")
