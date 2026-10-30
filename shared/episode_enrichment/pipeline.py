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

from . import render
from .luna import EnrichmentModelError, MENTIONS_ROLE, TOPICS_ROLE, run_mentions, run_topics
from .paragraphs import ParagraphStream, build_stream, stream_from_json
from .source import AudioSttSource, TranscriptSource
from .stt import DEFAULT_STT_MODEL, TranscriptionError
from .validate import validate_topics

logger = logging.getLogger(__name__)

SCRIPT_FILE = "script.md"
SCRIPT_HASH_FILE = "script.sha256"
# The self-hosted engine that wrote script.md, so a resumed job still records it.
STT_ENGINE_FILE = "stt-engine.txt"
PARAGRAPHS_FILE = "paragraphs.json"
ANALYSIS_FILE = "analysis.json"
MENTIONS_FILE = "mentions.json"
NOTES_FILE = "notes.md"
MANIFEST_FILE = "manifest.json"
REPORT_FILE = "gates.json"
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
    else:
        try:
            text = source.transcript()
        except TranscriptionError as exc:
            raise EnrichmentError(exc.stage, str(exc)) from exc
        except (TypeError, ValueError, OSError) as exc:
            raise EnrichmentError("script", str(exc)) from exc
        stt_engine = getattr(text, "stt_engine", None)
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
    return PreparedScript(text=text, path=script_path, sha256=digest, source=source, stt_engine=stt_engine)


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
def _stage_topics(work_dir: Path, stream: ParagraphStream, runner_factory,
                  source: TranscriptSource, preknown_topics=None) -> tuple[dict, dict]:
    path = work_dir / ANALYSIS_FILE
    cached = _read_json(path) if path.exists() else None
    if cached and cached.get("topics") and validate_topics(stream, cached).ok:
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
        analysis, report, _raw = run_topics(stream, runner=runner_factory(TOPICS_ROLE))
    except EnrichmentModelError as exc:
        raise EnrichmentError(f"topics:{exc.stage}", str(exc)) from exc
    payload = report.to_json()
    payload["durationSeconds"] = round(time.monotonic() - started, 1)
    _atomic_write(path, json.dumps(analysis, ensure_ascii=False, indent=1))
    return analysis, payload


@tracked_stage("reference_extraction", lambda r: {"references": sum(len(t.get("mentions") or []) for t in r[0].get("topics") or [])})
def _stage_mentions(work_dir: Path, stream: ParagraphStream, analysis: dict,
                    runner_factory, source_kind="audio") -> tuple[dict, dict]:
    path = work_dir / MENTIONS_FILE
    cached = _read_json(path) if path.exists() else None
    if cached and isinstance(cached.get("topics"), list):
        current_progress().emit("reference_checkpoint", "reused", reason="checkpoint_reused")
        logger.info("reusing checkpointed mentions")
        return cached, {"cached": True, "ok": True}
    started = time.monotonic()
    try:
        mentions, report, _raw = run_mentions(
            stream, analysis, runner=runner_factory(MENTIONS_ROLE),
            source_kind=source_kind)
    except EnrichmentModelError as exc:
        raise EnrichmentError(f"mentions:{exc.stage}", str(exc)) from exc
    payload = report.to_json()
    payload["durationSeconds"] = round(time.monotonic() - started, 1)
    _atomic_write(path, json.dumps(mentions, ensure_ascii=False, indent=1))
    return mentions, payload


def default_runner_factory(work_dir: Path):
    def factory(role: str):
        from pi_harness import PiRunner
        return PiRunner(role=role, cwd=str(work_dir))
    return factory


def enrich_script(
    *, script: PreparedScript, work_dir: str | Path, title: str,
    slug: str | None = None, published: str | None = None,
    date_basis: str = "unknown", duration_seconds: int | float | None = None,
    mp3_url: str | None = None, notes_url: str | None = None,
    script_url: str | None = None, source_url: str | None = None,
    stt_model: str = DEFAULT_STT_MODEL, topics: list | None = None,
    runner_factory=None,
) -> EnrichmentResult:
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    runner_factory = runner_factory or default_runner_factory(work)
    reports: dict[str, Any] = {"script": {
        "ok": True, "sha256": script.sha256,
        "origin": getattr(script.source, "content_origin", "unknown")}}
    stream = _stage_stream(work, script)
    analysis, reports["topics"] = _stage_topics(
        work, stream, runner_factory, script.source, topics)
    source_kind = getattr(script.source, "kind", "audio")
    mentions, reports["mentions"] = _stage_mentions(
        work, stream, analysis, runner_factory, source_kind)

    script_markdown = render.render_script_markdown(script.text)
    if _sha256(script_markdown) != script.sha256:
        raise EnrichmentError("script", "script renderer changed canonical bytes")
    notes_markdown = render.render_notes_markdown(
        stream=stream, analysis=analysis, mentions=mentions, title=title,
        published=published, duration_seconds=duration_seconds, mp3_url=mp3_url,
        script_url=script_url, source_url=source_url, source_kind=source_kind)
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
    )
    _atomic_write(work / SCRIPT_FILE, script_markdown)
    _atomic_write(work / NOTES_FILE, notes_markdown)
    _atomic_write(work / MANIFEST_FILE, manifest)
    _atomic_write(work / REPORT_FILE, json.dumps(reports, ensure_ascii=False, indent=1))
    return EnrichmentResult(
        script_markdown=script_markdown, notes_markdown=notes_markdown,
        manifest=manifest, description=description, stream=stream,
        analysis=analysis, mentions=mentions, reports=reports)


def enrich(
    *, source: TranscriptSource | None = None, audio_path=None, work_dir,
    title: str, slug: str | None = None, published: str | None = None,
    date_basis: str = "unknown", duration_seconds: int | float | None = None,
    mp3_url: str | None = None, notes_url: str | None = None,
    script_url: str | None = None, source_url: str | None = None,
    stt_model: str = DEFAULT_STT_MODEL, jobs: int = 4, topics: list | None = None, runner_factory=None,
) -> EnrichmentResult:
    """Ensure a script, then enrich exactly that script."""
    script = ensure_script(work_dir=work_dir, source=source, audio_path=audio_path,
                           stt_model=stt_model, jobs=jobs)
    return enrich_script(
        script=script, work_dir=work_dir, title=title, slug=slug,
        published=published, date_basis=date_basis,
        duration_seconds=duration_seconds, mp3_url=mp3_url,
        notes_url=notes_url, script_url=script_url, source_url=source_url,
        stt_model=stt_model, topics=topics, runner_factory=runner_factory)
