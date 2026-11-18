"""Script-first episode enrichment with durable checkpoints."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from tg_tools_common.progress import current_progress, tracked_stage

from . import render, timing as timing_mod
from .luna import (EnrichmentModelError, MENTIONS_ROLE, TOPICS_ROLE, mention_batches, run_mentions,
                   run_topics)
from .paragraphs import ParagraphStream, build_stream, stream_from_json
from .prompts import clean_source_description
from .segments import Segment, check_segments, single_segment, substream
from .source import AudioSttSource, TranscriptSource
from .speakers import speaker_entry, speaker_intervals
from .stt import DEFAULT_STT_MODEL, TranscriptionError
from .validate import notes_gates, validate_topics

logger = logging.getLogger(__name__)

SCRIPT_FILE = "script.md"
SCRIPT_HASH_FILE = "script.sha256"
# The self-hosted engine that wrote script.md, so a resumed job still records it.
STT_ENGINE_FILE = "stt-engine.txt"
# What speech-to-text said about when words were spoken (episode_enrichment.timing), so a resumed job
# still publishes the timed transcript, and that artifact itself (uploaded as <prefix>.segments.json).
TIMING_FILE = "timing.json"
TIMED_FILE = "segments.json"
PARAGRAPHS_FILE = "paragraphs.json"
ANALYSIS_FILE = "analysis.json"
MENTIONS_FILE = "mentions.json"
NOTES_FILE = "notes.md"
MANIFEST_FILE = "manifest.json"
REPORT_FILE = "gates.json"
# The notes document a caller asks for. "sections" is an rss episode's notes (X-104); "topics" is the
# summary + spans + references document the reader pipelines (TTS, transcribe-audio) read.
NOTES_SHAPE_TOPICS = "topics"
NOTES_SHAPE_SECTIONS = "sections"
NOTES_SHAPES = (NOTES_SHAPE_TOPICS, NOTES_SHAPE_SECTIONS)
_DEPENDENT_CHECKPOINTS = (PARAGRAPHS_FILE, ANALYSIS_FILE, MENTIONS_FILE,
                          NOTES_FILE, MANIFEST_FILE, REPORT_FILE)


class EnrichmentError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class PreparedScript:
    text: str
    path: Path
    sha256: str
    source: TranscriptSource
    stt_engine: str | None = None
    timed_transcript: str | None = None   # segments.json text, None without timings
    timing: dict = field(default_factory=lambda: timing_mod.no_timing("not speech-to-text"))  # manifest summary


@dataclass
class EnrichmentResult:
    script_markdown: str
    notes_markdown: str
    manifest: str
    description: str
    stream: ParagraphStream
    analysis: dict
    mentions: dict
    reports: dict = field(default_factory=dict)
    timed_transcript: str | None = None

    @property
    def topic_count(self) -> int:
        return len(self.analysis.get("topics") or [])

    @property
    def mention_count(self) -> int:
        return sum(len(topic.get("mentions") or [])
                   for topic in self.mentions.get("topics") or [])


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_reports(work: Path, reports: dict) -> None:
    """The run's reports beside the job (gates.json, the one reports file): every stage's counts and notes, the
    mention batches included; a failed run writes what it has (F-56)."""
    _atomic_write(work / REPORT_FILE, json.dumps(reports, ensure_ascii=False, indent=1))


def _record_reports(reports: dict, parts: list, segmentwise: bool) -> None:
    """The topics and mentions reports the run holds so far, into ``reports``."""
    if segmentwise:
        reports["segments"] = [{"label": seg.label, "topics": topics, "mentions": mentions}
                               for seg, _, _, topics, mentions in parts]
    else:
        reports["topics"], reports["mentions"] = parts[0][3], parts[0][4]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("discarding unreadable checkpoint %s", path)
        return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _invalidate_dependents(work_dir: Path) -> None:
    for name in _DEPENDENT_CHECKPOINTS:
        (work_dir / name).unlink(missing_ok=True)
    for pattern in ("topics-*.json", "mentions-*.json"):   # per-segment and per-batch call caches
        for path in work_dir.glob(pattern):
            path.unlink(missing_ok=True)


def checkpointed_script(work_dir: str | Path) -> str | None:
    """The script a work dir holds when its checkpoint is whole (script.md matches script.sha256), else None.

    A failed enrichment still publishes this script: the transcript cost a paid or slow STT run, and the
    work dir is swept.
    """
    work = Path(work_dir)
    try:
        text = (work / SCRIPT_FILE).read_text(encoding="utf-8")
        digest = (work / SCRIPT_HASH_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if text.strip() and _sha256(text) == digest else None


def checkpointed_timed_transcript(work_dir: str | Path) -> str | None:
    """segments.json when it belongs to the work dir's whole script checkpoint, else None."""
    work = Path(work_dir)
    if checkpointed_script(work) is None:
        return None
    try:
        text = (work / TIMED_FILE).read_text(encoding="utf-8")
        digest = (work / SCRIPT_HASH_FILE).read_text(encoding="utf-8").strip()
        return text if json.loads(text).get("scriptSha256") == digest else None
    except (OSError, ValueError, AttributeError):
        return None


@tracked_stage("transcript_ready", lambda r: {"words": len(r.text.split())})
def ensure_script(*, work_dir: str | Path, source: TranscriptSource | None = None,
                  audio_path: str | Path | None = None,
                  stt_model: str = DEFAULT_STT_MODEL, jobs: int = 4) -> PreparedScript:
    """Return a canonical script, running STT only when no script exists."""
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    if source is None:
        if audio_path is None:
            raise TypeError("source or audio_path is required")
        source = AudioSttSource(audio_path, stt_model=stt_model, jobs=jobs)

    script_path = work / SCRIPT_FILE
    existing = script_path.read_text(encoding="utf-8") if script_path.is_file() else None

    # Audio checkpoints are authoritative within a durable per-job work root.
    # Calling source.transcript() merely to compare would pay for STT again.
    if existing and existing.strip() and getattr(source, "kind", "audio") == "audio":
        current_progress().emit("transcript_checkpoint", "reused", reason="checkpoint_reused")
        text = existing
        engine_path = work / STT_ENGINE_FILE
        stt_engine = engine_path.read_text(encoding="utf-8").strip() if engine_path.is_file() else None
        timing = (_read_json(work / TIMING_FILE) or timing_mod.no_timing("the timing checkpoint is unreadable")) \
            if (work / TIMING_FILE).is_file() else \
            timing_mod.no_timing("the script checkpoint has no timings (transcribed before they were kept)")
    else:
        try:
            text = source.transcript()
        except TranscriptionError as exc:
            raise EnrichmentError(exc.stage, str(exc)) from exc
        except (TypeError, ValueError, OSError) as exc:
            raise EnrichmentError("script", str(exc)) from exc
        stt_engine = getattr(text, "stt_engine", None)
        timing = (getattr(text, "timing", None) or timing_mod.no_timing("speech-to-text returned no timings")) \
            if getattr(source, "kind", "audio") == "audio" else None
    if not isinstance(text, str) or not text.strip():
        raise EnrichmentError("script", "script contains no text")

    digest = _sha256(text)
    previous = (work / SCRIPT_HASH_FILE).read_text(encoding="utf-8").strip() \
        if (work / SCRIPT_HASH_FILE).is_file() else None
    if previous and previous != digest:
        _invalidate_dependents(work)
    _atomic_write(script_path, render.render_script_markdown(text))
    _atomic_write(work / SCRIPT_HASH_FILE, digest + "\n")
    if stt_engine:
        _atomic_write(work / STT_ENGINE_FILE, stt_engine + "\n")
    else:
        (work / STT_ENGINE_FILE).unlink(missing_ok=True)
    if timing is None:   # a script source (reader speech): nothing was spoken to time
        (work / TIMING_FILE).unlink(missing_ok=True)
        timed, summary = None, timing_mod.no_timing("not speech-to-text")
    else:
        _atomic_write(work / TIMING_FILE, json.dumps(timing, ensure_ascii=False))
        timed, summary = timing_mod.build(text, timing)
    if timed:
        _atomic_write(work / TIMED_FILE, timed)
    else:
        (work / TIMED_FILE).unlink(missing_ok=True)
    return PreparedScript(text=text, path=script_path, sha256=digest, source=source, stt_engine=stt_engine,
                          timed_transcript=timed, timing=summary)


@tracked_stage("prepare_paragraphs")
def _stage_stream(work_dir: Path, script: PreparedScript) -> ParagraphStream:
    path = work_dir / PARAGRAPHS_FILE
    cached = _read_json(path) if path.exists() else None
    if cached:
        stream = stream_from_json(cached)
        if stream.stream_sha256 == build_stream(script.text).stream_sha256:
            return stream
        logger.warning("paragraph checkpoint does not match script; rebuilding")
    stream_method = getattr(script.source, "stream", None)
    stream = stream_method() if callable(stream_method) else build_stream(script.text)
    if stream.stream_sha256 != build_stream(script.text).stream_sha256:
        raise EnrichmentError("script", "source stream does not match canonical script")
    _atomic_write(path, json.dumps(stream.to_json(), ensure_ascii=False, indent=1))
    return stream


@tracked_stage("topic_analysis", lambda r: {"topics": len(r[0].get("topics") or [])})
def _stage_topics(path: Path, stream: ParagraphStream, runner_factory,
                  source: TranscriptSource, preknown_topics=None, *, sections: bool = False,
                  speaker=None, source_description: str | None = None) -> tuple[dict, dict]:
    cached = _read_json(path) if path.exists() else None
    if cached and cached.get("topics") and validate_topics(stream, cached, sections=sections).ok:
        current_progress().emit("topic_checkpoint", "reused", reason="checkpoint_reused")
        logger.info("reusing checkpointed topic analysis (%d topics)", len(cached["topics"]))
        return cached, {"cached": True, "ok": True}

    if bool(getattr(source, "has_preknown_topics", False)):
        try:
            analysis = source.analysis(stream, preknown_topics)
            report = validate_topics(stream, analysis)
        except (TypeError, ValueError, KeyError) as exc:
            raise EnrichmentError("topics:source", str(exc)) from exc
        if not report.ok:
            raise EnrichmentError("topics:source", "; ".join(report.errors))
        payload = report.to_json()
        payload["preknown"] = True
        _atomic_write(path, json.dumps(analysis, ensure_ascii=False, indent=1))
        return analysis, payload

    started = time.monotonic()
    try:
        analysis, report, _raw = run_topics(stream, runner=runner_factory(TOPICS_ROLE),
                                            sections=sections, speaker=speaker,
                                            source_description=source_description)
    except EnrichmentModelError as exc:
        raise EnrichmentError(f"topics:{exc.stage}", str(exc)) from exc
    payload = report.to_json()
    payload["durationSeconds"] = round(time.monotonic() - started, 1)
    _atomic_write(path, json.dumps(analysis, ensure_ascii=False, indent=1))
    return analysis, payload


@tracked_stage("reference_extraction", lambda r: {"references": sum(len(t.get("mentions") or []) for t in r[0].get("topics") or [])})
def _stage_mentions(path: Path, stream: ParagraphStream, analysis: dict,
                    runner_factory, source_kind="audio", *,
                    source_description: str | None = None) -> tuple[dict, dict]:
    cached = _read_json(path) if path.exists() else None
    if cached and isinstance(cached.get("topics"), list):
        current_progress().emit("reference_checkpoint", "reused", reason="checkpoint_reused")
        logger.info("reusing checkpointed mentions")
        return cached, {"cached": True, "ok": True}
    started = time.monotonic()
    batches = mention_batches(analysis["topics"])
    if len(batches) > 1:
        mentions, payload = _batched_mentions(path, stream, batches, runner_factory, source_kind,
                                              source_description=source_description)
    else:
        try:
            mentions, report, _raw = run_mentions(
                stream, analysis, runner=runner_factory(MENTIONS_ROLE),
                source_kind=source_kind, source_description=source_description)
        except EnrichmentModelError as exc:
            raise EnrichmentError(f"mentions:{exc.stage}", str(exc)) from exc
        payload = report.to_json()
    payload["durationSeconds"] = round(time.monotonic() - started, 1)
    _atomic_write(path, json.dumps(mentions, ensure_ascii=False, indent=1))
    return mentions, payload


def _batched_mentions(path: Path, stream: ParagraphStream, batches: list[list],
                      runner_factory, source_kind: str, *,
                      source_description: str | None = None) -> tuple[dict, dict]:
    """Pass 2 one batch of consecutive topics at a time (luna.mention_batches), each on only its topics'
    paragraphs, through the same prompt and gate; joined in order under one report. Each batch is cached
    by its paragraphs and topics (``mentions-batch-*.json``), so a retry reuses every finished batch.
    Names are made canonical within a batch, as they are within a segment."""
    topics, notes, parts, first = [], [], [], 0
    for number, batch in enumerate(batches, 1):
        name = f"batch {number}/{len(batches)} (topics {first + 1}-{first + len(batch)})"
        first += len(batch)
        part = substream(stream, Segment(name, batch[0]["startParagraphId"], batch[-1]["endParagraphId"]))
        cache = path.with_name("mentions-batch-%s.json" % _sha256(
            part.stream_sha256 + json.dumps(batch, sort_keys=True, ensure_ascii=False))[:12])
        cached = _read_json(cache) if cache.exists() else None
        if cached and isinstance(cached.get("topics"), list) and isinstance(cached.get("report"), dict):
            mentions, report = {"topics": cached["topics"]}, {**cached["report"], "cached": True}
        else:
            started = time.monotonic()
            try:
                mentions, gate, _raw = run_mentions(part, {"topics": batch}, runner=runner_factory(MENTIONS_ROLE),
                                                    source_kind=source_kind,
                                                    source_description=source_description)
            except EnrichmentModelError as exc:
                logger.error("mentions %s failed: %s", name, exc)
                raise EnrichmentError(f"mentions:{exc.stage}", f"{name}: {exc}") from exc
            report = {**gate.to_json(), "durationSeconds": round(time.monotonic() - started, 1)}
            _atomic_write(cache, json.dumps({**mentions, "report": report}, ensure_ascii=False, indent=1))
        kept = sum(len(topic.get("mentions") or []) for topic in mentions["topics"])
        logger.info("mentions %s: %d mentions, %s s", name, kept, report.get("durationSeconds"))
        topics.extend(mentions["topics"])
        notes.extend(f"{name}: {note}" for note in report.get("notes") or [])
        parts.append({"batch": name, "topics": len(batch), "stats": report.get("stats") or {},
                      "durationSeconds": report.get("durationSeconds"), "cached": bool(report.get("cached"))})
    total = {key: sum(p["stats"].get(key) or 0 for p in parts)
             for key in ("mentionsReturned", "mentionsKept", "mentionsDropped")}
    total["keepRate"] = round(total["mentionsKept"] / total["mentionsReturned"], 3) \
        if total["mentionsReturned"] else None
    return {"topics": topics}, {"ok": True, "errors": [], "notes": notes, "stats": total, "batches": parts}


def _segments(stream: ParagraphStream, script: PreparedScript, segmenter, context, topics) -> list[Segment]:
    """The segmenter's segments, checked to cover the stream once, in order."""
    if segmenter is None:
        return single_segment(stream, None, context or {})
    timed = json.loads(script.timed_transcript) if script.timed_transcript else None
    try:
        segments = segmenter(stream, timed, dict(context or {}))
    except (TypeError, ValueError, KeyError) as exc:
        raise EnrichmentError("segments", f"segmenter failed: {exc}") from exc
    errors = check_segments(stream, segments)
    if errors:
        raise EnrichmentError("segments", "; ".join(errors))
    if len(segments) > 1 and (topics or getattr(script.source, "has_preknown_topics", False)):
        raise EnrichmentError("segments", "a script with preknown topics is enriched in one segment")
    return segments


def _assemble(parts) -> tuple[dict, dict]:
    """One analysis and one mentions document from the segments' (one segment: unchanged)."""
    if len(parts) == 1:
        return parts[0][1], parts[0][2]
    summary = "\n\n".join(f"{seg.label}: {str(a.get('summary', '')).strip()}" for seg, a, _, _, _ in parts)
    topics = [topic for _, a, _, _, _ in parts for topic in a.get("topics") or []]
    mentions = [topic for _, _, m, _, _ in parts for topic in m.get("topics") or []]
    return {"summary": summary, "topics": topics}, {"topics": mentions}


def _clock_lookup(script: PreparedScript, stream: ParagraphStream):
    """(at, texts): a paragraph's clock from the stored timings, and its text for the quotes.

    ``at`` is None without timings (a reader script, or timings that do not belong to this script): then
    the notes carry no clock at all, never a guessed one.
    """
    texts = {paragraph.id: paragraph.text for paragraph in stream.paragraphs}
    if not script.timed_transcript:
        return None, texts
    try:
        starts = {str(item["id"]): float(item["start"])
                  for item in json.loads(script.timed_transcript).get("paragraphs") or []}
    except (ValueError, AttributeError, KeyError, TypeError):
        logger.warning("the timed transcript is unreadable; the notes carry no clocks")
        return None, texts
    if not all(paragraph_id in starts for paragraph_id in texts):
        logger.warning("the timed transcript is not this script's; the notes carry no clocks")
        return None, texts
    from tg_tools_common.clocks import clock
    return (lambda paragraph_id: clock(starts[str(paragraph_id)])), texts


def default_runner_factory(work_dir: Path, tier: str | None):
    """Runners for the two transcript roles on ``tier`` (from ``pi_harness.enrichment_tier``).

    There is no default tier: a caller without one has not checked its pipeline's configuration.
    """
    def factory(role: str):
        from pi_harness import EnrichmentConfigError, PiRunner
        if not tier:
            raise EnrichmentConfigError("enrichment ran without a model tier")
        return PiRunner(role=role, cwd=str(work_dir), tier=tier)
    return factory


def enrich_script(
    *, script: PreparedScript, work_dir: str | Path, title: str,
    slug: str | None = None, published: str | None = None,
    date_basis: str = "unknown", duration_seconds: int | float | None = None,
    mp3_url: str | None = None, notes_url: str | None = None,
    script_url: str | None = None, source_url: str | None = None,
    stt_model: str = DEFAULT_STT_MODEL, topics: list | None = None,
    runner_factory=None, tier: str | None = None, segments_url: str | None = None,
    segmenter=None, segment_context: dict | None = None,
    notes_shape: str = NOTES_SHAPE_TOPICS, speaker=None, source_description: str | None = None,
) -> EnrichmentResult:
    """Enrich ``script``. ``segmenter`` (episode_enrichment.segments) splits it into parts that are
    enriched one at a time; the default is one segment, today's single pass.

    ``notes_shape`` picks the notes document, never the calls: both shapes make the same two calls.
    ``"topics"`` is the summary + spans + references document the reader pipelines write; ``"sections"``
    is an rss episode's notes (X-104: a lede per section, the episode's main points, code-written clocks,
    quotes and names, and the form gates in the report). ``speaker`` is who the episode's metadata names,
    if anybody: one name or ``{"name", "firm"}``, passed to the prompt as one sentence.
    ``source_description`` is the publisher's own description of the episode: it reaches both prompts only as
    spellings and framing, never as grounding. It is cleaned once here, only its character count is reported,
    and the text itself never reaches the report or the manifest.
    """
    if notes_shape not in NOTES_SHAPES:
        raise ValueError(f"unknown notes shape {notes_shape!r}: one of {', '.join(NOTES_SHAPES)}")
    sections_notes = notes_shape == NOTES_SHAPE_SECTIONS
    cleaned_description = clean_source_description(source_description)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    runner_factory = runner_factory or default_runner_factory(work, tier)
    reports: dict[str, Any] = {"script": {
        "ok": True, "sha256": script.sha256,
        "origin": getattr(script.source, "content_origin", "unknown")},
        "sourceDescription": {"chars": len(cleaned_description) if cleaned_description else 0}}
    stream = _stage_stream(work, script)
    source_kind = getattr(script.source, "kind", "audio")
    segments = _segments(stream, script, segmenter, segment_context, topics)
    if sections_notes and len(segments) > 1:
        raise EnrichmentError("notes", "the sections notes are one document over the whole episode; "
                              "a segmenter cannot split it")
    parts = []
    try:
        for segment in segments:
            part = substream(stream, segment) if len(segments) > 1 else stream
            # Each call is cached by the text it ran on, so a retry reuses every finished segment.
            key = part.stream_sha256[:12]
            where = f"{segment.label}: " if segment.label else ""
            try:
                seg_analysis, topic_report = _stage_topics(
                    work / f"topics-{key}.json", part, runner_factory, script.source, topics,
                    sections=sections_notes, speaker=speaker, source_description=cleaned_description)
                seg_mentions, mention_report = _stage_mentions(
                    work / f"mentions-{key}.json", part, seg_analysis, runner_factory, source_kind,
                    source_description=cleaned_description)
            except EnrichmentError as exc:
                raise EnrichmentError(exc.stage, where + str(exc)) from exc
            parts.append((segment, seg_analysis, seg_mentions, topic_report, mention_report))
            _record_reports(reports, parts, len(segments) > 1)
        analysis, mentions = _assemble(parts)
        _atomic_write(work / ANALYSIS_FILE, json.dumps(analysis, ensure_ascii=False, indent=1))
        _atomic_write(work / MENTIONS_FILE, json.dumps(mentions, ensure_ascii=False, indent=1))

        at, texts = _clock_lookup(script, stream)
        script_markdown = render.render_script_markdown(script.text)
        if _sha256(script_markdown) != script.sha256:
            raise EnrichmentError("script", "script renderer changed canonical bytes")
        if sections_notes:
            # The form gates read the payload both calls returned: a failing section is the report's, and
            # costs no call (X-104). Nothing here can change the answer, only say what is wrong with it.
            names, _firms = speaker_entry(speaker)
            gate = notes_gates(analysis, mentions, speakers=names)
            reports["notes"] = gate.to_json()
            if gate.notes:
                logger.warning("the notes fail %d form checks: %s", len(gate.notes), "; ".join(gate.notes[:5]))
            notes_markdown = render.render_episode_sections(
                title=title, summary=str(analysis.get("summary") or ""),
                main_points=analysis.get("mainPoints") or [], topics=analysis.get("topics") or [],
                references=mentions.get("topics") or [],
                speakers=speaker_intervals(speaker, stream.first_id(), stream.last_id()),
                at=at, texts=texts, published=published, duration_seconds=duration_seconds,
                mp3_url=mp3_url, script_url=script_url, source_url=source_url, source_kind=source_kind)
        else:
            notes_markdown = render.render_notes_markdown(
                stream=stream, analysis=analysis, mentions=mentions, title=title,
                published=published, duration_seconds=duration_seconds, mp3_url=mp3_url,
                script_url=script_url, source_url=source_url, source_kind=source_kind,
                sections=[(seg.label, a, m) for seg, a, m, _, _ in parts] if len(parts) > 1 else None)
        description = render.build_feed_description(
            analysis=analysis, published=published, duration_seconds=duration_seconds,
            mp3_url=mp3_url, notes_url=notes_url, script_url=script_url,
            source_url=source_url, source_kind=source_kind)
        manifest = render.build_manifest(
            stream=stream, analysis=analysis, mentions=mentions, reports=reports,
            title=title, slug=slug, published=published, date_basis=date_basis,
            duration_seconds=duration_seconds, mp3_url=mp3_url,
            notes_url=notes_url, script_url=script_url, source_url=source_url,
            stt_model=(script.stt_engine or stt_model) if source_kind == "audio" else None,
            roles={"topics": TOPICS_ROLE, "mentions": MENTIONS_ROLE},
            source_kind=source_kind,
            content_origin=getattr(script.source, "content_origin", "unknown"),
            topic_origin=getattr(script.source, "topic_origin", "luna_topics"),
            summary_origin=getattr(script.source, "summary_origin", "luna_summary"),
            script_sha256=script.sha256,
            timing=script.timing,
            segments_url=segments_url if script.timed_transcript else None,
            segments=[segment.to_json() for segment in segments],
            feed_description=description,
        )
        _atomic_write(work / SCRIPT_FILE, script_markdown)
        _atomic_write(work / NOTES_FILE, notes_markdown)
        _atomic_write(work / MANIFEST_FILE, manifest)
        _atomic_write(work / REPORT_FILE, json.dumps(reports, ensure_ascii=False, indent=1))
    except BaseException:
        # A stage that fails after the mentions ran still leaves what the run reported, then re-raises.
        if parts:
            _write_reports(work, reports)
        raise
    return EnrichmentResult(
        script_markdown=script_markdown, notes_markdown=notes_markdown,
        manifest=manifest, description=description, stream=stream,
        analysis=analysis, mentions=mentions, reports=reports,
        timed_transcript=script.timed_transcript)


def enrich(
    *, source: TranscriptSource | None = None, audio_path=None, work_dir,
    title: str, slug: str | None = None, published: str | None = None,
    date_basis: str = "unknown", duration_seconds: int | float | None = None,
    mp3_url: str | None = None, notes_url: str | None = None,
    script_url: str | None = None, source_url: str | None = None,
    stt_model: str = DEFAULT_STT_MODEL, jobs: int = 4, topics: list | None = None, runner_factory=None,
    tier: str | None = None, segments_url: str | None = None,
    segmenter=None, segment_context: dict | None = None,
    notes_shape: str = NOTES_SHAPE_TOPICS, speaker=None, source_description: str | None = None,
) -> EnrichmentResult:
    """Ensure a script, then enrich exactly that script."""
    if runner_factory is None and not tier:
        # Before the paid speech-to-text step, not after it.
        from pi_harness import EnrichmentConfigError
        raise EnrichmentConfigError("enrichment ran without a model tier")
    script = ensure_script(work_dir=work_dir, source=source, audio_path=audio_path,
                           stt_model=stt_model, jobs=jobs)
    return enrich_script(
        script=script, work_dir=work_dir, title=title, slug=slug,
        published=published, date_basis=date_basis,
        duration_seconds=duration_seconds, mp3_url=mp3_url,
        notes_url=notes_url, script_url=script_url, source_url=source_url,
        stt_model=stt_model, topics=topics, runner_factory=runner_factory, tier=tier,
        segments_url=segments_url, segmenter=segmenter, segment_context=segment_context,
        notes_shape=notes_shape, speaker=speaker, source_description=source_description)
