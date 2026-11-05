"""Bloomberg Surveillance seams (X-76): split a 3-hour show into its hours at the broadcast's own opens.

Surveillance only. The notes job reaches this through ``SEGMENTERS`` (the generic segmenter hook,
``(stream, timed, context) -> list[Segment]``). The rules are the accepted seam study
(supervisor ``work/SV/SEAMS.md``, 4 Nov); every number below comes from it.

* **Marks.** Radio: each wall-clock hour of the window (3600 s, 7200 s). TV (the ads-cut VOD): each
  third of the file.
* **Open.** Radio: "this is bloomberg surveillance with", confirmed by "bloomberg originals" within
  60 s after (the same line also re-introduces segments at :24 and :54). TV: "this is bloomberg
  surveillance" followed by "with", or by "audience worldwide" / "hour of" / "surveillance hour"
  within 30 s (a bare line also closes segments before breaks).
* **Seam (rule "seam").** The confirmed open nearest the mark, within 10 minutes. The reset starts at
  the end of the last silence of 5 s or more in the 90 s before the open: the cold-open montage on TV,
  the open itself on radio. The cut is the paragraph boundary nearest the reset start.
* **Fallback (rule "fallback").** No confirmed open near the mark: the paragraph boundary nearest it.
* **Estimated (rule "estimated").** No timings: the paragraph at the mark's share of the script's
  characters.

Times come from ``segments.json``'s timed segments; a phrase's time is interpolated inside its segment.
Labels: radio ``Hour 2 · 08:00–09:00 EST`` (the window's wall clock, from ``segmentContext.start`` and
``.timezone``); TV ``Hour 2 · 0:48–1:36`` (file time).
"""
from __future__ import annotations

import bisect
import re
from datetime import datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from episode_enrichment import Segment

WINDOW_S = 600      # a seam is within 10 min of its mark
GAP_S = 5.0         # a silence that starts a reset
LOOKBACK_S = 90     # how far before the open the reset may start
HOUR_S = 3600
OPEN = "this is bloomberg surveillance"
RADIO_CONFIRM, RADIO_CONFIRM_S = re.compile(r"bloomberg originals"), 60
TV_CONFIRM, TV_CONFIRM_S = re.compile(r"audience worldwide|hour of|surveillance hour"), 30


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


class _Timeline:
    """The timed segments as one normalised text, with a time for every character."""

    def __init__(self, segments: list[Mapping[str, Any]]):
        self.segments = [s for s in segments if s.get("start") is not None]
        parts, self.offsets = [], []
        position = 0
        for segment in self.segments:
            text = _norm(str(segment.get("text") or ""))
            self.offsets.append(position)
            parts.append(text)
            position += len(text) + 1
        self.text = " ".join(parts)

    def time_at(self, char: int) -> float:
        i = max(bisect.bisect_right(self.offsets, char) - 1, 0)
        segment, start = self.segments[i], self.offsets[i]
        length = (self.offsets[i + 1] - 1 if i + 1 < len(self.offsets) else len(self.text)) - start
        share = min(max((char - start) / length, 0.0), 1.0) if length > 0 else 0.0
        return float(segment["start"]) + share * (float(segment["end"]) - float(segment["start"]))

    def times(self, pattern: re.Pattern[str]) -> list[tuple[float, int]]:
        return [(self.time_at(m.start()), m.end()) for m in pattern.finditer(self.text)]

    def reset(self, open_s: float) -> float:
        """The start of the speech after the last 5 s silence in the 90 s before the open."""
        reset = open_s
        for before, after in zip(self.segments, self.segments[1:]):
            start = float(after["start"])
            if start > open_s:
                break
            if start >= open_s - LOOKBACK_S and start - float(before["end"]) >= GAP_S:
                reset = start
        return reset


def _opens(timeline: _Timeline, show: str) -> list[float]:
    """Times of the confirmed hour opens."""
    if show == "radio":
        confirms = [t for t, _ in timeline.times(RADIO_CONFIRM)]
        return [t for t, _ in timeline.times(re.compile(OPEN + " with"))
                if any(0 < c - t <= RADIO_CONFIRM_S for c in confirms)]
    confirms = [t for t, _ in timeline.times(TV_CONFIRM)]
    return [t for t, end in timeline.times(re.compile(OPEN))
            if timeline.text[end:end + 5] == " with" or any(0 <= c - t <= TV_CONFIRM_S for c in confirms)]


def _marks(show: str, duration: float) -> list[float]:
    if show == "radio":
        return [HOUR_S * k for k in range(1, max(round(duration / HOUR_S), 1))]
    return [duration * k / 3 for k in (1, 2)]


def _cuts(stream, timed: Mapping[str, Any] | None, show: str, duration: float | None) -> list[tuple[int, str]]:
    """(index of the paragraph that starts each later hour, rule), in order."""
    paragraphs = stream.paragraphs
    if timed and timed.get("paragraphs") and len(timed["paragraphs"]) == len(paragraphs):
        starts = [float(p["start"]) for p in timed["paragraphs"]]
        duration = float(duration or timed["paragraphs"][-1]["end"])
        timeline = _Timeline(timed.get("segments") or [])
        opens = _opens(timeline, show) if timeline.segments else []
        nearest = lambda t: min(range(1, len(starts)), key=lambda i: abs(starts[i] - t))
        cuts = []
        for mark in _marks(show, duration):
            near = [t for t in opens if abs(t - mark) <= WINDOW_S]
            if near:
                cuts.append((nearest(timeline.reset(min(near, key=lambda t: abs(t - mark)))), "seam"))
            else:
                cuts.append((nearest(mark), "fallback"))
    else:
        sizes = [len(p.text) for p in paragraphs]
        total, ends = sum(sizes), []
        for size in sizes:
            ends.append((ends[-1] if ends else 0) + size)
        shares = [m / duration for m in _marks(show, duration)] if duration else [k / 3 for k in (1, 2)]
        cuts = [(min(range(1, len(paragraphs)), key=lambda i: abs(ends[i - 1] - share * total)), "estimated")
                for share in shares]
    kept, last = [], 0
    for index, rule in cuts:   # two marks on one paragraph keep the first: never an empty hour
        if index > last:
            kept.append((index, rule))
            last = index
    return kept


def _clock(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}:{minutes % 60:02d}"


def _labels(show: str, bounds: list[tuple[float | None, float | None]], context: Mapping[str, Any]) -> list[str]:
    start, zone = context.get("start"), context.get("timezone")
    labels = []
    for hour, (begin, end) in enumerate(bounds, 1):
        if show == "radio" and start and zone:
            local = datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone(ZoneInfo(str(zone)))
            first, second = local + timedelta(hours=hour - 1), local + timedelta(hours=hour)
            labels.append(f"Hour {hour} · {first:%H:%M}–{second:%H:%M} {first.tzname()}")
        elif begin is not None and end is not None:
            labels.append(f"Hour {hour} · {_clock(begin)}–{_clock(end)}")
        else:
            labels.append(f"Hour {hour}")
    return labels


def segmenter(show: str):
    def split(stream, timed: Mapping[str, Any] | None, context: Mapping[str, Any]) -> list[Segment]:
        paragraphs = stream.paragraphs
        if len(paragraphs) < 3:
            return [Segment("", paragraphs[0].id, paragraphs[-1].id, rule="single")]
        cuts = _cuts(stream, timed, show, context.get("durationSeconds"))
        timed_paragraphs = (timed or {}).get("paragraphs") if timed and len(timed.get("paragraphs") or []) == len(
            paragraphs) else None
        if not cuts:
            return [Segment("", paragraphs[0].id, paragraphs[-1].id, rule="single")]
        firsts, rules = [0] + [i for i, _ in cuts], ["start"] + [r for _, r in cuts]   # the rule that began each hour
        lasts = [i - 1 for i, _ in cuts] + [len(paragraphs) - 1]
        bounds = [(float(timed_paragraphs[a]["start"]), float(timed_paragraphs[b]["end"])) if timed_paragraphs
                  else (None, None) for a, b in zip(firsts, lasts)]
        # a label runs to the next hour's start, so the hours read contiguous; end_s stays the speech's end
        labels = _labels(show, [(b, bounds[i + 1][0] if i + 1 < len(bounds) else e)
                                for i, (b, e) in enumerate(bounds)], context)
        return [Segment(label, paragraphs[a].id, paragraphs[b].id, start_s=begin, end_s=end, rule=rule)
                for label, a, b, (begin, end), rule in zip(labels, firsts, lasts, bounds, rules)]
    return split


SEGMENTERS = {"surveillance-radio": segmenter("radio"), "surveillance-tv": segmenter("tv")}
