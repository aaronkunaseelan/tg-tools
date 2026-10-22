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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlencode

from config import Settings
from tg_tools_common.progress import current_progress, tracked_stage
from tg_tools_common.rclone_upload import immutable_copy_argv
from episode_enrichment import (EpisodeNames, EpisodeNamingMetadata,
                                build_episode_names, names_from_prefix)
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


@dataclass(frozen=True)
class PublishResult:
    slug: str
    title: str
    public_url: str
    file_size_bytes: int
    script_url: str | None = None
    notes_url: str | None = None
    manifest_url: str | None = None


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


def _names_with_prefix(names: EpisodeNames, prefix: str) -> EpisodeNames:
    return EpisodeNames(prefix, f"{prefix}.mp3", f"{prefix}.script.md",
                        f"{prefix}.notes.md", f"{prefix}.manifest.json")


def build_public_audio_url(settings: Settings, slug: str) -> str:
    return f"{settings.public_audio_base_url}/{slug}.mp3"


def render_completion_message(
    title_or_slug: str,
    public_url: str,
    notes_url: str | None = None,
    script_url: str | None = None,
    source_url: str | None = None,
) -> str:
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


def delete_episode(settings: Settings, slug: str) -> None:
    slug = _validate_slug(slug)
    episode = _get_episode_by_slug(settings, slug)
    if episode is None:
        raise PipelineError("delete", f"No episode found for slug `{slug}`")

    _api_json(settings, "DELETE", f"/api/episodes/{episode['id']}")
    # Every artifact the publisher uploaded for this slug, not just the audio.
    # Since the script/notes split an episode owns four objects; deleting only
    # the mp3 left three orphans in the bucket per deletion.
    names = names_from_prefix(slug)
    failures: list[str] = []
    for file_name in (names.audio_name, names.script_name,
                      names.notes_name, names.manifest_name):
        remote = f"{settings.b2_remote}:{settings.b2_bucket}/audio/episodes/{file_name}"
        try:
            _run([settings.rclone_bin, "delete", remote], stage="delete")
        except PipelineError as exc:
            # Older episodes predate some of these artifacts, and the API row
            # is already gone. Collect and report rather than aborting
            # part-way through and leaving a worse mess than we started with.
            failures.append(f"{file_name}: {exc}")
    if failures:
        logger.warning("delete_episode(%s) left artifacts behind: %s",
                       slug, "; ".join(failures))


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


def ingest_url_with_metadata(
    settings: Settings,
    url: str,
    metadata: EpisodeMetadata,
    *,
    route: SourceRoute = SourceRoute.OCI_MULLVAD,
    work_dir: Path | None = None,
) -> PublishResult:
    identity = hashlib.sha256(url.encode("utf-8")).hexdigest()
    names = _episode_names(metadata, identity)
    published = already_published(settings, names.prefix)
    if published is not None:
        return published
    names = _names_with_prefix(names, names.prefix)
    with _staging_dir(work_dir) as staging:
        mp3_path = staging / names.audio_name
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            _download_url_to_mp3(settings, url, mp3_path, route=route)
        return _finalize_publish(settings, metadata, names, mp3_path, staging)


def ingest_direct_audio_url(
    settings: Settings,
    url: str,
    *,
    work_dir: Path | None = None,
) -> PublishResult:
    metadata = fetch_metadata(settings, url, route=SourceRoute.OCI_MULLVAD)
    identity = hashlib.sha256(url.encode("utf-8")).hexdigest()
    names = _episode_names(metadata, identity)
    published = already_published(settings, names.prefix)
    if published is not None:
        return published
    names = _names_with_prefix(names, names.prefix)
    with _staging_dir(work_dir) as staging:
        mp3_path = staging / names.audio_name
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            # Never let ffmpeg fetch a remote URL directly: stage it through
            # the same explicit Mullvad yt-dlp route as extractor sources.
            _download_url_to_mp3(settings, url, mp3_path, route=SourceRoute.OCI_MULLVAD)
        return _finalize_publish(settings, metadata, names, mp3_path, staging)


def ingest_local_audio(
    settings: Settings,
    source_path: Path,
    title_hint: str | None = None,
    *,
    work_dir: Path | None = None,
) -> PublishResult:
    base_title = title_hint or source_path.stem or "audio"
    metadata = EpisodeMetadata(
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
    )
    try:
        identity = hashlib.sha256(source_path.read_bytes()).hexdigest()
    except OSError:
        # Admission normally guarantees a staged file. Keep metadata-only
        # planning/test callers deterministic without pretending the bytes were read.
        identity = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()
    names = _episode_names(metadata, identity)
    published = already_published(settings, names.prefix)
    if published is not None:
        return published
    names = _names_with_prefix(names, names.prefix)
    with _staging_dir(work_dir) as staging:
        mp3_path = staging / names.audio_name
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            _convert_input_to_mp3(settings, str(source_path), mp3_path, stage="convert")
        return _finalize_publish(settings, metadata, names, mp3_path, staging)


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


def build_public_notes_url(settings: Settings, names: EpisodeNames) -> str:
    return f"{settings.public_md_base_url}/{names.notes_name}"


def build_public_script_url(settings: Settings, names: EpisodeNames) -> str:
    return f"{settings.public_md_base_url}/{names.script_name}"


def build_public_md_url(settings: Settings, slug: str) -> str:
    return f"{settings.public_md_base_url}/{slug}.notes.md"


def build_public_manifest_url(settings: Settings, names: EpisodeNames | str) -> str:
    name = names.manifest_name if isinstance(names, EpisodeNames) else f"{names}.manifest.json"
    return f"{settings.public_md_base_url}/{name}"


def _enrich_episode(
    settings: Settings,
    metadata: EpisodeMetadata,
    names: EpisodeNames,
    mp3_path: Path,
    work_dir: Path | None,
):
    """Run transcript enrichment, or return None when it is not in play.

    Raises PipelineError('enrichment') when enrichment fails *and* is required,
    so the episode is never installed pointing at a transcript that does not
    exist.
    """
    if not settings.enrichment_enabled:
        return None

    try:
        from episode_enrichment import AudioSttSource, enrich
    except Exception as exc:
        if settings.enrichment_required:
            raise PipelineError(
                "enrichment", f"transcript enrichment is unavailable: {exc}") from exc
        logger.warning("enrichment unavailable; publishing audio only: %s", exc)
        return None

    target = Path(work_dir) if work_dir else mp3_path.parent / "enrichment"
    try:
        return enrich(
            source=AudioSttSource(
                mp3_path, stt_model=settings.enrichment_stt_model,
                jobs=settings.enrichment_jobs),
            work_dir=target,
            title=metadata.title,
            slug=names.prefix,
            published=metadata.source_published_at,
            date_basis=metadata.date_basis,
            duration_seconds=metadata.duration_seconds,
            mp3_url=build_public_audio_url(settings, names.prefix),
            notes_url=build_public_notes_url(settings, names),
            script_url=build_public_script_url(settings, names),
            source_url=metadata.source_url,
            stt_model=settings.enrichment_stt_model,
            jobs=settings.enrichment_jobs,
        )
    except Exception as exc:
        if settings.enrichment_required:
            stage = getattr(exc, "stage", "run")
            raise PipelineError("enrichment", f"{stage}: {exc}") from exc
        logger.warning("enrichment failed; publishing audio only: %s", exc)
        return None


def _finalize_publish(
    settings: Settings,
    metadata: EpisodeMetadata,
    names: EpisodeNames | str,
    mp3_path: Path,
    work_dir: Path | None = None,
) -> PublishResult:
    _validate_mp3_artifact(settings, mp3_path)
    if isinstance(names, str):
        names = _names_with_prefix(
            EpisodeNames(names, "", "", "", ""), names)

    # Script and notes are complete before the first upload. A failed analysis
    # can therefore never install an episode with missing documents.
    enriched = _enrich_episode(settings, metadata, names, mp3_path, work_dir)
    artifact_dir = Path(work_dir) if work_dir else mp3_path.parent
    script_path = notes_path = manifest_path = None
    if enriched is not None:
        script_path = artifact_dir / names.script_name
        notes_path = artifact_dir / names.notes_name
        manifest_path = artifact_dir / names.manifest_name
        script_path.write_text(enriched.script_markdown, encoding="utf-8")
        notes_path.write_text(enriched.notes_markdown, encoding="utf-8")
        manifest_path.write_text(enriched.manifest, encoding="utf-8")

    uploaded_keys: list[str] = []
    public_audio_url = build_public_audio_url(settings, names.prefix)
    script_url = notes_url = manifest_url = None
    try:
        _upload_mp3(settings, mp3_path)
        uploaded_keys.append(f"audio/episodes/{names.audio_name}")
        if enriched is not None:
            assert script_path is not None and notes_path is not None and manifest_path is not None
            for local_path, file_name in (
                (script_path, names.script_name),
                (notes_path, names.notes_name),
                (manifest_path, names.manifest_name),
            ):
                key = f"audio/episodes/{file_name}"
                _upload_artifact(settings, local_path, key)
                uploaded_keys.append(key)
            script_url = build_public_script_url(settings, names)
            notes_url = build_public_notes_url(settings, names)
            manifest_url = build_public_manifest_url(settings, names)

        try:
            _create_episode(
                settings=settings,
                metadata=metadata,
                slug=names.prefix,
                public_audio_url=public_audio_url,
                file_size_bytes=mp3_path.stat().st_size,
                notes_url=notes_url,
                script_url=script_url,
                manifest_url=manifest_url,
                description=enriched.description if enriched is not None else None,
            )
        except PipelineError as exc:
            # A lost response (or concurrent create conflict) is resolved by
            # reading the deterministic identity, never by replaying the POST.
            if exc.dependency == "publication" and exc.operation == "POST" and (
                exc.retryable or exc.http_status in {400, 409} or exc.error_code == 1101
            ):
                published = already_published(settings, names.prefix)
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
        notes_url=notes_url,
        manifest_url=manifest_url,
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
def _upload_mp3(settings: Settings, mp3_path: Path) -> None:
    remote = f"{settings.b2_remote}:{settings.b2_bucket}/audio/episodes/"
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
        "feed": "audio",
        "slug": slug,
        "title": title,
        # Feed ordering stays on ingestion time. The original publication date
        # lives in the Markdown and manifest, where it can be honest about
        # being unknown.
        "pub_date": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
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
def _get_episode_by_slug(settings: Settings, slug: str) -> dict | None:
    result = _api_json(settings, "GET", "/api/episodes?" + urlencode({"feed": "audio", "slug": slug}))
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


def already_published(settings: Settings, prefix: str) -> PublishResult | None:
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
    """
    episode = _get_episode_by_slug(settings, prefix)
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
                    or build_public_audio_url(settings, prefix)),
        file_size_bytes=size,
        script_url=(episode.get("speech_text_url") or episode.get("script_url")
                    or build_public_script_url(settings, names)),
        notes_url=(episode.get("md_url") or episode.get("notes_url")
                   or build_public_notes_url(settings, names)),
        manifest_url=(episode.get("manifest_url")
                      or build_public_manifest_url(settings, names)),
    )


def _format_duration(duration_seconds: int) -> str | None:
    if duration_seconds <= 0:
        return None
    hours, remainder = divmod(duration_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
