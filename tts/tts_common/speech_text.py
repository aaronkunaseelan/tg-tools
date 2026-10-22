"""Render faithful artifact Markdown into TTS speech text."""

from __future__ import annotations

import html
import re
from pathlib import Path


def artifact_to_speech_text(artifact: str | Path) -> str:
    raw = str(artifact)
    if "\n" not in raw and len(raw) < 4096 and Path(raw).exists():
        text = Path(raw).read_text(encoding="utf-8")
    else:
        text = raw
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            text = text[end + 4 :]
    text = re.sub(r"```.*?```", "\ncode block omitted\n", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    lines = []
    in_table = False
    for line in text.splitlines():
        if re.match(r"\s*\|.*\|\s*$", line):
            if not in_table:
                lines.append("table omitted")
                in_table = True
            continue
        in_table = False
        if line.lstrip().startswith("#"):
            line = re.sub(r"^#+\s*", "", line).strip()
            numbered = re.match(r"^(\d+(?:\.\d+)*)\.\s*(?:\[[^]]+\]\s*)?(.+)$", line)
            if numbered:
                line = f"Section {numbered.group(1)}. {numbered.group(2)}"
            if line:
                lines.extend(["", line, ""])
            continue
        line = re.sub(r"[*_`~]", "", line)
        lines.append(line)
    out = "\n".join(lines)
    out = html.unescape(out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() + "\n"


def _split_long_paragraph(para: str, max_chars: int) -> list[str]:
    """Split an oversized paragraph at sentence, then word, boundaries.

    A single indivisible token longer than max_chars is hard-split at the
    cap (documented exception; tested)."""
    sentences = re.split(r"(?<=[.!?])\s+", para)
    pieces: list[str] = []
    cur = ""
    for sentence in sentences:
        while len(sentence) > max_chars:
            # Pathological sentence: split at the last word boundary in range.
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            head, sentence = sentence[:cut].strip(), sentence[cut:].strip()
            if cur:
                pieces.append(cur)
                cur = ""
            if head:
                pieces.append(head)
        candidate = (cur + " " + sentence).strip() if cur else sentence
        if len(candidate) <= max_chars:
            cur = candidate
        else:
            pieces.append(cur)
            cur = sentence
    if cur:
        pieces.append(cur)
    return [p for p in pieces if p]


def chunk_speech_text(text: str, max_chars: int = 3000) -> list[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for para in paras:
        if len(para) > max_chars:
            if cur:
                chunks.append(cur.strip())
                cur = ""
            chunks.extend(_split_long_paragraph(para, max_chars))
            continue
        candidate = (cur + "\n\n" + para).strip() if cur else para
        if len(candidate) <= max_chars:
            cur = candidate
        else:
            chunks.append(cur.strip())
            cur = para
    if cur:
        chunks.append(cur.strip())
    return chunks
