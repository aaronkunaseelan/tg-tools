from __future__ import annotations

import contextlib
import hashlib
import html
import json
import logging
import random
import time
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlencode

from config import Settings
from tg_tools_common.podcast_urls import display_url_from_env
from tg_tools_common.progress import current_progress, tracked_stage
from tg_tools_common.rclone_upload import immutable_copy_argv
from episode_enrichment import (EpisodeNames, EpisodeNamingMetadata,
                                build_episode_names, names_from_prefix)
from episode_enrichment.naming import document_version
from source_audio import (
    SourceAudioError,
    SourceMetadata,
    SourceRoute,
    SourceToolSettings,
    convert_input_to_mp3,
    download_source_to_mp3_for_route,
    fetch_source_metadata_for_route,
    run_command,
)


STOP_WORDS = {
    "a",
    "about",
    "after",
    "all",
    "also",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "back",
    "be",
    "been",
    "both",
    "breakdown",
    "but",
    "by",
    "can",
    "channel",
    "clip",
    "discussion",
    "do",
    "does",
    "down",
    "episode",
    "ep",
    "etc",
    "even",
    "explained",
    "featuring",
    "feat",
    "for",
    "from",
    "full",
    "ft",
    "get",
    "got",
    "has",
    "had",
    "have",
    "he",
    "her",
    "here",
    "his",
    "how",
    "if",
    "in",
    "into",
    "interview",
    "is",
    "it",
    "its",
    "just",
    "like",
    "live",
    "long",
    "may",
    "me",
    "more",
    "most",
    "much",
    "my",
    "new",
    "no",
    "nor",
    "not",
    "now",
    "of",
    "off",
    "official",
    "on",
    "only",
    "or",
    "our",
    "out",
    "over",
    "part",
    "per",
    "plus",
    "podcast",
    "reaction",
    "review",
    "same",
    "she",
    "show",
    "so",
    "some",
    "subscribe",
    "such",
    "talk",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "to",
    "too",
    "update",
    "up",
    "us",
    "very",
    "via",
    "video",
    "vs",
    "watch",
    "way",
    "we",
    "well",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "why",
    "with",
    "will",
    "you",
    "your",
}


logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    def __init__(self, stage: str, message: str, stderr: str = "", *,
                 dependency: str | None = None, operation: str | None = None,
                 http_status: int | None = None, retryable: bool | None = None,
                 error_code: int | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.stderr = stderr
        self.dependency = dependency or (
            "publication" if stage == "api" else
            "source" if stage in {"source", "metadata", "download", "route_health"} else stage)
        self.operation = operation
        self.http_status = http_status
        self.retryable = retryable
        self.error_code = error_code

    def diagnostic(self) -> dict:
        return {"dependency": self.dependency, "operation": self.operation,
                "httpStatus": self.http_status, "retryable": self.retryable,
                "errorCode": self.error_code}


# The feeds rss-audio may publish: feed id -> (B2 prefix, env var holding the keyed public root).
# The prefix is also the Worker's `feed` id and the first path segment of every stored URL.
FEEDS = {
    "audio": ("audio", "RSS_AUDIO_PUBLIC_ROOT"),
    "surveillance": ("surveillance", "SURVEILLANCE_PUBLIC_ROOT"),
}
# Which enrichment pipeline (a key of the enrichment tier file) each feed's episodes belong to.
ENRICHMENT_PIPELINES = {"audio": "episodes", "surveillance": "surveillance"}
SCHEDULED_DATE_BASIS = "scheduled_source"


def feed_spec(feed: str) -> tuple[str, str]:
    try:
        return FEEDS[feed]
    except (KeyError, TypeError):
        raise PipelineError("input", f"Unknown feed {feed!r}; known feeds: {', '.join(sorted(FEEDS))}") from None


@dataclass(frozen=True)
class JobOptions:
    """Optional job inputs that apply to every source kind (absent = today's behaviour)."""
    feed: str = "audio"
    published_at: str | None = None   # ISO 8601 UTC, normalised to ...Z
    title: str | None = None
    delivery: str = "telegram"
    # Model tier of this job's enrichment, resolved once at job start (enrichment_preflight).
    enrichment_tier: str | None = None


def job_options(input_json: dict) -> JobOptions:
    feed = input_json.get("feed", "audio")
    feed_spec(feed)
    published_at = input_json.get("publishedAt")
    if published_at is not None:
        published_at = _utc_iso(published_at)
    title = input_json.get("title")
    if title is not None:
        if not isinstance(title, str) or not " ".join(title.split()):
            raise PipelineError("input", "title must be a non-empty string")
        title = " ".join(title.split())
    delivery = input_json.get("delivery", "telegram")
    if delivery not in {"telegram", "none"}:
        raise PipelineError("input", f"Unknown delivery {delivery!r}; expected 'none' or absent")
    return JobOptions(feed=feed, published_at=published_at, title=title, delivery=delivery)


def enrichment_preflight(settings: Settings, feed: str) -> str | None:
    """The enrichment model tier of ``feed``'s pipeline, read from the tier file now.

    Called once at the start of a job, before any download or paid call. A configuration problem
    fails the job (terminal, errorClass ``enrichment_config``); it is never the optional
    "enrichment failed, audio published anyway" outcome. None when enrichment is switched off.
    """
    if not settings.enrichment_enabled:
        return None
    feed_spec(feed)
    pipeline = ENRICHMENT_PIPELINES[feed]
    try:
        from pi_harness import EnrichmentConfigError, enrichment_tier
    except ImportError as exc:
        raise PipelineError("enrichment_config", f"enrichment model configuration is unavailable: {exc}",
                            retryable=False) from exc
    try:
        return enrichment_tier(pipeline)
    except EnrichmentConfigError as exc:
        raise PipelineError("enrichment_config", str(exc), retryable=False) from exc


def _utc_iso(value) -> str:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise PipelineError("input", f"publishedAt must be an ISO 8601 UTC time, got {value!r}") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise PipelineError("input", f"publishedAt must carry a UTC offset, got {value!r}")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_options(metadata: "EpisodeMetadata", options: JobOptions) -> "EpisodeMetadata":
    """Feed, title and date the job asked for. A given title is the whole episode title, so the
    source's channel, creators and series (which would prefix it and the slug) are dropped."""
    changes: dict = {"feed": options.feed}
    if options.title:
        changes.update(title=options.title, channel="", creators=(), series="")
    if options.published_at:
        changes.update(source_published_at=options.published_at, date_basis=SCHEDULED_DATE_BASIS,
                       pub_date=options.published_at,
                       published_human=datetime.fromisoformat(options.published_at.replace("Z", "+00:00"))
                       .strftime("%b %d, %Y"))
    return replace(metadata, **changes)


@dataclass(frozen=True)
class EpisodeMetadata:
    title: str
    channel: str
    description: str
    duration_seconds: int
    published_human: str
    source_url: str | None = None
    creators: tuple[str, ...] = ()
    series: str = ""
    # Provenanced original publication date; never ingestion time, and None
    # rather than a guess. `date_basis` says where it came from.
    source_published_at: str | None = None
    date_basis: str = "unknown"
    enclosure_url: str | None = None
    feed: str = "audio"
    # Feed ordering time the job supplied; None = ingestion time.
    pub_date: str | None = None


@dataclass(frozen=True)
class PublishResult:
    slug: str
    title: str
    public_url: str
    file_size_bytes: int
    script_url: str | None = None
    notes_url: str | None = None
    manifest_url: str | None = None
    segments_url: str | None = None
    page_url: str | None = None
    # The notes job to submit (pipeline.notes_input) when the episode has a script and no notes yet.
    notes: dict | None = None


def _episode_names(metadata: EpisodeMetadata, identity: str,
                   now: datetime | None = None) -> EpisodeNames:
    return build_episode_names(EpisodeNamingMetadata(
        title=metadata.title,
        identity=identity,
        creators=metadata.creators,
        channel=metadata.channel,
        series=metadata.series,
        published_at=metadata.source_published_at,
        created_at=now,
    ))


def _public_bases(settings: Settings, feed: str) -> tuple[str, str]:
    """(audio base, document base) of one feed's public episode URLs."""
    prefix, _ = feed_spec(feed)
    if feed == "audio":
        return settings.public_audio_base_url, settings.public_md_base_url
    if not settings.base_url:
        raise PipelineError("config", "Missing RSS audio base URL")
    base = f"{settings.base_url}/{prefix}/episodes"
    return base, base


def build_public_audio_url(settings: Settings, slug: str, feed: str = "audio") -> str:
    return f"{_public_bases(settings, feed)[0]}/{slug}.mp3"


def render_completion_message(
    title_or_slug: str,
    public_url: str,
    notes_url: str | None = None,
    script_url: str | None = None,
    source_url: str | None = None,
    feed: str = "audio",
) -> str:
    # Links in messages take the podcast's keyed path; stored URLs keep the storage form.
    prefix, root_variable = feed_spec(feed)
    public_url, notes_url, script_url = (display_url_from_env(url, prefix, root_variable)
                                         for url in (public_url, notes_url, script_url))
    safe_label = html.escape(title_or_slug, quote=False)
    safe_url = html.escape(public_url, quote=True)
    message = f'Posted: <a href="{safe_url}">{safe_label}</a>'
    extras = []
    if script_url:
        extras.append(f'<a href="{html.escape(script_url, quote=True)}">Script</a>')
    if notes_url:
        extras.append(f'<a href="{html.escape(notes_url, quote=True)}">Notes</a>')
    if source_url:
        extras.append(f'<a href="{html.escape(source_url, quote=True)}">Original</a>')
    if extras:
        message += " . " + " . ".join(extras)
    return message


def save_failure_log(settings: Settings, error: PipelineError, extra: str = "") -> Path:
    settings.fail_log_dir.mkdir(parents=True, exist_ok=True)
    settings.fail_log_dir.chmod(0o700)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    path = settings.fail_log_dir / f"rss-audio-bot-FAIL-{stamp}.log"
    body = [
        f"time: {datetime.now(UTC).isoformat()}",
        f"stage: {error.stage}",
        f"message: {error}",
    ]
    if extra:
        body.extend(["", extra])
    if error.stderr:
        body.extend(["", "stderr:", error.stderr])
    _write_private_text(path, "\n".join(body) + "\n")
    return path


def ensure_environment(settings: Settings) -> None:
    if not settings.api_base_url:
        raise PipelineError("config", "Missing RSS audio API base URL")
    if not settings.api_token:
        raise PipelineError("config", "Missing RSS audio API token")

    for binary in (
        settings.yt_dlp_bin,
        settings.ffmpeg_bin,
        settings.ffprobe_bin,
        settings.rclone_bin,
    ):
        _run(["bash", "-lc", f"command -v {shlex.quote(binary)} >/dev/null"], stage="healthcheck")


def list_recent_episodes(settings: Settings, limit: int = 10) -> list[tuple[str, str]]:
    rows = _list_episodes(settings)
    return [(row["slug"], row["title"]) for row in rows[:limit]]


def delete_episode(settings: Settings, slug: str, feed: str | None = None) -> None:
    """Delete an episode's row and every object it owns, in whichever feed holds it (or ``feed``)."""
    slug = _validate_slug(slug)
    feeds = [feed] if feed else list(FEEDS)
    found = next(((name, row) for name in feeds if (row := _get_episode_by_slug(settings, slug, name))), None)
    if found is None:
        raise PipelineError("delete", f"No episode found for slug `{slug}`")
    feed, episode = found
    _api_json(settings, "DELETE", f"/api/episodes/{episode['id']}")
    # Every object named <slug>.*: the mp3 and each version of the script, segments, notes and manifest
    # (a slug ends in a 12-hex identity, so the pattern holds only this episode's objects).
    delete_documents(settings, slug, feed)


def delete_documents(settings: Settings, slug: str, feed: str, *, below_version: int | None = None) -> None:
    """Delete ``slug``'s objects in ``feed``: all of them, or only the documents of versions below n."""
    slug = _validate_slug(slug)
    remote = f"{settings.b2_remote}:{settings.b2_bucket}/{feed_spec(feed)[0]}/episodes/"
    if below_version is None:
        patterns = [f"{slug}.*"]
    else:
        listed = _run([settings.rclone_bin, "lsf", "--include", f"{slug}.*", remote], stage="delete").stdout.split()
        patterns = [name for name in listed if not name.endswith(".mp3")
                    and (document_version(slug, name) or below_version) < below_version]
    for pattern in patterns:
        _run([settings.rclone_bin, "delete", "--include", pattern, remote], stage="delete")


@contextlib.contextmanager
def _staging_dir(work_dir: Path | None):
    """Durable work dir when the caller supplies one, else a temp dir.

    A durable directory lets a retry reuse completed enrichment stages instead
    of re-running transcription.
    """
    if work_dir is not None:
        target = Path(work_dir)
        target.mkdir(parents=True, exist_ok=True)
        yield target
        return
    with tempfile.TemporaryDirectory(prefix="rss-audio-bot-") as tmpdir:
        yield Path(tmpdir)


DEFAULT_OPTIONS = JobOptions()


def ingest_url_with_metadata(
    settings: Settings,
    url: str,
    metadata: EpisodeMetadata,
    *,
    route: SourceRoute = SourceRoute.OCI_MULLVAD,
    work_dir: Path | None = None,
    options: JobOptions = DEFAULT_OPTIONS,
) -> PublishResult:
    metadata = apply_options(metadata, options)
    identity = hashlib.sha256(url.encode("utf-8")).hexdigest()
    names = _episode_names(metadata, identity)
    published = already_published(settings, names.prefix, metadata.feed, metadata, work_dir)
    if published is not None:
        return published
    names = names_from_prefix(names.prefix)
    with _staging_dir(work_dir) as staging:
        mp3_path = staging / names.audio_name
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            _download_url_to_mp3(settings, url, mp3_path, route=route)
        return _finalize_publish(settings, metadata, names, mp3_path, staging, options.enrichment_tier)


def ingest_direct_audio_url(
    settings: Settings,
    url: str,
    *,
    work_dir: Path | None = None,
    options: JobOptions = DEFAULT_OPTIONS,
) -> PublishResult:
    metadata = apply_options(fetch_metadata(settings, url, route=SourceRoute.OCI_MULLVAD), options)
    identity = hashlib.sha256(url.encode("utf-8")).hexdigest()
    names = _episode_names(metadata, identity)
    published = already_published(settings, names.prefix, metadata.feed, metadata, work_dir)
    if published is not None:
        return published
    names = names_from_prefix(names.prefix)
    with _staging_dir(work_dir) as staging:
        mp3_path = staging / names.audio_name
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            # Never let ffmpeg fetch a remote URL directly: stage it through
            # the same explicit Mullvad yt-dlp route as extractor sources.
            _download_url_to_mp3(settings, url, mp3_path, route=SourceRoute.OCI_MULLVAD)
        return _finalize_publish(settings, metadata, names, mp3_path, staging, options.enrichment_tier)


def ingest_local_audio(
    settings: Settings,
    source_path: Path,
    title_hint: str | None = None,
    *,
    work_dir: Path | None = None,
    options: JobOptions = DEFAULT_OPTIONS,
) -> PublishResult:
    base_title = title_hint or source_path.stem or "audio"
    metadata = apply_options(EpisodeMetadata(
        title=base_title.replace("-", " ").strip() or "audio",
        channel="telegram",
        description="",
        duration_seconds=0,
        # A Telegram upload carries no public original-publication date, and
        # ingestion time is not one.
        published_human="Unknown",
        source_url=None,
        source_published_at=None,
        date_basis="unknown",
    ), options)
    try:
        identity = hashlib.sha256(source_path.read_bytes()).hexdigest()
    except OSError:
        # Admission normally guarantees a staged file. Keep metadata-only
        # planning/test callers deterministic without pretending the bytes were read.
        identity = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()
    names = _episode_names(metadata, identity)
    published = already_published(settings, names.prefix, metadata.feed, metadata, work_dir)
    if published is not None:
        return published
    names = names_from_prefix(names.prefix)
    with _staging_dir(work_dir) as staging:
        mp3_path = staging / names.audio_name
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            _convert_input_to_mp3(settings, str(source_path), mp3_path, stage="convert")
        return _finalize_publish(settings, metadata, names, mp3_path, staging, options.enrichment_tier)


def ingest_live_window(
    settings: Settings,
    *,
    source_url: str,
    identity_text: str,
    fetch_audio,
    work_dir: Path | None = None,
    options: JobOptions = DEFAULT_OPTIONS,
) -> PublishResult:
    """Publish a window cut from a live stream.

    The identity is the window (video id + start), not the bytes, so a retry or a re-run finds
    its episode before paying for three hours of audio. ``fetch_audio(staging_dir)`` returns the
    window's audio file and is called only when the episode does not exist yet.
    """
    metadata = apply_options(EpisodeMetadata(
        title=options.title or "Live window", channel="", description="", duration_seconds=0,
        published_human="Unknown", source_url=source_url, source_published_at=None, date_basis="unknown",
    ), options)
    names = _episode_names(metadata, hashlib.sha256(identity_text.encode("utf-8")).hexdigest())
    published = already_published(settings, names.prefix, metadata.feed, metadata, work_dir)
    if published is not None:
        return published
    names = names_from_prefix(names.prefix)
    with _staging_dir(work_dir) as staging:
        mp3_path = staging / names.audio_name
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            audio_path = Path(fetch_audio(staging))
            _convert_input_to_mp3(settings, str(audio_path), mp3_path, stage="convert")
        metadata = replace(metadata, duration_seconds=_probe_duration_seconds(settings, mp3_path))
        return _finalize_publish(settings, metadata, names, mp3_path, staging, options.enrichment_tier)


def _probe_duration_seconds(settings: Settings, path: Path) -> int:
    result = _run([settings.ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of",
                   "default=noprint_wrappers=1:nokey=1", str(path)], stage="validate", timeout=60)
    try:
        return max(0, round(float(result.stdout.strip())))
    except ValueError:
        return 0


@tracked_stage("source_metadata", lambda r: {"title": " ".join(r.title.split())[:200], "audioDurationSeconds": max(0,r.duration_seconds)})
def fetch_metadata(
    settings: Settings,
    url: str,
    *,
    route: SourceRoute = SourceRoute.OCI_MULLVAD,
) -> EpisodeMetadata:
    try:
        metadata = fetch_source_metadata_for_route(_source_tool_settings(settings), url, route)
    except SourceAudioError as exc:
        raise PipelineError(exc.stage, str(exc), exc.stderr) from exc
    return _episode_metadata_from_source(metadata)


def _episode_metadata_from_source(metadata: SourceMetadata) -> EpisodeMetadata:
    return EpisodeMetadata(
        title=metadata.title,
        channel=metadata.channel,
        description=metadata.description,
        duration_seconds=metadata.duration_seconds,
        published_human=metadata.published_human,
        source_url=metadata.source_url,
        creators=tuple(getattr(metadata, "creators", ()) or ()),
        series=str(getattr(metadata, "series", "") or ""),
        source_published_at=getattr(metadata, "source_published_at", None),
        date_basis=getattr(metadata, "date_basis", "unknown"),
    )


def build_public_notes_url(settings: Settings, names: EpisodeNames, feed: str = "audio") -> str:
    return f"{_public_bases(settings, feed)[1]}/{names.notes_name}"


def build_public_script_url(settings: Settings, names: EpisodeNames, feed: str = "audio") -> str:
    return f"{_public_bases(settings, feed)[1]}/{names.script_name}"


def build_public_segments_url(settings: Settings, names: EpisodeNames, feed: str = "audio") -> str:
    return f"{_public_bases(settings, feed)[1]}/{names.segments_name}"


def build_public_page_url(settings: Settings, names: EpisodeNames, feed: str = "audio") -> str:
    return f"{_public_bases(settings, feed)[1]}/{names.page_name}"


def build_public_md_url(settings: Settings, slug: str, feed: str = "audio") -> str:
    return f"{_public_bases(settings, feed)[1]}/{slug}.notes.md"


def build_public_manifest_url(settings: Settings, names: EpisodeNames | str, feed: str = "audio") -> str:
    name = names.manifest_name if isinstance(names, EpisodeNames) else f"{names}.manifest.json"
    return f"{_public_bases(settings, feed)[1]}/{name}"


def _transcribe(settings: Settings, mp3_path: Path, work_dir: Path | None, tier: str | None):
    """The episode's script and timed transcript (a PreparedScript), or None for audio only.

    Speech-to-text runs here; notes are the notes job's (notes_job.py), so nothing waits on them. A
    configuration problem fails the job; any other failure publishes the audio alone, as before.
    """
    if not settings.enrichment_enabled:
        return None
    if not tier:   # the notes job will need it: fail before the paid step, not after
        raise PipelineError("enrichment_config", "enrichment ran without a model tier", retryable=False)
    from episode_enrichment import AudioSttSource, ensure_script
    target = Path(work_dir) if work_dir else mp3_path.parent / "enrichment"
    try:
        return ensure_script(work_dir=target, source=AudioSttSource(
            mp3_path, stt_model=settings.enrichment_stt_model, jobs=settings.enrichment_jobs))
    except Exception as exc:
        if getattr(exc, "error_class", None) == "enrichment_config":
            raise PipelineError("enrichment_config", str(exc), retryable=False) from exc
        logger.warning("speech-to-text failed; publishing audio only: %s", exc)
        return None


def notes_input(*, feed: str, slug: str, version: int, script_sha256: str | None, title: str,
                published_at: str | None, date_basis: str, duration_seconds: int | float | None,
                source_url: str | None, stt_engine: str | None, segmenter: str | None = None,
                segment_context: dict | None = None) -> dict:
    """The notes job's input (notes_job.py). Everything its manifest needs travels with it.

    The original's link is ``originalUrl``: a ``sourceUrl`` would make the worker fetch it as a source.
    """
    payload = {"kind": "rss", "inputKind": "notes", "feed": feed, "slug": slug, "version": version,
               "scriptSha256": script_sha256, "title": title, "publishedAt": published_at,
               "dateBasis": date_basis, "durationSeconds": duration_seconds, "originalUrl": source_url,
               "sttEngine": stt_engine, "delivery": "none"}
    if segmenter:
        payload["segmenter"] = segmenter
    if segment_context:
        payload["segmentContext"] = dict(segment_context)
    return payload


def _notes_for(metadata: EpisodeMetadata, slug: str, version: int, script_sha256: str | None,
               stt_engine: str | None) -> dict:
    return notes_input(feed=metadata.feed, slug=slug, version=version, script_sha256=script_sha256,
                       title=metadata.title, published_at=metadata.source_published_at,
                       date_basis=metadata.date_basis, duration_seconds=metadata.duration_seconds or None,
                       source_url=metadata.source_url, stt_engine=stt_engine)


def _finalize_publish(
    settings: Settings,
    metadata: EpisodeMetadata,
    names: EpisodeNames | str,
    mp3_path: Path,
    work_dir: Path | None = None,
    tier: str | None = None,
) -> PublishResult:
    _validate_mp3_artifact(settings, mp3_path)
    if isinstance(names, str):
        names = names_from_prefix(names)

    # The script and its timed transcript are complete before the first upload; notes follow in
    # their own job, so the episode goes live as soon as speech-to-text is done.
    script = _transcribe(settings, mp3_path, work_dir, tier)
    artifact_dir = Path(work_dir) if work_dir else mp3_path.parent
    documents: list[tuple[Path, str]] = []   # (local path, file name), uploaded in this order
    if script is not None:
        for text, file_name in ((script.text, names.script_name), (script.timed_transcript, names.segments_name)):
            if text is not None:   # no timings, no segments file
                (artifact_dir / file_name).write_text(text, encoding="utf-8")
                documents.append((artifact_dir / file_name, file_name))

    uploaded_keys: list[str] = []
    feed = metadata.feed
    prefix = feed_spec(feed)[0]
    public_audio_url = build_public_audio_url(settings, names.prefix, feed)
    script_url = segments_url = None
    try:
        _upload_mp3(settings, mp3_path, prefix)
        uploaded_keys.append(f"{prefix}/episodes/{names.audio_name}")
        for local_path, file_name in documents:
            key = f"{prefix}/episodes/{file_name}"
            _upload_artifact(settings, local_path, key)
            uploaded_keys.append(key)
        uploaded = {file_name for _, file_name in documents}
        if names.script_name in uploaded:
            script_url = build_public_script_url(settings, names, feed)
        if names.segments_name in uploaded:
            segments_url = build_public_segments_url(settings, names, feed)

        try:
            _create_episode(
                settings=settings,
                metadata=metadata,
                slug=names.prefix,
                public_audio_url=public_audio_url,
                file_size_bytes=mp3_path.stat().st_size,
                script_url=script_url,
            )
        except PipelineError as exc:
            # A lost response (or concurrent create conflict) is resolved by
            # reading the deterministic identity, never by replaying the POST.
            if exc.dependency == "publication" and exc.operation == "POST" and (
                exc.retryable or exc.http_status in {400, 409} or exc.error_code == 1101
            ):
                published = already_published(settings, names.prefix, feed, metadata, work_dir)
                if published is not None:
                    return published
            raise
    except PipelineError:
        # A create response may be ambiguous; existing deterministic objects
        # may already be referenced. Preserve bytes until reconciliation.
        logger.warning("publication incomplete; preserving %d uploads", len(uploaded_keys))
        raise

    return PublishResult(
        slug=names.prefix,
        title=metadata.title,
        public_url=public_audio_url,
        file_size_bytes=mp3_path.stat().st_size,
        script_url=script_url,
        segments_url=segments_url,
        notes=_notes_for(metadata, names.prefix, 1, script.sha256, script.stt_engine) if script_url else None,
    )


@tracked_stage("download_audio")
def _download_url_to_mp3(
    settings: Settings,
    url: str,
    output_path: Path,
    *,
    route: SourceRoute,
) -> None:
    try:
        download_source_to_mp3_for_route(_source_tool_settings(settings), url, output_path, route)
    except SourceAudioError as exc:
        raise PipelineError(exc.stage, str(exc), exc.stderr) from exc


@tracked_stage("convert_audio")
def _convert_input_to_mp3(
    settings: Settings,
    source: str,
    output_path: Path,
    *,
    stage: str,
) -> None:
    try:
        convert_input_to_mp3(_source_tool_settings(settings), source, output_path, stage=stage)
    except SourceAudioError as exc:
        raise PipelineError(exc.stage, str(exc), exc.stderr) from exc


@tracked_stage("upload_audio")
def _upload_mp3(settings: Settings, mp3_path: Path, prefix: str = "audio") -> None:
    remote = f"{settings.b2_remote}:{settings.b2_bucket}/{prefix}/episodes/"
    _run([settings.rclone_bin, "copy", str(mp3_path), remote], stage="upload", timeout=3600)


@tracked_stage("upload_document")
def _upload_artifact(settings: Settings, local_path: Path, key: str) -> None:
    """Upload one enrichment artifact to an exact key.

    A retry can never silently rewrite an artifact a published episode
    already points at; a changed body is an error instead.
    """
    remote = f"{settings.b2_remote}:{settings.b2_bucket}/{key}"
    with immutable_copy_argv(settings.rclone_bin, local_path, remote) as argv:
        _run(argv, stage="upload", timeout=600)


def _validate_mp3_artifact(
    settings: Settings,
    mp3_path: Path,
    *,
    stage: str = "validate",
) -> None:
    if not mp3_path.exists() or mp3_path.stat().st_size <= 0:
        raise PipelineError(stage, "MP3 artifact is missing or empty")

    result = _run(
        [
            settings.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name",
            "-of",
            "json",
            str(mp3_path),
        ],
        stage=stage,
        timeout=60,
    )
    try:
        probe = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise PipelineError(stage, "ffprobe returned invalid JSON", result.stdout[:500]) from exc

    streams = probe.get("streams")
    if not isinstance(streams, list) or not any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio"
        for stream in streams
    ):
        raise PipelineError(stage, "MP3 artifact contains no audio stream", result.stdout[:500])

    duration_value = (probe.get("format") or {}).get("duration") if isinstance(probe.get("format"), dict) else None
    if duration_value not in (None, "N/A"):
        try:
            duration_seconds = float(duration_value)
        except (TypeError, ValueError):
            duration_seconds = None
        if duration_seconds is not None and duration_seconds <= 0:
            raise PipelineError(stage, "MP3 artifact has non-positive duration", result.stdout[:500])


@tracked_stage("publish_episode")
def _create_episode(
    settings: Settings,
    metadata: EpisodeMetadata,
    slug: str,
    public_audio_url: str,
    file_size_bytes: int,
    notes_url: str | None = None,
    script_url: str | None = None,
    manifest_url: str | None = None,
    description: str | None = None,
) -> dict:
    title_bits = [part for part in (metadata.channel.strip(), metadata.title.strip()) if part]
    title = " | ".join(title_bits) if title_bits else metadata.title
    # Prefer the generated compact description over the publisher's
    # promotional text; fall back only when enrichment did not run.
    body = (description or "").strip() or metadata.description.strip() or metadata.title
    payload = {
        "feed": metadata.feed,
        "slug": slug,
        "title": title,
        # Feed ordering stays on ingestion time unless the job supplied a
        # publication time (scheduled sources). Otherwise the original date
        # lives in the Markdown and manifest, where it can be honest about
        # being unknown.
        "pub_date": metadata.pub_date or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "description": body,
        "mp3_url": public_audio_url,
        "mp3_size": file_size_bytes,
        "duration": _format_duration(metadata.duration_seconds),
        "explicit": "no",
        "keywords": "auto,rss-audio-bot",
        "block": "no",
    }
    if notes_url:
        payload["md_url"] = notes_url
    if script_url:
        payload["speech_text_url"] = script_url
    if manifest_url:
        payload["manifest_url"] = manifest_url
    return _api_json(settings, "POST", "/api/episodes", payload)


def _api_json(
    settings: Settings,
    method: str,
    path: str,
    payload: dict | None = None,
) -> dict | list:
    url = f"{settings.api_base_url.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {settings.api_token}",
        "Accept": "application/json",
        "User-Agent": "rss-audio-bot/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    request = urlrequest.Request(url, data=data, headers=headers, method=method)
    # Only reads are replayed locally. An uncertain write is reconciled by
    # exact slug lookup at the start of the next controller attempt.
    attempts = 2 if method == "GET" else 1
    for attempt in range(attempts):
        current_progress().check()
        try:
            with urlrequest.urlopen(request, timeout=20 if method == "GET" else 60) as response:
                body = response.read().decode("utf-8").strip()
            break
        except urlerror.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace").strip()
            try:
                problem = json.loads(detail)
            except (ValueError, TypeError):
                problem = {}
            if not isinstance(problem, dict):
                problem = {}
            defect = problem.get("error_code") == 1101 or problem.get("retryable") is False
            retryable = not defect and (exc.code in {408, 429} or exc.code >= 500)
            error = PipelineError("api", f"{method} {path} failed with HTTP {exc.code}", detail,
                                  operation=method, http_status=exc.code,
                                  retryable=retryable, error_code=problem.get("error_code"))
        except (urlerror.URLError, TimeoutError) as exc:
            error = PipelineError("api", f"{method} {path} failed", str(getattr(exc, "reason", exc)),
                                  operation=method, retryable=True)
        if not error.retryable or attempt + 1 == attempts:
            raise error
        with current_progress().stage("publication_retry", reason="provider_wait"):
            time.sleep(random.uniform(0.5, 1.5))

    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise PipelineError("api", f"{method} {path} returned invalid JSON", body[:500],
                            operation=method, retryable=True) from exc


def _list_episodes(settings: Settings, search: str | None = None) -> list[dict]:
    path = "/api/episodes?feed=audio"
    if search:
        path += "&search=" + urlencode({"q": search})[2:]
    result = _api_json(settings, "GET", path)
    if not isinstance(result, list):
        raise PipelineError("api", "Episode list returned unexpected payload")
    return [row for row in result if isinstance(row, dict)]


@tracked_stage("check_publication")
def _get_episode_by_slug(settings: Settings, slug: str, feed: str = "audio") -> dict | None:
    feed_spec(feed)
    result = _api_json(settings, "GET", "/api/episodes?" + urlencode({"feed": feed, "slug": slug}))
    if not isinstance(result, list):
        raise PipelineError("api", "Episode lookup returned unexpected payload", operation="GET", retryable=True)
    for row in result:
        if isinstance(row, dict) and row.get("slug") == slug:
            return row
    return None


def _source_tool_settings(settings: Settings) -> SourceToolSettings:
    return SourceToolSettings(
        mac_ssh_target=settings.mac_ssh_target,
        mac_ytdlp_path=settings.mac_ytdlp_path,
        mac_cookies_from_browser=settings.mac_cookies_from_browser,
        yt_dlp_bin=settings.yt_dlp_bin,
        yt_dlp_format=settings.yt_dlp_format,
        ffmpeg_bin=settings.ffmpeg_bin,
        mullvad_proxy=settings.mullvad_proxy,
        yt_dlp_remote_components=settings.yt_dlp_remote_components,
    )


def _validate_slug(slug: str) -> str:
    """Accept any slug the naming module can mint.

    The date component is ``YYYY-MM-DD`` when the source declares a
    publication date and the literal ``undated`` when it does not (see
    shared/episode_enrichment/naming.py). The original pattern only allowed
    the dated form, so every ``undated-`` episode -- the documented
    no-source-date case -- was undeletable.

    The shape is still constrained: this value is interpolated into remote
    object keys, so path separators, dots and traversal must stay impossible.
    """
    cleaned = slug.strip()
    if not re.fullmatch(
        r"(?:\d{4}-\d{2}-\d{2}|undated)-[a-z0-9]+(?:-[a-z0-9]+)*", cleaned
    ):
        raise PipelineError("delete", "Invalid episode slug")
    return cleaned


def _write_private_text(path: Path, body: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    path.chmod(0o600)


def _run(
    args: list[str],
    *,
    stage: str,
    timeout: int | None = None,
    cwd: str | None = None,
):
    current_progress().check()
    try:
        return run_command(args, stage=stage, timeout=timeout, cwd=cwd)
    except SourceAudioError as exc:
        raise PipelineError(exc.stage, str(exc), exc.stderr) from exc


def _ensure_unique_slug(settings: Settings, slug: str) -> str:
    candidate = slug
    index = 2
    while _get_episode_by_slug(settings, candidate) is not None:
        candidate = f"{slug}-{index}"
        index += 1
    return candidate


def _checkpointed_engine(work_dir: Path | None) -> str | None:
    from episode_enrichment.pipeline import STT_ENGINE_FILE
    path = Path(work_dir) / STT_ENGINE_FILE if work_dir else None
    try:
        return (path.read_text(encoding="utf-8").strip() or None) if path and path.is_file() else None
    except OSError:
        return None


def already_published(settings: Settings, prefix: str, feed: str = "audio",
                      metadata: EpisodeMetadata | None = None, work_dir: Path | None = None) -> PublishResult | None:
    """Return this source's existing episode, if a prior attempt installed it.

    Publication is not idempotent by itself: the runtime attempt is finalized
    only *after* the upload and the episode install, so a controller outage in
    that window makes the job retry work that already completed. The old
    behaviour was to notice the occupied slug and mint ``<prefix>-2``, which
    published a duplicate episode and paid for a second full upload -- the
    exact outage the retry hardening was written for.

    A deterministic prefix ends in twelve hex characters of the source
    identity (URL hash, or content hash for uploads), so an episode sitting at
    this exact key is this same source's episode from an earlier attempt, not
    an unrelated collision. Recognising it and returning it converts the retry
    into a no-op that reports the original episode.

    It reports what the row holds, nothing guessed. A row with a script and no notes yet carries the
    same notes request the first attempt made (keyed on slug and version), so a lost submit is
    resubmitted, idempotently. Its speech-to-text engine comes from this job's work dir checkpoint when
    one survives, else it is unknown.
    """
    episode = _get_episode_by_slug(settings, prefix, feed)
    if episode is None:
        return None
    logger.info(
        "episode %s is already published; treating this attempt as a no-op", prefix)
    names = names_from_prefix(prefix)
    size = episode.get("file_size_bytes") or episode.get("fileSizeBytes") or 0
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 0
    return PublishResult(
        slug=prefix,
        title=str(episode.get("title") or names.prefix),
        public_url=(str(episode.get("audio_url") or episode.get("mp3_url") or "")
                    or build_public_audio_url(settings, prefix, feed)),
        file_size_bytes=size,
        script_url=episode.get("speech_text_url") or None,
        notes_url=episode.get("md_url") or None,
        manifest_url=episode.get("manifest_url") or None,
        notes=(_notes_for(metadata, prefix, document_version(prefix, episode["speech_text_url"]) or 1, None,
                          _checkpointed_engine(work_dir))
               if metadata is not None and episode.get("speech_text_url") and not episode.get("md_url") else None),
    )


def _format_duration(duration_seconds: int) -> str | None:
    if duration_seconds <= 0:
        return None
    hours, remainder = divmod(duration_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
