#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from qmd_config import qmd_repo_root


def _emit(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()
    return 0


def _retryable_payload(exc: Exception) -> dict[str, Any]:
    return {
        "adapter": "qmd_embedding_fastembed",
        "outcome": "retryable_error",
        "errorClass": exc.__class__.__name__,
        "errorText": str(exc),
        "backoffSec": 60,
    }


def _load_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("bridge stdin payload is required")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("bridge stdin payload must be a JSON object")
    return payload


def _resolve_qmd_repo_root(payload: dict[str, Any]) -> Path:
    repo_root = qmd_repo_root(payload.get("qmdRepoRoot"))
    if not repo_root.exists():
        raise FileNotFoundError(f"qmd repo root not found: {repo_root}")
    return repo_root


def main() -> int:
    try:
        payload = _load_payload()
        qmd_repo_root = _resolve_qmd_repo_root(payload)
        if str(qmd_repo_root) not in sys.path:
            sys.path.insert(0, str(qmd_repo_root))

        from qmd.runtime_client import RuntimeControllerClient
        from qmd.worker import ClaimContext, _classify_error, _prepare_success_artifacts

        api_url = payload.get("apiUrl")
        bearer_token = payload.get("bearerToken")
        worker_id = payload.get("workerId")
        claim_payload = payload.get("claim") or {}
        if not isinstance(claim_payload, dict):
            raise ValueError("claim must be a JSON object")
        job = claim_payload.get("job")
        attempt = claim_payload.get("attempt")
        if not isinstance(job, dict) or not isinstance(attempt, dict):
            raise ValueError("claim must include job and attempt objects")

        runtime = RuntimeControllerClient(
            base_url=str(api_url) if isinstance(api_url, str) and api_url else None,
            bearer_token=str(bearer_token) if isinstance(bearer_token, str) and bearer_token else None,
        )
        claim = ClaimContext(job=job, attempt=attempt)
        worker_label = str(worker_id) if isinstance(worker_id, str) and worker_id else str(attempt.get("workerId") or "runtime-controller-worker")

        try:
            outcome = _prepare_success_artifacts(runtime, claim, worker_id=worker_label)
        except Exception as exc:
            classified = _classify_error(exc)
            return _emit(
                {
                    "adapter": "qmd_embedding_fastembed",
                    "outcome": classified.outcome,
                    "errorClass": classified.error_class,
                    "errorText": classified.error_text,
                    "backoffSec": classified.backoff_sec,
                }
            )

        return _emit(
            {
                "adapter": "qmd_embedding_fastembed",
                "outcome": outcome.outcome,
                "resultJson": outcome.result_json,
                "outputArtifactId": outcome.output_artifact_id,
                "errorClass": outcome.error_class,
                "errorText": outcome.error_text,
                "backoffSec": outcome.backoff_sec,
            }
        )
    except Exception as exc:
        return _emit(_retryable_payload(exc))


if __name__ == "__main__":
    raise SystemExit(main())
