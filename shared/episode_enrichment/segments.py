"""Segments: the generic hook that lets a show be enriched in parts (X-76).

A segmenter is ``(stream, timed, context) -> list[Segment]``:

* ``stream`` is the canonical paragraph stream;
* ``timed`` is the parsed ``segments.json`` (``episode_enrichment.timing``), or None without timings;
* ``context`` is whatever the caller knows about the episode (duration, title, window start...).

It returns contiguous segments covering every paragraph once, in order. Topics and mentions then
run per segment. The default, ``single_segment``, is one segment: today's single pass, byte for byte.
Show-specific segmenters (Surveillance's seams) live with their feed, not here.
"""
from __future__ import annotations

from dataclasses import dataclass

from .paragraphs import ParagraphStream, stream_hash


@dataclass(frozen=True)
class Segment:
    label: str                 # the notes section heading; "" for a single segment
    start_paragraph_id: str
    end_paragraph_id: str
    start_s: float | None = None
    end_s: float | None = None
    rule: str = "single"       # how the cut was chosen (the segmenter's own words: single, seam, fallback...)

    def to_json(self) -> dict:
        return {"label": self.label, "startParagraphId": self.start_paragraph_id,
                "endParagraphId": self.end_paragraph_id, "start": self.start_s, "end": self.end_s,
                "rule": self.rule}


def single_segment(stream: ParagraphStream, timed: dict | None, context: dict) -> list[Segment]:
    return [Segment("", stream.first_id(), stream.last_id())]


def check_segments(stream: ParagraphStream, segments: list[Segment]) -> list[str]:
    """Errors when the segments do not cover the stream once, in order (empty when they do)."""
    if not segments:
        return ["the segmenter returned no segments"]
    errors, cursor = [], 0
    for index, segment in enumerate(segments):
        start, end = stream.position(segment.start_paragraph_id), stream.position(segment.end_paragraph_id)
        if start is None or end is None or end < start:
            return [f"segment {index} has an unusable span "
                    f"{segment.start_paragraph_id}..{segment.end_paragraph_id}"]
        if start != cursor:
            errors.append(f"segment {index} starts at {segment.start_paragraph_id}, not at paragraph {cursor + 1}")
        cursor = end + 1
    if cursor != len(stream):
        errors.append(f"segments stop after {cursor} of {len(stream)} paragraphs")
    return errors


def substream(stream: ParagraphStream, segment: Segment) -> ParagraphStream:
    """The segment's paragraphs as a stream of their own, keeping their ids."""
    start, end = stream.position(segment.start_paragraph_id), stream.position(segment.end_paragraph_id)
    paragraphs = stream.paragraphs[start:end + 1]
    return ParagraphStream(paragraphs=paragraphs, stream_sha256=stream_hash([p.text for p in paragraphs]),
                           _index={p.id: pos for pos, p in enumerate(paragraphs)})
