"""The Surveillance pipeline: a scheduled Bloomberg Surveillance show becomes an episode of the `surveillance` feed.

Chain test: ``tests/test_chain_surveillance.py``. ``scheduled_sources.py`` (the nightly timer) submits the shows,
with no Telegram message; when a radio window is due is this module's (``window_days``). The publish job ends with the episode live (mp3, script and segments); its notes job (``notes_job.py``) cuts the notes
at the broadcast's own seams (``surveillance_seams.py``), then adds the page.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import live_window
from pipeline import (JobOptions, PipelineError, RuntimeExecutionResult, _source_tool_settings, execution_result,
                      exit_rotator, ingest_live_window, ingest_local_audio, publish_link, route_health_error,
                      source_link)
from source_audio import SourceAudioError, SourceRoute, check_oci_mullvad_health
from surveillance_seams import SEGMENTERS
from tg_tools_common.progress import current_progress

logger = logging.getLogger(__name__)


async def run(settings, bot, input_json: dict[str, Any], options: JobOptions,
              work_dir: Path | None) -> RuntimeExecutionResult:
    link = source_link(input_json)
    if "liveWindow" in input_json:   # radio
        if link:
            raise PipelineError("input", "A job takes either sourceUrl or liveWindow, not both")
        return await asyncio.to_thread(publish_window, settings, input_json, options, work_dir)
    if link is not None:   # TV: the playlist's video
        return await asyncio.to_thread(publish_link, settings, input_json, link, work_dir, options)
    capture = input_json.get("localSourcePath")   # a radio window captured earlier (surveillance_backfill.py)
    if not (isinstance(capture, str) and Path(capture).is_file()):
        raise PipelineError("input", "Unsupported runtime RSS input")
    published = await asyncio.to_thread(
        ingest_local_audio, settings, Path(capture), str(input_json.get("titleHint") or Path(capture).stem),
        work_dir=work_dir, options=options)
    return execution_result(published, None, options.feed)


def named_segmenter(name: str | None) -> Callable | None:
    """The notes segmenter a job names (``segmenter``); an unknown name is the input's error, before any read."""
    if not name:
        return None
    if name not in SEGMENTERS:
        raise PipelineError("input", f"unknown segmenter {name!r} (known: {', '.join(sorted(SEGMENTERS))})",
                            retryable=False)
    return SEGMENTERS[name]


def publish_window(settings, input_json: dict[str, Any], options: JobOptions,
                   work_dir: Path | None) -> RuntimeExecutionResult:
    """Publish a window of a live stream, once its opening is heard. OCI Mullvad route only: the stream URL
    is bound to its egress."""
    try:
        window = live_window.parse_window(input_json["liveWindow"])
    except live_window.LiveWindowError as exc:
        raise PipelineError("input", str(exc)) from exc
    if input_json.get("preferredRoute") not in (None, "", SourceRoute.OCI_MULLVAD.value):
        raise PipelineError("input", "liveWindow is only available on the oci_mullvad route")
    health = check_oci_mullvad_health(_source_tool_settings(settings))
    if not health.ok:
        raise route_health_error(SourceRoute.OCI_MULLVAD, health.reason, health.detail)

    rotator = exit_rotator(settings)

    def fetch_audio(staging: Path) -> Path:
        # Like a URL job, an IP-shaped refusal from yt-dlp (bot check, rate limit) rotates the Mullvad exit and
        # tries again. Only the stream lookup is retried that way: its URL is bound to the exit that resolved it.
        for attempt in range(rotator.attempts()):
            try:
                with current_progress().stage("download_audio"):
                    path, _duration = live_window.fetch_live_window(
                        _source_tool_settings(settings), window, staging, ffprobe_bin=settings.ffprobe_bin,
                        transcribe_clip=lambda clip: _transcribe_opening(settings, clip),
                        check=current_progress().check)
                return path
            except live_window.LiveWindowError as exc:
                raise PipelineError(exc.error_class, str(exc), exc.stderr, retryable=exc.retryable) from exc
            except SourceAudioError as exc:
                if (exc.stage == "metadata" and attempt + 1 < rotator.attempts()
                        and rotator.eligible(f"{exc}\n{exc.stderr}") and rotator.rotate(attempt)):
                    delay = random.uniform(*rotator.jitter_sec)
                    logger.info("mullvad egress rotated after %s; retrying the stream lookup in %.1fs", exc.stage, delay)
                    with current_progress().stage("source_retry", retrySeconds=delay, reason="source_retry"):
                        time.sleep(delay)
                    continue
                raise PipelineError(exc.stage, str(exc), exc.stderr) from exc
        raise PipelineError("source", "No stream lookup was attempted")

    published = ingest_live_window(
        settings, source_url=window.source_url, identity_text=window.identity,
        fetch_audio=fetch_audio, work_dir=work_dir, options=options)
    return execution_result(published, window.source_url, options.feed)


def _transcribe_opening(settings, clip: Path) -> str:
    """The opening clip's transcript, by the path enrichment uses (self-hosted first, Azure fallback)."""
    from episode_enrichment import transcribe_audio
    try:
        return str(transcribe_audio(clip, model=settings.enrichment_stt_model, jobs=settings.enrichment_jobs))
    except Exception as exc:
        raise live_window.LiveWindowError("window_transcribe", f"opening check could not transcribe: {exc}",
                                          retryable=True) from exc


WINDOW_FIELDS = ("videoId", "timezone", "weekdays", "start", "seconds", "expectOpening", "settleMinutes")


def check_window_source(source: Mapping[str, Any]) -> None:
    """The radio fields of a ``youtube_live_window`` source (``scheduled_sources.py`` checks the rest).
    ValueError (KeyError for an unknown zone) names the first one that is wrong."""
    missing = sorted(set(WINDOW_FIELDS) - set(source))
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    ZoneInfo(source["timezone"])
    clock_time.fromisoformat(source["start"])
    for field in ("seconds", "settleMinutes"):
        if isinstance(source[field], bool) or not isinstance(source[field], int) or source[field] < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    weekdays = source["weekdays"]
    if not (isinstance(weekdays, list) and weekdays and all(isinstance(day, int) and 1 <= day <= 7 for day in weekdays)):
        raise ValueError("weekdays must be ISO weekday numbers 1 (Mon) .. 7 (Sun)")


def window_start(source: Mapping[str, Any], day: date) -> datetime:
    return datetime.combine(day, clock_time.fromisoformat(source["start"]),
                            tzinfo=ZoneInfo(source["timezone"])).astimezone(UTC)


def window_days(source: Mapping[str, Any], now: datetime, since: date) -> list[date]:
    """The air days due by ``now``: its weekdays in its zone, from ``since`` and within the lookback, once the
    window ended ``settleMinutes`` ago."""
    today = now.astimezone(ZoneInfo(source["timezone"])).date()
    days = []
    for back in range(source["lookbackDays"], -1, -1):
        day = today - timedelta(days=back)
        if day < since or day.isoweekday() not in source["weekdays"]:
            continue
        if now < window_start(source, day) + timedelta(seconds=source["seconds"], minutes=source["settleMinutes"]):
            continue
        days.append(day)
    return days


def window_fields(source: Mapping[str, Any], start: str) -> dict[str, Any]:
    """What one air day's publish job carries besides the scheduled item's own fields; ``start`` is ISO UTC."""
    fields: dict[str, Any] = {"liveWindow": {"videoId": source["videoId"], "start": start, "seconds": source["seconds"],
                                             "expectOpening": source["expectOpening"]}}
    if source.get("segmenter"):   # hour labels on the window's wall clock
        fields["segmentContext"] = {"start": start, "timezone": source["timezone"]}
    return fields
