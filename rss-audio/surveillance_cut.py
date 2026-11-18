"""Cut the condensed edition out of the published mp3: paragraph-aligned intervals, ``ffmpeg -c copy``.

The cut makes no model call. It keeps the ``interview`` and ``panel`` blocks of a structure document
(``surveillance_structure``), moves each block's head to the conversation itself, merges runs a couple
of paragraphs apart, and writes the audio plus its derived transcript, segments and plan. The
work owns the files and the upload (SV-condensed packet 3); this module owns the arithmetic.

Four things the port put right, from the pilot harness (``work/SV-condensed``, X-104):

* the merge never moves an interval's end backwards (D4): a block that nests inside the previous one
  extends nothing and loses nothing, where the pilot silently dropped audio and kept the seek line;
* one interval to paragraph mapping, ``intervals``, checked per interval rather than in total (D3),
  and it is what the timed transcript is built from — not a ``zip`` that truncates (D5);
* a cut whose measured duration misses its plan is a failure (``CutError``, D16), not a warning line;
* a plan-only plan says so and cannot be published (``PlanError``, D15).

Replay: the four stored plans of 4-5 Nov reproduce from their structure documents and inputs
(``tests/test_surveillance_cut.py``); ``drift`` is the ``--reconcile`` check, as a test.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import config
from episode_enrichment.paragraphs import build_stream
from episode_enrichment.timing import SCHEMA

import surveillance_structure as structure

PRE, TAIL = 0.4, 0.3      # pad so no first or last word is clipped
MERGE_GAP_PARAGRAPHS = 2  # a block starting this soon after the previous end joins the run (one between)
DURATION_TOLERANCE_S = 2.0   # plan phase 5: the audio's duration is within 2 s of the interval sum
DRIFT_S = 0.5             # tolerance for the plan's computed starts against the measured file
PLAN_SCHEMA = "tg-tools/condensed-plan/1"


class CutError(RuntimeError):
    """The cut could not be trusted: its audio misses its plan, or its paragraphs do not map."""

    error_class = "cut"


class IntervalMapError(CutError):
    """The condensed stream does not carry the paragraphs the plan says interval by interval."""


class PlanError(RuntimeError):
    """A plan that may not be published: plan-only, or with no measured duration (D15)."""

    error_class = "plan"


def plan(inp: dict, doc: dict) -> list[dict]:
    """Kept intervals on paragraph boundaries, previous guests' sign-offs trimmed, adjacent runs merged.

    A merge extends an interval's end and never moves it backwards (D4): a block that nests inside the
    previous one leaves both blocks' heads (so the seek list keeps its topic) and the audio unchanged.
    Each interval records how its head was found (``how``) and how many paragraphs the sign-off trim
    moved it past the handoff (``trimmed``, D19).

    The merge distance is the pilot's ``lo - hi <= 2``, kept as published: that is one skipped
    paragraph between two runs, though the pilot's own comment claimed two. The four stored plans depend
    on it, so the number is the evidence's, not the comment's.
    """
    paras = inp["paragraphs"]
    index = {p["id"]: i for i, p in enumerate(paras)}
    others = structure.known_guests(inp, doc)
    spans = []
    for b in structure.kept(doc["blocks"]):
        lo, hi = index[b["startParagraphId"]], index[b["endParagraphId"]]
        start, how, trimmed = structure.start_index(paras, b, lo, hi, index, others)
        spans.append({"lo": start, "hi": hi, "block": b, "how": how, "trimmed": trimmed})
    if not spans:
        raise CutError("the structure keeps no interview or panel: there is no condensed edition to cut")
    merged: list[dict] = []
    for s in spans:
        if merged and s["lo"] - merged[-1]["hi"] <= MERGE_GAP_PARAGRAPHS:      # one run, not two
            merged[-1]["hi"] = max(merged[-1]["hi"], s["hi"])                 # never backwards (D4)
            merged[-1]["blocks"].append(s["block"])
            merged[-1]["how"].append(s["how"])
            merged[-1]["trimmed"].append(s["trimmed"])
        else:
            merged.append({"lo": s["lo"], "hi": s["hi"], "blocks": [s["block"]],
                           "how": [s["how"]], "trimmed": [s["trimmed"]]})
    out, cursor = [], 0.0
    for m in merged:
        first, last = paras[m["lo"]], paras[m["hi"]]
        audio_start, audio_end = max(0.0, first["t"] - PRE), last["end"] + TAIL
        out.append({**m, "audio_start": audio_start, "audio_end": audio_end,
                    "shift": audio_start - cursor,
                    "seconds": round(audio_end - audio_start, 2),
                    "guests": [b.get("guest") for b in m["blocks"] if b.get("guest")],
                    "heads": [index[b["startParagraphId"]] for b in m["blocks"]]})
        cursor += audio_end - audio_start
    return out


def intervals(plan_intervals: list[dict], stream, paragraphs: list[dict] | None = None) -> list[dict]:
    """The one interval to paragraph mapping: interval *i* owns the stream's next ``hi-lo+1`` paragraphs.

    Checked per interval and not only in total (D3): the condensed stream must re-split into exactly
    the paragraphs each interval keeps, and the intervals must run in one order without overlapping. A
    paragraph that splits in two, a merge across an interval boundary and a stream shorter than the plan
    all fail here, by interval, before anything is written (D5).

    ``paragraphs`` is the show's own paragraph list: with it, each condensed paragraph is also checked
    against the source paragraph it must be, so a re-split inside an interval fails too. A caller that
    holds only the cut (the notes job) passes none: the counts and the order are still checked.
    """
    at, previous_hi, out = 0, None, []
    for i, it in enumerate(plan_intervals):
        lo, hi = int(it["lo"]), int(it["hi"])
        if hi < lo:
            raise IntervalMapError(f"interval {i}: ends at {hi} before it starts at {lo}")
        if previous_hi is not None and lo <= previous_hi:
            raise IntervalMapError(f"interval {i}: starts at paragraph {lo}, inside the previous interval "
                                   f"(which ends at {previous_hi})")
        previous_hi = hi
        count = hi - lo + 1
        if at + count > len(stream.paragraphs):
            raise IntervalMapError(f"interval {i}: {len(stream.paragraphs) - at} of {count} paragraphs "
                                   f"left in the condensed stream")
        for k in range(count):
            if paragraphs is None:
                break
            para, source = stream.paragraphs[at + k], paragraphs[lo + k]
            if para.text != source["text"]:
                raise IntervalMapError(f"interval {i}: condensed paragraph {para.id} is {source['id']} in the "
                                       f"source, so the stream re-split inside the interval")
        out.append({"lo": lo, "hi": hi, "start": at, "end": at + count - 1,
                    **{key: it[key] for key in ("guests", "heads", "how", "trimmed") if key in it}})
        at += count
    if at != len(stream.paragraphs):
        raise IntervalMapError(f"the intervals keep {at} paragraphs, the condensed stream has "
                               f"{len(stream.paragraphs)}")
    return out


def timeline(inp: dict, plan_intervals: list[dict]) -> tuple[list[str], list[float]]:
    """Kept paragraph texts and their starts inside the condensed file: ``t - shift``."""
    paras = inp["paragraphs"]
    kept, starts = [], []
    for it in plan_intervals:
        for p in paras[it["lo"]:it["hi"] + 1]:
            kept.append(p["text"])
            starts.append(round(p["t"] - it["shift"], 2))
    return kept, starts


def rows(starts: list[float], last_end: float) -> list[dict]:
    """Paragraph timing: each end is the next start - 0.01, the last one the end of the condensed file."""
    return [{"start": s, "end": round(starts[i + 1] - 0.01, 2) if i + 1 < len(starts) else round(last_end, 2)}
            for i, s in enumerate(starts)]


def condensed_transcript(inp: dict, plan_intervals: list[dict], last_end: float,
                         segments: list[dict] | None = None) -> dict:
    """The script, its checked interval mapping and the timed transcript of the condensed file.

    The paragraph rows come from the checked mapping, so the transcript cannot have fewer paragraphs
    than the script (D5) and every interval's own start is known to the caller (D3).
    """
    kept, starts = timeline(inp, plan_intervals)
    script = "\n\n".join(kept) + "\n"
    stream = build_stream(script)
    mapping = intervals(plan_intervals, stream, inp["paragraphs"])
    art = {"schema": SCHEMA, "basis": "words",
           "scriptSha256": hashlib.sha256(script.encode()).hexdigest(),
           "streamSha256": stream.stream_sha256,
           "paragraphs": [{"id": p.id, **row} for p, row in zip(stream.paragraphs, rows(starts, last_end))]}
    if segments is not None:
        art["segments"] = segments
    return {"script": script, "stream": stream, "mapping": mapping, "transcript": art}


def plan_document(plan_intervals: list[dict], structure_name: str, paragraphs: int, segments: int,
                  measured: float | None) -> dict:
    """The plan as the work publishes it: the intervals, the planned and the measured length."""
    return {"schema": PLAN_SCHEMA, "structure": structure_name,
            "intervals": [{k: v for k, v in it.items() if k != "blocks"} for it in plan_intervals],
            "plannedSeconds": round(sum(it["seconds"] for it in plan_intervals), 1),
            "measuredSeconds": None if measured is None else round(measured, 1),
            "paragraphs": paragraphs, "segments": segments, "planOnly": measured is None}


def publishable(plan_document_: dict) -> None:
    """Refuse a plan that may not be published: a plan-only plan has no audio and no true length (D15)."""
    if plan_document_.get("planOnly") or plan_document_.get("measuredSeconds") is None:
        raise PlanError("plan-only: no measured duration, so there is no audio and no true length to publish")


def duration_gate(measured: float, planned: float) -> None:
    """A cut that misses its plan is a failure (D16), not a warning line and an exit 0."""
    if abs(measured - planned) > DURATION_TOLERANCE_S:
        raise CutError(f"the cut measured {measured:.1f}s against a planned {planned:.1f}s "
                       f"(tolerance {DURATION_TOLERANCE_S:.1f}s)")


def drift(computed: list[dict], measured: list[dict]) -> float:
    """The largest paragraph-start disagreement between a computed timeline and the measured file.

    The pilot's ``--reconcile`` was a hand command nothing on the path ran (D18); it is this one
    comparison, asserted over the four stored shows in the tests.
    """
    if len(computed) != len(measured):
        raise CutError(f"{len(computed)} computed paragraphs against {len(measured)} measured")
    return max((abs(a["start"] - b["start"]) for a, b in zip(computed, measured)), default=0.0)


def media_bins() -> tuple[str, str]:
    """(ffmpeg, ffprobe) as rss-audio's settings name them: ``config.Settings.ffmpeg_bin`` / ``ffprobe_bin``.

    The cut is handed its work and no settings by its job, so the two binaries are read through
    ``config.load_settings``, the one settings definition every other rss-audio media step is given: a
    deployment that points ``RSS_AUDIO_FFMPEG_BIN`` at a binary off the unit's PATH cuts with it too (C5).
    """
    settings = config.load_settings()
    return settings.ffmpeg_bin, settings.ffprobe_bin


def ffmpeg_cut(source: Path, plan_intervals: list[dict], work: Path, *, ffmpeg_bin: str | None = None) -> Path:
    """Each interval copied without re-encoding, then concatenated, with the configured ffmpeg (C5)."""
    ffmpeg = ffmpeg_bin or media_bins()[0]
    work.mkdir(parents=True, exist_ok=True)
    parts = []
    for i, it in enumerate(plan_intervals, 1):
        part = work / f"part{i:03d}.mp3"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{it['audio_start']:.3f}", "-to", f"{it['audio_end']:.3f}",
                        "-i", str(source), "-c", "copy", str(part)], check=True)
        parts.append(part)
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    out = work / "condensed.mp3"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-c", "copy", str(out)], check=True)
    return out


def duration(path: Path, *, ffprobe_bin: str | None = None) -> float:
    """The file's measured length in seconds, with the configured ffprobe (C5)."""
    ffprobe = ffprobe_bin or media_bins()[1]
    probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=True)
    return float(probe.stdout.strip())


def parent_segments(inp: dict, plan_intervals: list[dict], parent: dict | None) -> list[dict]:
    """The parent's timed segments inside the kept paragraphs, moved onto the condensed clock."""
    if not parent:
        return []
    paras = inp["paragraphs"]
    out = []
    for it in plan_intervals:
        for s in parent.get("segments") or []:
            if s["start"] >= paras[it["lo"]]["t"] - 0.01 and s["end"] <= paras[it["hi"]]["end"] + 0.01:
                out.append({"start": round(s["start"] - it["shift"], 2),
                            "end": round(s["end"] - it["shift"], 2), "text": s["text"]})
    return out


def cut(source: Path, inp: dict, doc: dict, work: Path, *, structure_name: str,
        parent: dict | None = None) -> dict:
    """The measured cut: the mp3, its derived transcript, its segments and its plan document.

    Raises ``CutError`` when the audio misses the plan by more than ``DURATION_TOLERANCE_S``, so a
    plan and an audio file that disagree can never reach the feed (D16).
    """
    plan_intervals = plan(inp, doc)
    audio = ffmpeg_cut(source, plan_intervals, Path(work))
    measured = duration(audio)
    planned = round(sum(it["seconds"] for it in plan_intervals), 1)
    duration_gate(measured, planned)
    segments = parent_segments(inp, plan_intervals, parent)
    derived = condensed_transcript(inp, plan_intervals, measured, segments)
    return {"audio": audio, "measured": measured, "planned": planned, "intervals": plan_intervals,
            **derived,
            "plan": plan_document(plan_intervals, structure_name, len(derived["stream"]), len(segments),
                                  measured)}


def plan_only(inp: dict, doc: dict, *, structure_name: str) -> dict:
    """What the cut would keep, and how long, with no download and no audio: not publishable (D15)."""
    plan_intervals = plan(inp, doc)
    total = round(sum(it["seconds"] for it in plan_intervals), 2)
    derived = condensed_transcript(inp, plan_intervals, total)
    return {"intervals": plan_intervals, **derived,
            "plan": plan_document(plan_intervals, structure_name, len(derived["stream"]), 0, None)}
