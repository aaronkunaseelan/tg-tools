from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import Bot

from config import Settings, load_settings, validate_settings, TRANSCRIBE_LEASE_SECONDS, transcribe_worker_id
from download_names import _safe_download_name
from mtproto_dl import download_file as mtproto_download
from mtproto_dl import ensure_mtproto_ready, needs_mtproto
from pipeline import PipelineError, fetch_metadata
from source_audio import stable_url_for_display
from runtime_client import RuntimeClient, RuntimeClientError
from runtime_jobs import (DeliveryTargetMissing, message_reference,
                          scribe_revision_delivery_payload, transcript_delivery_payload)
from scribe_store import ScribeOrderingError, ScribeStore
from source_audio import SourceAudioError, classify_text, validate_source_url

logger = logging.getLogger(__name__)

TRANSCRIBE_BIN = str(
    Path(__file__).resolve().parents[1] / "transcribe-audio" / "transcribe.py"
)


@dataclass(frozen=True)
class TranscribeResult:
    transcript: str
    title_hint: str | None
    filename: str
    force_document: bool


def _safe_doc_stem(value: str) -> str:
    stem = Path(value).stem
    normalized = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return normalized[:60] or "transcript"


def _transcript_doc_name(input_json: dict[str, Any], title_hint: str | None) -> str:
    base = (
        title_hint
        or input_json.get("titleHint")
        or input_json.get("telegramFileName")
        or (
            Path(str(input_json["sourceUrl"]).split("?", 1)[0]).stem
            if input_json.get("sourceUrl")
            else None
        )
        or "transcript"
    )
    return f"{_safe_doc_stem(str(base))}.md"


def _transcribe_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "RSS_AUDIO_BOT_TOKEN",
        "RSS_AUDIO_API_TOKEN",
        "TELEGRAM_API_HASH",
        "TELEGRAM_API_ID",
        "OPENROUTER_API_KEY_TRANSCRIBE",
        "TRANSCRIBE_AUDIO_OPENROUTER_API_KEY",
        # Keep in step with source_audio's deny-list: both subprocess
        # boundaries scrub the same set.
        "OPENAI_API_KEY",
    ):
        env.pop(key, None)
    return env


def _summarize_subprocess_stderr(stderr: bytes | None) -> str:
    if not stderr:
        return "unknown error"
    text = stderr.decode(errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "unknown error"
    interesting = [line for line in lines if " ERROR " in line or line.startswith("ERROR ")]
    if interesting:
        return "\n".join(interesting[-2:])
    return "\n".join(lines[-4:])


async def _download_telegram_input(settings: Settings, bot: Bot, input_json: dict[str, Any], tmpdir: str) -> str:
    local_source = input_json.get("localSourcePath")
    if isinstance(local_source, str) and Path(local_source).is_file():
        return local_source
    telegram_file_id = input_json.get("telegramFileId")
    if not isinstance(telegram_file_id, str) or not telegram_file_id:
        raise PipelineError("input", "Missing Telegram file id")
    target = Path(tmpdir) / _safe_download_name(str(input_json.get("telegramFileName") or "telegram-audio"))
    file_size = input_json.get("fileSize")
    if needs_mtproto(file_size if isinstance(file_size, int) else None):
        try:
            ensure_mtproto_ready()
        except Exception as exc:
            raise PipelineError("download", f"Large Telegram file requires MTProto credentials: {exc}") from exc
        try:
            message_ref = message_reference(input_json)
        except DeliveryTargetMissing as exc:
            # Terminal: MTProto needs the originating message to fetch a file
            # larger than the Bot API limit.
            raise PipelineError(
                "input",
                f"Large Telegram file requires a messageRef to download: {exc}") from exc
        await mtproto_download(
            chat_id=message_ref["chat_id"],
            message_id=message_ref["message_id"],
            dest_path=str(target),
        )
    else:
        file_obj = await bot.get_file(telegram_file_id)
        await file_obj.download_to_drive(custom_path=str(target))
    return str(target)


def _source_url_display(input_json: dict[str, Any]) -> str | None:
    """Best-effort display URL for the success result.

    It runs after a finished transcription, so a malformed sourceUrl yields
    None rather than failing a job whose provider spend is already done.
    """
    if input_json.get("inputKind") == "telegram_text":
        return None
    try:
        source_url = _source_url(input_json)
    except PipelineError:
        return None
    if source_url is None:
        return None
    return stable_url_for_display(source_url)


def _source_url(input_json: dict[str, Any]) -> str | None:
    source = input_json.get("sourceUrl")
    if source is None:
        return None
    if not isinstance(source, str) or not source.strip():
        raise PipelineError("input", "Unsupported source URL")
    classified = classify_text(source)
    if not classified:
        raise PipelineError("input", "Unsupported source URL")
    source_url = classified[1]
    try:
        return validate_source_url(source_url)
    except SourceAudioError as exc:
        raise PipelineError("input", f"Unsupported source URL: {exc}") from exc


async def execute_transcribe_job(settings: Settings, bot: Bot, input_json: dict[str, Any]) -> TranscribeResult:
    with tempfile.TemporaryDirectory(prefix="tg-transcribe-runtime-") as tmpdir:
        force_document = False
        title_hint = input_json.get("titleHint") if isinstance(input_json.get("titleHint"), str) else None
        source_url = _source_url(input_json)
        if source_url is not None:
            source_arg = source_url
            force_document = True
            try:
                metadata = await asyncio.to_thread(fetch_metadata, settings, source_arg)
            except PipelineError:
                metadata = None
            if metadata and metadata.title:
                title_hint = metadata.title
        else:
            source_arg = await _download_telegram_input(settings, bot, input_json, tmpdir)

        out_path = os.path.join(tmpdir, "transcript.md")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            TRANSCRIBE_BIN,
            source_arg,
            "--model",
            str(input_json.get("model") or "gpt-4o-mini-transcribe"),
            "--jobs",
            str(input_json.get("jobs") or 3),
            "-o",
            out_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_transcribe_subprocess_env(),
        )
        try:
            try:
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
            except asyncio.TimeoutError as exc:
                raise PipelineError("transcribe", "Transcription timed out") from exc
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
        if proc.returncode != 0:
            raise PipelineError("transcribe", _summarize_subprocess_stderr(stderr))
        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            raise PipelineError("transcribe", "Empty transcript output")
        transcript = Path(out_path).read_text(encoding="utf-8").strip()
        return TranscribeResult(
            transcript=transcript,
            title_hint=title_hint,
            filename=_transcript_doc_name(input_json, title_hint),
            force_document=force_document,
        )


def _load_source_text(input_json: dict[str, Any]) -> str:
    if "sourceText" not in input_json:
        raise PipelineError("input", "sourceText is required")
    source_text = input_json["sourceText"]
    if not isinstance(source_text, str):
        raise PipelineError("input", "sourceText must be a string")
    return source_text


async def execute_scribe_job(settings: Settings, bot: Bot, input_json: dict[str, Any]) -> TranscribeResult:
    if input_json.get("inputKind") == "telegram_text":
        text = _load_source_text(input_json).strip()
        if not text:
            raise PipelineError("input", "Empty scribe text append")
        return TranscribeResult(
            transcript=text,
            title_hint=None,
            filename="scribe.md",
            force_document=True,
        )
    return await execute_transcribe_job(settings, bot, input_json)


def _result_id(finalized: dict[str, Any]) -> str | None:
    result = finalized.get("result") if isinstance(finalized, dict) else None
    if isinstance(result, dict) and result.get("resultId"):
        return str(result["resultId"])
    return None


class _FinalizeTracking:
    """Proxy around RuntimeClient that records whether finalize was called.

    Lets the outer guard below tell "this attempt reported an outcome" from
    "this attempt died silently", without threading a flag through every
    branch of the claim body.
    """

    def __init__(self, client: RuntimeClient) -> None:
        self._client = client
        self.finalized = False

    def finalize(self, payload: dict[str, Any]) -> Any:
        result = self._client.finalize(payload)
        self.finalized = True
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


async def _finalize_unexpected(
    client: RuntimeClient,
    *,
    job_id: str,
    attempt_id: str,
    lease_token: str,
    exc: BaseException,
) -> None:
    """Best-effort finalize for a failure no handler modelled.

    Without this an unmodelled exception (a bare KeyError, an OSError, a
    sqlite failure) left the claimed attempt unfinalized until the lease
    expired -- 1800s of nothing, with only a log line to show for it.
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


async def process_claim(*, client: RuntimeClient, settings: Settings, bot: Bot, claim: dict[str, Any]) -> None:
    """Run one claim, guaranteeing the attempt is always finalized."""
    tracking = _FinalizeTracking(client)
    attempt = dict(claim["attempt"])
    job = dict(claim["job"])
    try:
        await _execute_claim(client=tracking, settings=settings, bot=bot, claim=claim)
    except BaseException as exc:
        if not tracking.finalized:
            logger.exception(
                "Unmodelled failure processing job %s; finalizing as retryable",
                job.get("jobId"))
            await _finalize_unexpected(
                client,
                job_id=str(job["jobId"]),
                attempt_id=str(attempt["attemptId"]),
                lease_token=str(attempt["leaseToken"]),
                exc=exc,
            )
        raise


async def _execute_claim(*, client: RuntimeClient, settings: Settings, bot: Bot, claim: dict[str, Any]) -> None:
    job = dict(claim["job"])
    attempt = dict(claim["attempt"])
    input_json = dict(job.get("inputJson") or {})
    job_id = str(job["jobId"])
    attempt_id = str(attempt["attemptId"])
    lease_token = str(attempt["leaseToken"])
    scribe = input_json.get("scribe") if isinstance(input_json.get("scribe"), dict) else None

    await asyncio.to_thread(client.mark_running, job_id=job_id, attempt_id=attempt_id, lease_token=lease_token)
    try:
        result = await execute_scribe_job(settings, bot, input_json) if scribe else await execute_transcribe_job(settings, bot, input_json)
        revision = None
        if scribe:
            store = ScribeStore(settings.scribe_db_path)
            revision = await asyncio.to_thread(
                store.finalize_entry,
                thread_id=str(scribe["threadId"]),
                entry_id=str(scribe["entryId"]),
                content_text=result.transcript,
                source_metadata={
                    "inputKind": input_json.get("inputKind"),
                    "titleHint": result.title_hint,
                    "transcribeProvider": input_json.get("provider"),
                    "transcribeModel": input_json.get("model"),
                    "transcriptChars": len(result.transcript),
                },
            )
    except ScribeOrderingError as exc:
        await asyncio.to_thread(
            client.finalize,
            {
                "jobId": job_id,
                "attemptId": attempt_id,
                "leaseToken": lease_token,
                "outcome": "retryable_error",
                "errorClass": "scribe_ordering",
                "errorText": str(exc),
                "resultJson": {"ok": False, "stage": "scribe_ordering", "message": str(exc)},
                "backoffSec": 30,
            },
        )
        return
    except PipelineError as exc:
        if scribe:
            await asyncio.to_thread(
                ScribeStore(settings.scribe_db_path).mark_entry_failed,
                entry_id=str(scribe["entryId"]),
                error_class=exc.stage,
                error_text=str(exc),
            )
        finalized = await asyncio.to_thread(
            client.finalize,
            {
                "jobId": job_id,
                "attemptId": attempt_id,
                "leaseToken": lease_token,
                "outcome": "terminal_error" if exc.stage == "input" else "retryable_error",
                "errorClass": exc.stage,
                "errorText": str(exc),
                "resultJson": {"ok": False, "stage": exc.stage, "message": str(exc)},
                "backoffSec": 300,
            },
        )
        if exc.stage == "input":
            failure_input_json = dict(input_json)
            if scribe:
                failure_scribe = dict(scribe)
                failure_scribe["deliveryRole"] = "bot_failure"
                failure_input_json["scribe"] = failure_scribe
            delivery = transcript_delivery_payload(
                job_id=job_id,
                result_id=_result_id(finalized),
                input_json=failure_input_json,
                transcript=f"FAILED at {exc.stage}: {exc}",
                filename="transcript-error.md",
                caption="Transcript failed",
            )
            await asyncio.to_thread(client.append_delivery, delivery)
        return

    result_json: dict[str, Any] = {
        "ok": True,
        "transcriptChars": len(result.transcript),
        "titleHint": result.title_hint,
        "filename": result.filename,
        "sourceUrlDisplay": _source_url_display(input_json),
    }
    if scribe and revision is not None:
        result_json.update(
            {
                "scribeThreadId": revision.thread_id,
                "scribeEntryId": revision.entry_id,
                "scribeRevisionId": revision.revision_id,
                "revision": revision.revision,
                "filename": revision.filename,
                "markdownHash": revision.markdown_hash,
                "markdownChars": len(revision.markdown_text),
                "entryChars": len(result.transcript),
                "inputKind": input_json.get("inputKind"),
                "transcribeProvider": input_json.get("provider"),
                "transcribeModel": input_json.get("model"),
            }
        )
    finalized = await asyncio.to_thread(
        client.finalize,
        {
            "jobId": job_id,
            "attemptId": attempt_id,
            "leaseToken": lease_token,
            "outcome": "succeeded",
            "resultJson": result_json,
        },
    )
    if scribe and revision is not None:
        delivery = scribe_revision_delivery_payload(
            job_id=job_id,
            result_id=_result_id(finalized),
            input_json=input_json,
            markdown_text=revision.markdown_text,
            filename=revision.filename,
            caption=f"Scribe v{revision.revision:03d} ({len(revision.markdown_text)} chars)",
            revision_id=revision.revision_id,
            revision=revision.revision,
        )
    else:
        delivery = transcript_delivery_payload(
            job_id=job_id,
            result_id=_result_id(finalized),
            input_json=input_json,
            transcript=result.transcript,
            filename=result.filename,
            caption=f"Transcript ({len(result.transcript)} chars)",
        )
    await asyncio.to_thread(client.append_delivery, delivery)


async def worker_loop(settings: Settings) -> None:
    client = RuntimeClient(settings)
    bot = Bot(settings.bot_token)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    worker_id = transcribe_worker_id(settings.runtime_worker_id)
    registered = False
    while not stop.is_set():
        try:
            if not registered:
                await asyncio.to_thread(
                    client.register_worker,
                    worker_id=worker_id,
                    worker_venue="oci",
                    capacity=1,
                    supported_execution_profiles=("cpu_media",),
                    workload_types=("transcribe_audio",),
                    producer_systems=("rss-audio",),
                    allowed_queue_classes=("foreground",),
                    queue_preference=("foreground",),
                    metadata_json={"component": "transcribe-runtime-worker", "pid": os.getpid()},
                )
                registered = True
            await asyncio.to_thread(client.heartbeat_worker, worker_id=worker_id, metadata_json={"phase": "idle"})
            claim = await asyncio.to_thread(
                client.claim_job,
                worker_id=worker_id,
                lease_duration_sec=TRANSCRIBE_LEASE_SECONDS,
                workload_types=("transcribe_audio",),
            )
            if claim:
                await process_claim(client=client, settings=settings, bot=bot, claim=claim)
                continue
        except RuntimeClientError:
            logger.exception("Runtime API unavailable; transcribe worker will retry")
            registered = False
        except Exception:
            logger.exception("Unexpected transcribe runtime worker failure")

        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = load_settings()
    validate_settings(settings)
    if not settings.bot_token:
        raise RuntimeError("RSS_AUDIO_BOT_TOKEN is required")
    if not os.path.isfile(TRANSCRIBE_BIN):
        raise RuntimeError(f"RSS audio transcribe pipeline is missing: {TRANSCRIBE_BIN}")
    asyncio.run(worker_loop(settings))


if __name__ == "__main__":
    main()
