"""The Surveillance pipeline: a scheduled Bloomberg Surveillance show becomes an episode of the `surveillance` feed.

Chain test: ``tests/test_chain_surveillance.py``. ``scheduled_sources.py`` (the nightly timer) submits the shows,
with no Telegram message; when a radio window is due is this module's (``window_days``). The publish job ends
with the episode live (mp3, script and segments) and submits its notes job (``notes_job.py``), which writes the
section notes (``surveillance_notes.py``) and the page.
"""
from __future__ import annotations

import asyncio
import logging
import random
import tempfile
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import live_window
import surveillance_condensed
import surveillance_metadata
from pipeline import (JobOptions, PipelineError, RuntimeExecutionResult, _source_tool_settings, execution_result,
                      exit_rotator, ingest_live_window, ingest_local_audio, publish_link, route_health_error,
                      source_link)
from source_audio import SourceAudioError, SourceRoute, check_oci_mullvad_health
from tg_tools_common.progress import current_progress

logger = logging.getLogger(__name__)


async def run(settings, bot, input_json: dict[str, Any], options: JobOptions,
              work_dir: Path | None) -> RuntimeExecutionResult:
    if input_json.get("inputKind") == surveillance_condensed.INPUT_KIND:   # a published show's condensed edition
        work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="condensed-"))
        published = await asyncio.to_thread(
            surveillance_condensed.run, settings, input_json, work, options.enrichment_tier)
        return execution_result(published, None, options.feed)
    block = parent_block(input_json)      # the one listing switch, read before the row is created
    options = replace(options, parent_block=block)
    link = source_link(input_json)
    if "liveWindow" in input_json:   # radio
        if link:
            raise PipelineError("input", "A job takes either sourceUrl or liveWindow, not both")
        return await asyncio.to_thread(publish_window, settings, input_json, options, work_dir)
    if link is not None:   # TV: the playlist's video
        result = await asyncio.to_thread(publish_link, settings, input_json, link, work_dir, options)
        return _hands_on(result, input_json)
    capture = input_json.get("localSourcePath")   # a radio window captured earlier (surveillance_backfill.py)
    if not (isinstance(capture, str) and Path(capture).is_file()):
        raise PipelineError("input", "Unsupported runtime RSS input")
    published = await asyncio.to_thread(
        ingest_local_audio, settings, Path(capture), str(input_json.get("titleHint") or Path(capture).stem),
        work_dir=work_dir, options=options)
    return _hands_on(execution_result(published, None, options.feed), input_json)


def parent_notes(input_json: Mapping[str, Any]) -> bool:
    """The one switch: does the full show also get its own notes? (``parentNotes`` in the source's entry.)

    Off unless the source's own ``config/scheduled-sources.json`` entry says ``"parentNotes": true`` (owner ask
    A-24 is pending; the default is to drop it, since the interviews-only edition is what the feed is for).
    An inventory that cannot be read, or a job whose source key names no source, stays off and says so.
    """
    key = str(input_json.get("sourceKey") or "")
    source_id = key.split(":", 1)[0]
    try:
        source = surveillance_metadata.source(source_id)
    except (KeyError, ValueError) as exc:
        logger.warning("parentNotes: %s; the full show's notes job is not submitted", exc)
        return False
    return source.get("parentNotes") is True


def parent_block(input_json: Mapping[str, Any]) -> str:
    """The full show's own ``block``: ``"yes"`` when its source's entry says ``"parentListed": false``.

    The second switch, read the way ``parentNotes`` is (the source's entry in ``config/scheduled-sources.json``).
    A hidden row is still published, uploaded and stored: the condensed edition reads its script, timed
    transcript and mp3 (``surveillance_condensed``). An inventory that cannot be read, or a job whose source
    key names no source, leaves the full show listed and says so.
    """
    key = str(input_json.get("sourceKey") or "")
    try:
        source = surveillance_metadata.source(key.split(":", 1)[0])
    except (KeyError, ValueError) as exc:
        logger.warning("parentListed: %s; the full show stays listed", exc)
        return "no"
    block = surveillance_condensed.parent_block(source)
    if block == "yes":
        logger.info("parentListed is off in its source's entry: the full show's row is published but not listed")
    return block


def _hands_on(result: RuntimeExecutionResult, input_json: Mapping[str, Any]) -> RuntimeExecutionResult:
    """What the published show hands the job layer: its condensed edition's job, and (by the switch) its notes.

    A show published without a script (enrichment off, or speech-to-text failed) has no script and no timed
    transcript in the store, so its condensed job could only fail, terminally, as a dead row in telemetry:
    none is submitted, and one line says why (C2).

    The notes input is the provenance the condensed job needs (its date basis, the original's link, the STT
    engine), so it is read before the switch drops it.
    """
    if result.script_url:
        condensed = surveillance_condensed.parent_job_input(result, input_json)
    else:
        condensed = None
        logger.info("%s: published with no script (script_url is empty), so no condensed job is submitted: "
                    "the cut needs the parent's script and its timed transcript", result.slug)
    keep_notes = parent_notes(input_json)
    if not keep_notes:
        logger.info("%s: parentNotes is off in its source's entry; the full show gets no notes job",
                    result.slug)
    return replace(result, notes=result.notes if keep_notes else None, condensed=condensed)


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

    published = execution_result(
        ingest_live_window(
            settings, source_url=window.source_url, identity_text=window.identity,
            fetch_audio=fetch_audio, work_dir=work_dir, options=options),
        window.source_url, options.feed)
    return _hands_on(published, input_json)


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
    return fields
