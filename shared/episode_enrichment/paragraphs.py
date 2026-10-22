"""Canonical transcript paragraph stream.

The paragraph stream is the fidelity anchor for the whole enrichment
pipeline: models may only *reference* paragraphs by id, never rewrite them.
``stream_sha256`` is re-computed after rendering and must match, which proves
no transcript text was dropped, reordered, or mutated on the way to Markdown.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

_BLANK_LINE_RE = re.compile(r"\n\s*\n")
_WHITESPACE_RE = re.compile(r"\s+")

# Typographic variants Luna reliably echoes back in a different form than the
# transcript carries. Normalising both sides keeps the evidence gate honest
# about *content* rather than about quote styling.
_PUNCT_FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "‒": "-", "―": "-",
    "…": "...", " ": " ",
}


def normalize(text: str) -> str:
    """Fold case, unicode punctuation, and whitespace for substring checks."""
    folded = unicodedata.normalize("NFKC", text or "")
    for source, target in _PUNCT_FOLD.items():
        folded = folded.replace(source, target)
    return _WHITESPACE_RE.sub(" ", folded).strip().lower()


@dataclass(frozen=True)
class Paragraph:
    id: str
    text: str
    words: int


@dataclass(frozen=True)
class ParagraphStream:
    paragraphs: tuple[Paragraph, ...]
    stream_sha256: str
    _index: dict[str, int] = field(repr=False, compare=False, default_factory=dict)

    @property
    def ids(self) -> list[str]:
        return [p.id for p in self.paragraphs]

    @property
    def total_words(self) -> int:
        return sum(p.words for p in self.paragraphs)

    def __len__(self) -> int:
        return len(self.paragraphs)

    def position(self, paragraph_id: str) -> int | None:
        """0-based position, or None when the id is not in the stream."""
        return self._index.get(paragraph_id)

    def text_of(self, paragraph_id: str) -> str | None:
        pos = self._index.get(paragraph_id)
        return self.paragraphs[pos].text if pos is not None else None

    def first_id(self) -> str:
        return self.paragraphs[0].id

    def last_id(self) -> str:
        return self.paragraphs[-1].id

    def to_json(self) -> dict:
        return {
            "count": len(self.paragraphs),
            "totalWords": self.total_words,
            "streamSha256": self.stream_sha256,
            "paragraphs": [
                {"id": p.id, "text": p.text, "words": p.words}
                for p in self.paragraphs
            ],
        }


def stream_hash(texts) -> str:
    """Hash of the canonical paragraph stream.

    Rendering re-joins emitted paragraphs the same way, so an equal hash means
    every paragraph survived intact and in order.
    """
    joined = "\n\n".join(texts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def build_stream(transcript_text: str) -> ParagraphStream:
    """Split formatted transcript text into the canonical paragraph stream."""
    chunks = [
        chunk.strip()
        for chunk in _BLANK_LINE_RE.split(transcript_text or "")
        if chunk.strip()
    ]
    if not chunks:
        raise ValueError("transcript contains no paragraphs")
    paragraphs = tuple(
        Paragraph(id=f"p{index:05d}", text=text, words=len(text.split()))
        for index, text in enumerate(chunks, start=1)
    )
    return ParagraphStream(
        paragraphs=paragraphs,
        stream_sha256=stream_hash([p.text for p in paragraphs]),
        _index={p.id: pos for pos, p in enumerate(paragraphs)},
    )


def stream_from_json(payload: dict) -> ParagraphStream:
    """Rebuild a stream from ``to_json`` output (checkpoint resume)."""
    paragraphs = tuple(
        Paragraph(id=str(item["id"]), text=str(item["text"]),
                  words=int(item.get("words") or len(str(item["text"]).split())))
        for item in payload["paragraphs"]
    )
    return ParagraphStream(
        paragraphs=paragraphs,
        stream_sha256=str(payload["streamSha256"]),
        _index={p.id: pos for pos, p in enumerate(paragraphs)},
    )
