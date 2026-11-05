"""Timed transcript: when each canonical paragraph was spoken, published beside script.md as
``<prefix>.segments.json``. Additive: script.md and the paragraph stream are unchanged.

Speech-to-text hands over ``timing``: a basis and timed units in source order,
``{"basis": "words" | "segments" | "chunks", "units": [{start, end, text}], "segments": [...]}``.

* ``words``: m3-asr's word timings (FluidAudio); paragraph times are exact to the word.
* ``segments``: a server that sends segments but no words; times are per segment.
* ``chunks``: the Azure fallback, which knows only where each 10-minute chunk starts; a paragraph's
  time is its chunk's span, so it can be off by up to a chunk.

Or ``{"basis": None, "reason": ...}`` when there are no timings. Unknown is never a guess: no
artifact is written, and the manifest says why.

Units map onto paragraphs by their non-whitespace characters. ``format_transcript`` only changes
whitespace, so the two sequences are identical; when they are not, the timing is refused.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re

from .paragraphs import build_stream

SCHEMA = "tg-tools/timed-transcript/1"
_WHITESPACE = re.compile(r"\s+")


def _chars(text: str) -> int:
    return len(_WHITESPACE.sub("", text or ""))


def no_timing(reason: str) -> dict:
    return {"basis": None, "reason": reason}


def from_self_hosted(timings: dict | None) -> dict:
    """``timing`` from transcribe.transcribe_self_hosted's third element."""
    words = (timings or {}).get("words")
    segments = (timings or {}).get("segments")
    if words:
        units = [{"start": w["start"], "end": w["end"], "text": w["word"]} for w in words]
        return {"basis": "words", "units": units, "segments": segments or units}
    if segments:
        return {"basis": "segments", "units": segments, "segments": segments}
    return no_timing("the self-hosted server sent no timings")


def from_chunks(parts: list[str], chunk_seconds: float, duration: float | None) -> dict:
    """``timing`` from the Azure chunk texts: chunk i covers [i * chunk_seconds, (i + 1) * chunk_seconds)."""
    units = []
    for index, text in enumerate(parts):
        start = index * chunk_seconds
        end = start + chunk_seconds
        if duration and index == len(parts) - 1:
            end = min(end, duration)
        units.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    return {"basis": "chunks", "units": units, "segments": units}


def build(script_text: str, timing: dict | None) -> tuple[str | None, dict]:
    """(segments.json text or None, the manifest's ``timing`` summary)."""
    if not timing or not timing.get("basis"):
        return None, no_timing((timing or {}).get("reason") or "no timings")
    units = timing.get("units") or []
    ends, total = [], 0
    for unit in units:
        total += _chars(unit.get("text"))
        ends.append(total)
    stream = build_stream(script_text)
    script_chars = sum(_chars(p.text) for p in stream.paragraphs)
    if not units or total != script_chars:
        return None, no_timing(f"timings do not match the script ({total} timed characters, "
                               f"{script_chars} in the script)")
    paragraphs, offset = [], 0
    for paragraph in stream.paragraphs:
        first, last = offset, offset + _chars(paragraph.text) - 1
        offset = last + 1
        paragraphs.append({"id": paragraph.id,
                           "start": units[bisect.bisect_right(ends, first)]["start"],
                           "end": units[bisect.bisect_right(ends, last)]["end"]})
    artifact = {
        "schema": SCHEMA,
        "basis": timing["basis"],
        "scriptSha256": hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
        "streamSha256": stream.stream_sha256,
        "paragraphs": paragraphs,
        "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]}
                     for s in timing.get("segments") or units],
    }
    # Deterministic bytes: a retry re-uploads an identical object, which the immutable upload allows.
    text = json.dumps(artifact, ensure_ascii=False, indent=1) + "\n"
    return text, {"basis": timing["basis"], "paragraphs": len(paragraphs), "segments": len(artifact["segments"])}
