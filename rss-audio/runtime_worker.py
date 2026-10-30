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
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, unquote
from typing import Any

from telegram import Bot
from tg_tools_common.lease import AttemptLease, LeaseLost
from tg_tools_common.progress import ProgressReporter, NullProgress, current_progress, use_progress

from config import Settings, load_settings, validate_settings, PUBLISH_LEASE_SECONDS
from download_names import _safe_download_name
from exit_rotation import ExitRotator
from mtproto_dl import download_file as mtproto_download
from mtproto_dl import ensure_mtproto_ready, needs_mtproto
from pipeline import (
    PipelineError,
    fetch_metadata,
    ingest_direct_audio_url,
    ingest_local_audio,
    ingest_url_with_metadata,
    render_completion_message,
)
from runtime_client import RuntimeClient, RuntimeClientError
from runtime_jobs import (DeliveryTargetMissing, confirmation_delivery_payload,
                          message_reference)
from source_audio import (
    SourceFailureKind,
    SourceRoute,
    check_oci_mullvad_health,
    classify_source_failure,
    classify_text,
    is_mac_reachable,
    route_label,
    stable_url_for_display,
)
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


@dataclass(frozen=True)
class RuntimeExecutionResult:
    slug: str
    title: str
    public_url: str
    file_size_bytes: int
    completion_html: str
    script_url: str | None = None
    notes_url: str | None = None
    manifest_url: str | None = None


@contextlib.contextmanager
def _download_dir(work_dir: Path | None):
    """Durable staging when a work dir is supplied, else a temp dir."""
    if work_dir is not None:
        target = Path(work_dir) / "download"
        target.mkdir(parents=True, exist_ok=True)
        yield target
        return
    with tempfile.TemporaryDirectory(prefix="rss-audio-runtime-telegram-") as tmpdir:
        yield Path(tmpdir)


def _source_settings(settings: Settings):
    from source_audio import SourceToolSettings

    return SourceToolSettings(
        mac_ssh_target=settings.mac_ssh_target,
        mac_ytdlp_path=settings.mac_ytdlp_path,
        mac_cookies_from_browser=settings.mac_cookies_from_browser,
        yt_dlp_bin=settings.yt_dlp_bin,
        yt_dlp_format=settings.yt_dlp_format,
        ffmpeg_bin=settings.ffmpeg_bin,
        mullvad_proxy=settings.mullvad_proxy,
        mullvad_control_url=settings.mullvad_control_url,
        mullvad_control_token=settings.mullvad_control_token,
        yt_dlp_remote_components=settings.yt_dlp_remote_components,
    )


def _route_health_error(route: SourceRoute, reason: str, detail: str = "") -> PipelineError:
    message = f"{route_label(route)} route unhealthy"
    if reason:
        message = f"{message}: {reason}"
    return PipelineError("route-health", message, detail)


def _candidate_routes(input_json: dict[str, Any]) -> list[SourceRoute]:
    preferred = input_json.get("preferredRoute")
    if preferred:
        return [SourceRoute(str(preferred))]
    return [SourceRoute.OCI_MULLVAD, SourceRoute.MAC_SSH]


def _exit_rotator(settings: Settings) -> ExitRotator:
    """Exit rotation is opt-in: without a control-server token the Mullvad
    route behaves exactly as before (a single attempt per job)."""
    return ExitRotator(
        control_url=settings.mullvad_control_url,
        token=settings.mullvad_control_token,
        proxy=settings.mullvad_proxy,
    )


def _execution_result(
    published: Any, source_url: str
) -> RuntimeExecutionResult:
    return RuntimeExecutionResult(
        slug=published.slug,
        title=published.title,
        public_url=published.public_url,
        file_size_bytes=published.file_size_bytes,
        completion_html=render_completion_message(
            published.title, published.public_url,
            notes_url=published.notes_url, script_url=published.script_url,
            source_url=source_url),
        script_url=published.script_url,
        notes_url=published.notes_url,
        manifest_url=published.manifest_url,
    )


def _attempt_mullvad_route(
    settings: Settings,
    source_url: str,
    work_dir: Path | None,
    rotator: ExitRotator,
) -> tuple[list[PipelineError], RuntimeExecutionResult | None]:
    """Try the proxy route up to ``rotator.attempts()`` times, rotating the
    Mullvad egress (with conservative jitter) between eligible failures.

    Exit rotation is the middle rung between the pre-flight health gate and
    the Mac fallback: it exists for IP-shaped refusals (bot checks, rate
    limits) that a healthy-but-pinned exit can trigger.  It never fires for
    proxy-unavailable or format failures, and never rotates more than
    ``max_rotations`` times per job.
    """
    failures: list[PipelineError] = []
    for attempt in range(rotator.attempts()):
        current_progress().check()
        try:
            metadata = fetch_metadata(settings, source_url, route=SourceRoute.OCI_MULLVAD)
            published = ingest_url_with_metadata(
                settings, source_url, metadata, route=SourceRoute.OCI_MULLVAD,
                work_dir=work_dir)
            return [], _execution_result(published, source_url)
        except PipelineError as exc:
            if exc.dependency != "source":
                raise
            failures.append(exc)
            if attempt + 1 < rotator.attempts():
                detail = f"{exc}\n{exc.stderr}"
                if rotator.eligible(detail) and rotator.rotate(attempt):
                    delay = random.uniform(*rotator.jitter_sec)
                    logger.info(
                        "mullvad egress rotated after %s; retrying route in %.1fs",
                        exc.stage, delay)
                    with current_progress().stage("source_retry", retrySeconds=delay, reason="source_retry"):
                        time.sleep(delay)
                    continue
            break
    return failures, None


def _fetch_and_publish_url(
    settings: Settings,
    input_json: dict[str, Any],
    source_url: str,
    work_dir: Path | None = None,
) -> RuntimeExecutionResult:
    failures: list[PipelineError] = []
    rotator = _exit_rotator(settings)
    for route in _candidate_routes(input_json):
        if route == SourceRoute.OCI_MULLVAD:
            health = check_oci_mullvad_health(_source_settings(settings))
            if not health.ok:
                failures.append(_route_health_error(route, health.reason, health.detail))
                continue
            route_failures, result = _attempt_mullvad_route(
                settings, source_url, work_dir, rotator)
            failures.extend(route_failures)
            if result is not None:
                return result
            continue

        if route == SourceRoute.MAC_SSH and not is_mac_reachable(_source_settings(settings)):
            failures.append(_route_health_error(route, "mac_unavailable"))
            continue

        try:
            metadata = fetch_metadata(settings, source_url, route=route)
            published = ingest_url_with_metadata(
                settings, source_url, metadata, route=route, work_dir=work_dir)
            return _execution_result(published, source_url)
        except PipelineError as exc:
            if exc.dependency != "source":
                raise
            failures.append(exc)
            continue

    if not failures:
        raise PipelineError("source", "No source routes were attempted")
    detail = "\n\n".join(f"{failure.stage}: {failure}\n{failure.stderr}" for failure in failures)
    # Retryability is classified from the raised error, so surfacing only the
    # last route's message let route ordering decide the outcome: a transient
    # "mac unreachable" after a terminal auth failure looked retryable, and a
    # terminal last route masked a genuinely retryable earlier one. Prefer a
    # route that failed transiently -- if any route might succeed later, the
    # job as a whole is worth retrying.
    representative = next(
        (failure for failure in failures if _is_retryable_pipeline_error(failure)),
        failures[-1],
    )
    raise PipelineError("source", str(representative), detail,
                        retryable=any(_is_retryable_pipeline_error(failure) for failure in failures))


def _classified_source_url(input_json: dict[str, Any]) -> tuple[str, str] | None:
    source = input_json.get("sourceUrl")
    if not isinstance(source, str) or not source.strip():
        return None
    classified = classify_text(source)
    if not classified:
        raise PipelineError("input", "Send a supported URL or audio file")
    return classified


async def execute_runtime_job(
    settings: Settings,
    bot: Bot,
    input_json: dict[str, Any],
    work_dir: Path | None = None,
) -> RuntimeExecutionResult:
    classified = _classified_source_url(input_json)
    if classified:
        kind, source_url = classified
        if kind == "direct_audio_url":
            published = await asyncio.to_thread(
                ingest_direct_audio_url, settings, source_url, work_dir=work_dir)
        else:
            published = await asyncio.to_thread(
                _fetch_and_publish_url, settings, input_json, source_url, work_dir)
        return RuntimeExecutionResult(
            slug=published.slug,
            title=published.title,
            public_url=published.public_url,
            file_size_bytes=published.file_size_bytes,
            completion_html=render_completion_message(
                published.title, published.public_url,
                notes_url=published.notes_url, script_url=published.script_url,
                source_url=source_url),
            script_url=published.script_url,
            notes_url=published.notes_url,
            manifest_url=published.manifest_url,
        )

    local_source = input_json.get("localSourcePath")
    telegram_file_id = input_json.get("telegramFileId")
    if not (isinstance(local_source, str) and Path(local_source).is_file()) and not (
        isinstance(telegram_file_id, str) and telegram_file_id
    ):
        raise PipelineError("input", "Unsupported runtime RSS input")

    # Only the MTProto branch needs it, but resolving here keeps the failure
    # typed and attributable to the input rather than surfacing as a KeyError
    # from inside the download.
    message_ref: dict[str, Any] = {}
    try:
        message_ref = message_reference(input_json)
    except DeliveryTargetMissing:
        message_ref = {}
    with _download_dir(work_dir) as tmpdir:
        if isinstance(local_source, str) and Path(local_source).is_file():
            target = Path(local_source)
        else:
            target = Path(tmpdir) / _safe_download_name(str(input_json.get("telegramFileName") or "telegram-audio"))
            file_size = input_json.get("fileSize")
            if needs_mtproto(file_size if isinstance(file_size, int) else None):
                try:
                    ensure_mtproto_ready()
                except Exception as exc:
                    raise PipelineError("download", f"Large Telegram file requires MTProto credentials: {exc}") from exc
                if not message_ref:
                    # Terminal: an oversized Telegram file can only be pulled
                    # via MTProto, which needs the originating message.
                    raise PipelineError(
                        "input",
                        "Large Telegram file requires a messageRef to download via MTProto")
                await mtproto_download(
                    chat_id=message_ref["chat_id"],
                    message_id=message_ref["message_id"],
                    dest_path=str(target),
                )
            else:
                file_obj = await bot.get_file(telegram_file_id)
                await file_obj.download_to_drive(custom_path=str(target))

        published = await asyncio.to_thread(
            ingest_local_audio,
            settings,
            target,
            str(input_json.get("titleHint") or target.stem),
            work_dir=work_dir,
        )
    return RuntimeExecutionResult(
        slug=published.slug,
        title=published.title,
        public_url=published.public_url,
        file_size_bytes=published.file_size_bytes,
        completion_html=render_completion_message(
            published.title, published.public_url,
            notes_url=published.notes_url, script_url=published.script_url),
        script_url=published.script_url,
        notes_url=published.notes_url,
        manifest_url=published.manifest_url,
    )


def _is_retryable_pipeline_error(exc: PipelineError) -> bool:
    if exc.retryable is not None:
        return exc.retryable
    if exc.dependency != "source":
        return exc.stage != "input"
    if exc.stage == "input":
        return False
    kind = classify_source_failure(f"{exc}\n{exc.stderr}")
    if kind in {
        SourceFailureKind.AUTH_REQUIRED,
        SourceFailureKind.SOURCE_UNAVAILABLE,
        SourceFailureKind.FORMAT_UNAVAILABLE,
    }:
        return False
    return True


def _result_id(finalized: dict[str, Any]) -> str | None:
    result = finalized.get("result") if isinstance(finalized, dict) else None
    if isinstance(result, dict) and result.get("resultId"):
        return str(result["resultId"])
    return None


def _source_url_display(input_json: dict[str, Any]) -> str | None:
    try:
        classified = _classified_source_url(input_json)
    except PipelineError:
        return None
    return stable_url_for_display(classified[1]) if classified else None


async def process_claim(*, client, settings, bot, claim):
    job, attempt = claim["job"], claim["attempt"]
    remaining = 7200
    if attempt.get("leaseExpiresAt"):
        remaining = max(0, (datetime.fromisoformat(attempt["leaseExpiresAt"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
    lease = AttemptLease(client=client, job_id=job["jobId"], attempt_id=attempt["attemptId"],
                         lease_token=attempt["leaseToken"], lease_seconds=7200, initial_remaining=remaining)
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
        except PipelineError as exc:
            current_progress().check()
            outcome = "retryable_error" if _is_retryable_pipeline_error(exc) else "terminal_error"
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
            artifacts = await asyncio.to_thread(_register_published_artifacts, client, result, work_dir, job_id, attempt_id)
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
                    "artifactIds": artifacts,
                },
            },
        )
        await _deliver(
            client, job_id=job_id, input_json=input_json, finalized=finalized,
            html_text=result.completion_html)
        # Retain source/checkpoints if registration is degraded; publication
        # and notification success must not depend on the optional catalog.
        expected_artifacts = sum(bool(url) for url in [result.public_url,result.script_url,result.notes_url,result.manifest_url])
        if os.environ.get("TG_TOOLS_PROGRESS_ENABLED") != "1" or len(artifacts) == expected_artifacts:
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


def _register_published_artifacts(client, result, work_dir, job_id, attempt_id):
    registered = []
    for url, kind, mime in [(result.public_url,"rss_audio","audio/mpeg"),
                            (result.script_url,"rss_script","text/markdown"),
                            (result.notes_url,"rss_notes","text/markdown"),
                            (result.manifest_url,"rss_manifest","application/json")]:
        if not url: continue
        name = unquote(urlsplit(url).path.rsplit("/",1)[-1])
        path = work_dir / name
        if Path(name).name != name or not path.is_file(): continue
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
    return registered


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
                    await process_claim(client=client, settings=settings, bot=bot, claim=claim)
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
