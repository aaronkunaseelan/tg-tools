from __future__ import annotations

import asyncio
import contextlib
import html
import hashlib
from datetime import datetime, timezone
import logging
import os
import random
import shutil
import signal
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, unquote
from typing import Any

from telegram import Bot
from tg_tools_common.lease import AttemptLease, LeaseLost
from tg_tools_common.progress import ProgressReporter, NullProgress, current_progress, use_progress

from config import Settings, load_settings, validate_settings, PUBLISH_LEASE_SECONDS
import episodes
import notes_job
import rebuild
import surveillance
from pipeline import (PIPELINES, PipelineError, RuntimeExecutionResult, enrichment_preflight, execution_result,
                      is_retryable, job_options, source_link)
from runtime_client import RuntimeClient, RuntimeClientError
from runtime_jobs import DeliveryTargetMissing, confirmation_delivery_payload, delivery_disabled
from source_audio import stable_url_for_display
from work_dirs import cleanup_work_dirs, job_work_dir

logger = logging.getLogger(__name__)

IDLE_POLL_SEC = 5
MAX_RETRY_DELAY_SEC = 120
_WORK_DIR_SWEEP_INTERVAL_SEC = 3600


def _retry_delay(consecutive_failures: int) -> float:
    """Idle cadence when healthy, capped exponential backoff when not.

    A flat 5s poll during a multi-hour controller outage is a lot of pointless
    load for both sides. Jitter keeps several workers from resynchronising
    into a thundering herd when the controller comes back.
    """
    if consecutive_failures <= 0:
        return IDLE_POLL_SEC
    delay = min(IDLE_POLL_SEC * (2 ** (consecutive_failures - 1)), MAX_RETRY_DELAY_SEC)
    return delay * (0.5 + random.random() / 2)


async def execute_runtime_job(
    settings: Settings,
    bot: Bot,
    input_json: dict[str, Any],
    work_dir: Path | None = None,
) -> RuntimeExecutionResult:
    """A publish runs its feed's pipeline (``episodes.run``, ``surveillance.run``); a notes, page or rebuild job
    is a later step both share. The configuration is checked before any paid step. A later step's
    ``publishedAt`` is the episode's date as stored (a bare extractor date is fine), not a publish option."""
    kind = input_json.get("inputKind")
    later = kind in (notes_job.INPUT_KIND, notes_job.PAGE_KIND, rebuild.INPUT_KIND)
    options = job_options({k: v for k, v in input_json.items() if k != "publishedAt"} if later else input_json)
    options = replace(options, enrichment_tier=enrichment_preflight(settings, options.feed))
    if not later:
        pipeline = surveillance if PIPELINES[options.feed] == "surveillance" else episodes
        return await pipeline.run(settings, bot, input_json, options, work_dir)
    work = work_dir or job_work_dir(settings.work_dir, kind)
    if kind == notes_job.INPUT_KIND:
        published = await asyncio.to_thread(
            notes_job.run_notes, settings, input_json, work, options.enrichment_tier)
    elif kind == notes_job.PAGE_KIND:
        published = await asyncio.to_thread(notes_job.run_page, settings, input_json, work)
    else:
        published = await asyncio.to_thread(rebuild.run_rebuild, settings, input_json, work, options.enrichment_tier)
    return execution_result(published, None, options.feed)


def _result_id(finalized: dict[str, Any]) -> str | None:
    result = finalized.get("result") if isinstance(finalized, dict) else None
    if isinstance(result, dict) and result.get("resultId"):
        return str(result["resultId"])
    return None


def _source_url_display(input_json: dict[str, Any]) -> str | None:
    try:
        link = source_link(input_json)
    except PipelineError:
        return None
    return stable_url_for_display(link[1]) if link else None


async def process_claim(*, client, settings, bot, claim, worker_id=None):
    job, attempt = claim["job"], claim["attempt"]
    remaining = 7200
    if attempt.get("leaseExpiresAt"):
        remaining = max(0, (datetime.fromisoformat(attempt["leaseExpiresAt"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
    lease = AttemptLease(client=client, job_id=job["jobId"], attempt_id=attempt["attemptId"],
                         lease_token=attempt["leaseToken"], lease_seconds=7200, initial_remaining=remaining,
                         worker_beat=None if worker_id is None else lambda: client.heartbeat_worker(
                             worker_id=worker_id, metadata_json={"phase": "busy", "jobId": job["jobId"]}))
    lease.start()
    enabled = os.environ.get("TG_TOOLS_PROGRESS_ENABLED") == "1"
    reporter = ProgressReporter(send=client.append_progress, job_id=job["jobId"], attempt_id=attempt["attemptId"],
                                lease_token=attempt["leaseToken"], check=lease.check).start() if enabled else NullProgress()
    # Authority checks remain active even when optional progress is disabled.
    reporter.check = lease.check
    reporter.authority_guard = True
    try:
        with use_progress(reporter):
            await _process_claim_inner(client=client, settings=settings, bot=bot, claim=claim)
    except LeaseLost:
        logger.warning("RSS attempt lost authority job=%s; leaving reconciliation to controller", job["jobId"])
    finally:
        if enabled: reporter.close()
        lease.close()


async def _process_claim_inner(
    *,
    client: RuntimeClient,
    settings: Settings,
    bot: Bot,
    claim: dict[str, Any],
) -> None:
    job = dict(claim["job"])
    attempt = dict(claim["attempt"])
    input_json = dict(job.get("inputJson") or {})
    job_id = str(job["jobId"])
    attempt_id = str(attempt["attemptId"])
    lease_token = str(attempt["leaseToken"])

    # Durable per-job directory: a retry reuses completed enrichment stages
    # instead of re-transcribing audio that already succeeded.
    work_dir = job_work_dir(settings.work_dir, job_id)

    await asyncio.to_thread(client.mark_running, job_id=job_id, attempt_id=attempt_id, lease_token=lease_token)
    # Every exit from here must finalize the attempt. An unfinalized claim is
    # invisible to the operator and blocks the job for the whole 7200s lease,
    # so the outer guard converts any unmodelled failure into a bounded retry
    # rather than a silent two-hour stall.
    finalized: dict[str, Any] | None = None
    try:
        try:
            result = await execute_runtime_job(settings, bot, input_json, work_dir)
            notes_job_id = None
            if result.notes is not None:
                notes_job_id = await asyncio.to_thread(_submit_notes, client, job, result.notes)
            condensed_job_id = None
            if result.condensed is not None:
                condensed_job_id = await asyncio.to_thread(_submit_condensed, client, job, result.condensed)
        except PipelineError as exc:
            current_progress().check()
            outcome = "retryable_error" if is_retryable(
                exc, unattended=delivery_disabled(input_json)) else "terminal_error"
            finalized = await asyncio.to_thread(
                client.finalize,
                {
                    "jobId": job_id,
                    "attemptId": attempt_id,
                    "leaseToken": lease_token,
                    "outcome": outcome,
                    "errorClass": exc.stage,
                    "errorText": str(exc),
                    "resultJson": {"ok": False, "stage": exc.stage, "message": str(exc), "error": exc.diagnostic(), "completionHtml": f"FAILED at {html.escape(exc.stage)}: {html.escape(str(exc))}"},
                    "backoffSec": 300,
                },
            )
            if outcome == "terminal_error":
                await _deliver(
                    client, job_id=job_id, input_json=input_json,
                    finalized=finalized,
                    html_text=f"FAILED at {html.escape(exc.stage)}: {html.escape(str(exc))}")
            return

        artifacts = []
        if os.environ.get("TG_TOOLS_PROGRESS_ENABLED") == "1":
            artifacts, registrable = await asyncio.to_thread(
                _register_published_artifacts, client, result, work_dir, job_id, attempt_id)
        if hasattr(current_progress(), "flush"):
            await asyncio.to_thread(current_progress().flush)
        current_progress().check()
        finalized = await asyncio.to_thread(
            client.finalize,
            {
                "jobId": job_id,
                "attemptId": attempt_id,
                "leaseToken": lease_token,
                "outcome": "succeeded",
                "resultJson": {
                    "ok": True,
                    "slug": result.slug,
                    "title": result.title,
                    "publicUrl": result.public_url,
                    "fileSizeBytes": result.file_size_bytes,
                    "sourceUrlDisplay": _source_url_display(input_json),
                    "completionHtml": result.completion_html,
                    "scriptUrl": result.script_url,
                    "notesUrl": result.notes_url,
                    "manifestUrl": result.manifest_url,
                    "segmentsUrl": result.segments_url,
                    "pageUrl": result.page_url,
                    "notesJobId": notes_job_id,
                    "condensedJobId": condensed_job_id,
                    "artifactIds": artifacts,
                },
            },
        )
        await _deliver(
            client, job_id=job_id, input_json=input_json, finalized=finalized,
            html_text=result.completion_html)
        # Retain source/checkpoints if registration is degraded; publication
        # and notification success must not depend on the optional catalog.
        if os.environ.get("TG_TOOLS_PROGRESS_ENABLED") != "1" or len(artifacts) == registrable:
            shutil.rmtree(work_dir, ignore_errors=True)
    except LeaseLost:
        raise
    except BaseException as exc:
        if finalized is None:
            logger.exception(
                "Unmodelled failure processing job %s; finalizing as retryable", job_id)
            await _finalize_unexpected(
                client, job_id=job_id, attempt_id=attempt_id,
                lease_token=lease_token, exc=exc)
        raise


def _submit_notes(client, job: dict[str, Any], notes: dict[str, Any]) -> str:
    """Submit the episode's notes job (notes_job.py).

    A failure is retryable: the retry reads the published episode back and submits the same key again.
    """
    request = dict(notes)
    try:
        submitted = client.submit_job(notes_job.notes_job_payload(job, request))
    except Exception as exc:
        raise PipelineError("notes_submit", f"could not submit the notes job: {exc}", retryable=True) from exc
    return str(((submitted or {}).get("job") or {}).get("jobId") or "")


def _submit_condensed(client, job: dict[str, Any], condensed: dict[str, Any]) -> str:
    """Submit the published show's condensed-edition job (surveillance_condensed.py).

    A failure is retryable, like the notes submit: the retry reads the published episode back and submits the
    same key again (the key is the parent and its version).
    """
    import surveillance_condensed
    try:
        submitted = client.submit_job(surveillance_condensed.condensed_job_payload(job, condensed))
    except Exception as exc:
        raise PipelineError("condensed_submit", f"could not submit the condensed job: {exc}",
                            retryable=True) from exc
    return str(((submitted or {}).get("job") or {}).get("jobId") or "")


def _register_published_artifacts(client, result, work_dir, job_id, attempt_id):
    """(registered artifact ids, how many of the result's documents this job holds in its work dir)."""
    registered, registrable = [], 0
    for url, kind, mime in [(result.public_url,"rss_audio","audio/mpeg"),
                            (result.script_url,"rss_script","text/markdown"),
                            (result.segments_url,"rss_segments","application/json"),
                            (result.page_url,"rss_page","text/html"),
                            (result.notes_url,"rss_notes","text/markdown"),
                            (result.manifest_url,"rss_manifest","application/json")]:
        if not url: continue
        name = unquote(urlsplit(url).path.rsplit("/",1)[-1])
        path = work_dir / name
        if Path(name).name != name or not path.is_file(): continue
        registrable += 1
        current_progress().check()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b""): digest.update(chunk)
        try:
            response = client.register_artifact(dict(artifactKind=kind,storageUri=url,contentHash=digest.hexdigest(),
                byteSize=path.stat().st_size,mimeType=mime,retentionClass="published",producerSystem="rss-audio",
                createdByJobId=job_id,createdByAttemptId=attempt_id))
            registered.append(response["artifact"]["artifactId"])
        except Exception:
            logger.warning("published artifact catalog registration deferred job=%s kind=%s",job_id,kind)
    return registered, registrable


async def _finalize_unexpected(
    client: RuntimeClient,
    *,
    job_id: str,
    attempt_id: str,
    lease_token: str,
    exc: BaseException,
) -> None:
    """Best-effort finalize for a failure no handler modelled.

    Retryable, not terminal: an unmodelled exception says nothing about the
    input, and the runtime already bounds retries by attempt cap and deadline.
    Swallows its own errors -- if the controller is unreachable there is
    nothing further this attempt can do, and raising here would mask the
    original failure.
    """
    try:
        await asyncio.to_thread(
            client.finalize,
            {
                "jobId": job_id,
                "attemptId": attempt_id,
                "leaseToken": lease_token,
                "outcome": "retryable_error",
                "errorClass": "unexpected_error",
                "errorText": f"{type(exc).__name__}: {exc}"[:2000],
                "resultJson": {
                    "ok": False,
                    "stage": "unexpected",
                    "message": f"{type(exc).__name__}: {exc}"[:2000],
                },
                "backoffSec": 300,
            },
        )
    except Exception:
        logger.exception("Could not finalize unmodelled failure for job %s", job_id)


async def _deliver(
    client: RuntimeClient,
    *,
    job_id: str,
    input_json: dict[str, Any],
    finalized: dict[str, Any],
    html_text: str,
    attempts: int = 3,
) -> None:
    """Append the Telegram confirmation row for an already-finalized job.

    The job is finalized before this runs, so it will never be retried: a
    failure here means the user silently never hears back. Retry in-loop, and
    when the job simply has no reply target, log and move on rather than
    letting the delivery failure escape into the unmodelled path.
    """
    if delivery_disabled(input_json):
        logger.info("Job %s finalized; no delivery requested (delivery=none)", job_id)
        return
    try:
        delivery = confirmation_delivery_payload(
            job_id=job_id,
            result_id=_result_id(finalized),
            input_json=input_json,
            html_text=html_text,
        )
    except DeliveryTargetMissing as exc:
        logger.error("Job %s finalized but has no Telegram reply target: %s", job_id, exc)
        return

    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(client.append_delivery, delivery)
            return
        except Exception:
            if attempt == attempts:
                logger.exception(
                    "Job %s finalized but its confirmation could not be queued "
                    "after %d attempts; durable result projection will retry",
                    job_id, attempts)
                return
            await asyncio.sleep(min(2 ** attempt, 10))


async def worker_loop(settings: Settings) -> None:
    client = RuntimeClient(settings)
    bot = Bot(settings.bot_token)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async def project_results():
        from result_projector import RSSResultProjector
        projector = RSSResultProjector(client)
        while not stop.is_set():
            try: await asyncio.to_thread(projector.project_once)
            except Exception: logger.exception("RSS result projection failed")
            try: await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError: pass
    projector_task = asyncio.create_task(project_results())
    worker_id = settings.runtime_worker_id
    registered = False
    # Drop stale work dirs left by crashed or abandoned jobs before claiming.
    await asyncio.to_thread(cleanup_work_dirs, settings.work_dir)
    # A worker stays up for weeks, so a startup-only sweep lets failed and
    # dead-lettered jobs accumulate forever. Re-sweep on a slow cadence.
    last_sweep = time.monotonic()
    consecutive_failures = 0
    try:
        while not stop.is_set():
            if time.monotonic() - last_sweep >= _WORK_DIR_SWEEP_INTERVAL_SEC:
                last_sweep = time.monotonic()
                try:
                    await asyncio.to_thread(cleanup_work_dirs, settings.work_dir)
                except Exception:
                    logger.exception("Periodic work-dir sweep failed")
            try:
                if not registered:
                    await asyncio.to_thread(
                        client.register_worker,
                        worker_id=worker_id,
                        worker_venue="oci",
                        capacity=1,
                        supported_execution_profiles=("cpu_media",),
                        workload_types=("rss_publish",),
                        producer_systems=("rss-audio",),
                        allowed_queue_classes=("foreground",),
                        queue_preference=("foreground",),
                        metadata_json={"component": "rss-audio-runtime-worker", "pid": os.getpid()},
                    )
                    registered = True
                await asyncio.to_thread(client.heartbeat_worker, worker_id=worker_id, metadata_json={"phase": "idle"})
                claim = await asyncio.to_thread(client.claim_job, worker_id=worker_id, lease_duration_sec=PUBLISH_LEASE_SECONDS)
                consecutive_failures = 0
                if claim:
                    await process_claim(client=client, settings=settings, bot=bot, claim=claim, worker_id=worker_id)
                    continue
            except RuntimeClientError as exc:
                consecutive_failures += 1
                # A controller outage lasts minutes to hours. Logging a full stack
                # trace every 5s buries the journal and hammers the API, so only
                # the first failure in a run carries the traceback.
                if consecutive_failures == 1:
                    logger.exception("Runtime API unavailable; RSS worker will retry")
                else:
                    logger.warning(
                        "Runtime API still unavailable (%d consecutive): %s",
                        consecutive_failures, exc)
                registered = False
            except Exception:
                consecutive_failures += 1
                logger.exception("Unexpected RSS runtime worker failure")

            try:
                await asyncio.wait_for(stop.wait(), timeout=_retry_delay(consecutive_failures))
            except asyncio.TimeoutError:
                pass
    finally:
        projector_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await projector_task


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    validate_settings(settings)
    if not settings.bot_token:
        raise RuntimeError("RSS_AUDIO_BOT_TOKEN is required")
    asyncio.run(worker_loop(settings))


if __name__ == "__main__":
    main()
