from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from config import load_settings, validate_settings
from runtime_client import RuntimeClient


class ReplayError(RuntimeError):
    pass


def build_replay_payload(job: dict, *, replay_reason: str | None = None, replayed_at: str | None = None) -> dict:
    replayed_at = replayed_at or datetime.now(UTC).isoformat()
    input_json = dict(job.get("inputJson") or {})
    source_key = str(input_json.get("sourceKey") or job.get("producerRef") or "").strip()
    if not source_key:
        raise ReplayError("job is missing sourceKey / producerRef")
    local_source_path = str(input_json.get("localSourcePath") or "").strip()
    if not local_source_path:
        raise ReplayError("job input is missing localSourcePath")
    if not Path(local_source_path).exists():
        raise ReplayError(f"local source missing: {local_source_path}")
    original_idempotency_key = str(job.get("idempotencyKey") or source_key).strip()
    if not original_idempotency_key:
        raise ReplayError("job is missing idempotencyKey")

    replay_nonce = str(uuid4())

    replay_input = {
        **input_json,
        "replayedFromJobId": str(job.get("jobId") or "").strip() or None,
        "replaySubmittedAt": replayed_at,
    }
    if replay_reason:
        replay_input["replayReason"] = replay_reason

    return {
        "producerSystem": str(job.get("producerSystem") or "meeting-summary"),
        "producerRef": str(job.get("producerRef") or source_key),
        "resultOwner": str(job.get("resultOwner") or "meeting-summary"),
        "resultRoute": str(job.get("resultRoute") or "meeting-summary.recording.processed.v1"),
        "workloadType": str(job.get("workloadType") or "meeting_summary"),
        "queueClass": str(job.get("queueClass") or "background"),
        "executionProfile": str(job.get("executionProfile") or "cpu_media"),
        "executionShape": str(job.get("executionShape") or "local_bounded"),
        "idempotencyKey": f"{original_idempotency_key}:replay:{replay_nonce}",
        "effectKey": f"{source_key}:replay:{replay_nonce}",
        "maxAttempts": int(job.get("maxAttempts") or 3),
        "inputJson": replay_input,
    }


def replay_dead_job(*, job_id: str, replay_reason: str | None = None) -> dict:
    settings = load_settings()
    validate_settings(settings)
    client = RuntimeClient(settings)
    job = client.get_job(job_id=job_id)
    status = str(job.get("status") or "")
    if status != "dead":
        raise ReplayError(f"job {job_id} is not dead (status={status or 'unknown'})")
    if str(job.get("producerSystem") or "") != "meeting-summary":
        raise ReplayError(f"job {job_id} is not owned by meeting-summary")
    payload = build_replay_payload(job, replay_reason=replay_reason)
    return client.submit_job(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="replay a dead meeting-summary runtime job")
    parser.add_argument("job_id", help="dead runtime job id to replay")
    parser.add_argument("--reason", help="optional replay reason")
    parser.add_argument("--json", action="store_true", help="emit structured output")
    args = parser.parse_args()

    response = replay_dead_job(job_id=args.job_id, replay_reason=args.reason)
    if args.json:
        print(json.dumps(response))
    else:
        print(json.dumps(response, indent=2))


if __name__ == "__main__":
    main()
