from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from config import ensure_directories, load_settings, validate_settings
from pipeline import ASRError, AudioError, LocalSourceMissingError, PipelineError, execute_pipeline
from runtime_client import RuntimeClient, RuntimeClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meeting-summary-runtime-worker")


@dataclass
class WorkerControlState:
    accepting_claims: bool = True


class UnsupportedSourceError(RuntimeError):
    pass


def _job_source_ref(job: dict[str, Any]) -> str:
    producer_ref = str(job.get("producerRef") or "").strip()
    if producer_ref:
        return producer_ref
    input_json = job.get("inputJson")
    if isinstance(input_json, dict):
        return str(input_json.get("sourceKey") or input_json.get("source_key") or "").strip()
    return ""


def require_telegram_source(job: dict[str, Any]) -> None:
    source_ref = _job_source_ref(job)
    if source_ref.startswith("tg:"):
        return
    label = source_ref.split(":", 1)[0] if ":" in source_ref else "unknown"
    raise UnsupportedSourceError(f"unsupported meeting-summary source for Telegram runtime worker: {label}")


def classify_worker_error(exc: Exception) -> tuple[str, str, str]:
    text = str(exc)
    if isinstance(exc, UnsupportedSourceError):
        return ("terminal_error", "unsupported_source", text)
    if isinstance(exc, LocalSourceMissingError):
        return ("terminal_error", "local_source_missing", text)
    if isinstance(exc, AudioError):
        if "exceeds limit" in text:
            return ("terminal_error", "audio_too_long", text)
        return ("terminal_error", "invalid_audio", text)
    if isinstance(exc, ASRError):
        return ("retryable_error", "asr_transient", text)
    if isinstance(exc, PipelineError):
        return ("terminal_error", "missing_input", text)
    if isinstance(exc, RuntimeClientError):
        return ("retryable_error", "network_error", text)
    return ("retryable_error", "storage_transient", text)


def worker_metadata(settings, **extras: Any) -> dict[str, Any]:
    metadata = {
        "producer": "meeting-summary",
        "hostname": os.getenv("HOSTNAME", "unknown"),
        "pid": os.getpid(),
    }
    metadata.update(extras)
    return metadata


def apply_worker_control(
    result: dict[str, Any] | None,
    control_state: WorkerControlState,
    *,
    worker_id: str,
    phase: str,
    job_id: str | None = None,
    attempt_id: str | None = None,
) -> None:
    if not isinstance(result, dict):
        return
    control = result.get("control")
    if not isinstance(control, dict):
        return
    if control.get("action") != "drain":
        return
    if control_state.accepting_claims:
        log.info(
            "worker_drain_requested worker_id=%s phase=%s reason=%s deadline_at=%s job_id=%s attempt_id=%s",
            worker_id,
            phase,
            control.get("reason"),
            control.get("deadlineAt"),
            job_id,
            attempt_id,
        )
    control_state.accepting_claims = False


def register_worker(settings, client: RuntimeClient, control_state: WorkerControlState) -> None:
    result = client.register_worker(
        worker_id=settings.runtime_worker_id,
        worker_venue=settings.runtime_worker_venue,
        capacity=settings.runtime_worker_capacity,
        supported_execution_profiles=settings.runtime_supported_execution_profiles,
        workload_types=settings.runtime_claim_workload_types,
        producer_systems=settings.runtime_claim_producer_systems,
        allowed_queue_classes=settings.runtime_allowed_queue_classes,
        queue_preference=settings.runtime_queue_preference,
        metadata_json=worker_metadata(settings, phase="startup"),
    )
    apply_worker_control(result, control_state, worker_id=settings.runtime_worker_id, phase="startup")


def heartbeat_worker(settings, client: RuntimeClient, control_state: WorkerControlState, *, phase: str, **extras: Any) -> None:
    result = client.heartbeat_worker(
        worker_id=settings.runtime_worker_id,
        metadata_json=worker_metadata(settings, phase=phase, **extras),
    )
    apply_worker_control(
        result,
        control_state,
        worker_id=settings.runtime_worker_id,
        phase=phase,
        job_id=str(extras.get("job_id")) if extras.get("job_id") is not None else None,
        attempt_id=str(extras.get("attempt_id")) if extras.get("attempt_id") is not None else None,
    )


def heartbeat_loop(
    client: RuntimeClient,
    *,
    settings,
    control_state: WorkerControlState,
    job_id: str,
    attempt_id: str,
    lease_token: str,
    lease_duration_sec: int,
    heartbeat_interval_sec: int,
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(heartbeat_interval_sec):
        try:
            client.heartbeat(
                job_id=job_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_duration_sec=lease_duration_sec,
            )
            heartbeat_worker(
                settings,
                client,
                control_state,
                phase="running",
                job_id=job_id,
                attempt_id=attempt_id,
            )
        except Exception:
            log.warning("heartbeat failed job_id=%s attempt_id=%s", job_id, attempt_id, exc_info=True)


def process_claim(settings, client: RuntimeClient, claim: dict[str, Any], control_state: WorkerControlState) -> dict[str, Any]:
    job = dict(claim["job"])
    attempt = dict(claim["attempt"])

    client.mark_running(
        job_id=str(job["jobId"]),
        attempt_id=str(attempt["attemptId"]),
        lease_token=str(attempt["leaseToken"]),
    )

    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        kwargs={
            "client": client,
            "settings": settings,
            "control_state": control_state,
            "job_id": str(job["jobId"]),
            "attempt_id": str(attempt["attemptId"]),
            "lease_token": str(attempt["leaseToken"]),
            "lease_duration_sec": settings.runtime_lease_duration_sec,
            "heartbeat_interval_sec": settings.runtime_heartbeat_interval_sec,
            "stop_event": stop_event,
        },
        daemon=True,
    )
    heartbeat_thread.start()

    try:
        require_telegram_source(job)
        result = execute_pipeline(settings, job)
        finalized = client.finalize(
            {
                "jobId": job["jobId"],
                "attemptId": attempt["attemptId"],
                "leaseToken": attempt["leaseToken"],
                "outcome": "succeeded",
                "resultJson": result.to_result_json(),
            }
        )
        log.info(
            "finalized succeeded job_id=%s attempt_id=%s summary_status=%s draining=%s",
            job["jobId"],
            attempt["attemptId"],
            result.summary_status,
            not control_state.accepting_claims,
        )
        return finalized
    except Exception as exc:
        outcome, error_class, error_text = classify_worker_error(exc)
        finalized = client.finalize(
            {
                "jobId": job["jobId"],
                "attemptId": attempt["attemptId"],
                "leaseToken": attempt["leaseToken"],
                "outcome": outcome,
                "errorClass": error_class,
                "errorText": error_text,
            }
        )
        log.warning(
            "finalized %s job_id=%s attempt_id=%s error_class=%s error=%s draining=%s",
            outcome,
            job["jobId"],
            attempt["attemptId"],
            error_class,
            error_text,
            not control_state.accepting_claims,
        )
        return finalized
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=5)


def run_worker(*, once: bool = False) -> int:
    settings = load_settings()
    ensure_directories(settings)
    validate_settings(settings)
    client = RuntimeClient(settings)
    control_state = WorkerControlState()
    log.info(
        "worker_started worker_id=%s venue=%s capacity=%s profiles=%s workload_types=%s producer_systems=%s queue_preference=%s allowed_queue_classes=%s lease=%s heartbeat=%s",
        settings.runtime_worker_id,
        settings.runtime_worker_venue,
        settings.runtime_worker_capacity,
        list(settings.runtime_supported_execution_profiles),
        list(settings.runtime_claim_workload_types),
        list(settings.runtime_claim_producer_systems),
        list(settings.runtime_queue_preference),
        list(settings.runtime_allowed_queue_classes),
        settings.runtime_lease_duration_sec,
        settings.runtime_heartbeat_interval_sec,
    )

    register_worker(settings, client, control_state)
    if not control_state.accepting_claims:
        log.info("worker_exit_drained worker_id=%s phase=startup", settings.runtime_worker_id)
        return 0

    processed = 0
    while True:
        try:
            heartbeat_worker(settings, client, control_state, phase="idle")
            if not control_state.accepting_claims:
                log.info("worker_exit_drained worker_id=%s phase=idle processed=%s", settings.runtime_worker_id, processed)
                return processed

            claim = client.claim_job(
                worker_id=settings.runtime_worker_id,
                worker_venue=settings.runtime_worker_venue,
                capacity=settings.runtime_worker_capacity,
                supported_execution_profiles=settings.runtime_supported_execution_profiles,
                workload_types=settings.runtime_claim_workload_types,
                producer_systems=settings.runtime_claim_producer_systems,
                allowed_queue_classes=settings.runtime_allowed_queue_classes,
                queue_preference=settings.runtime_queue_preference,
                metadata_json=worker_metadata(settings, phase="claim"),
                lease_duration_sec=settings.runtime_lease_duration_sec,
            )
        except RuntimeClientError as exc:
            log.warning("runtime claim failed: %s", exc)
            if once:
                raise
            time.sleep(2)
            continue
        if not claim:
            if once:
                return processed
            time.sleep(2)
            continue
        process_claim(settings, client, claim, control_state)
        processed += 1
        heartbeat_worker(
            settings,
            client,
            control_state,
            phase="post_attempt",
            job_id=str(claim["job"]["jobId"]),
            attempt_id=str(claim["attempt"]["attemptId"]),
        )
        if once:
            return processed
        if not control_state.accepting_claims:
            log.info("worker_exit_drained worker_id=%s phase=post_attempt processed=%s", settings.runtime_worker_id, processed)
            return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="meeting-summary runtime worker")
    parser.add_argument("--once", action="store_true", help="claim and process at most one job")
    parser.add_argument("--json", action="store_true", help="emit structured output")
    args = parser.parse_args()

    started = time.time()
    processed = run_worker(once=args.once)
    if args.json:
        print(json.dumps({"ok": True, "processed": processed, "elapsed_seconds": round(time.time() - started, 3)}))


if __name__ == "__main__":
    main()
