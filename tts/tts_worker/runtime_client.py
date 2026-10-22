from __future__ import annotations

import json
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


class RuntimeClientError(RuntimeError):
    """Structured runtime error carrying HTTP status and controller code."""

    def __init__(self, message: str, *, status: int | None = None, error_code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code


class RuntimeClient:
    def __init__(self, base_url: str, bearer_token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, query: dict[str, Any] | None = None, *, timeout: float = 30) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            qs = urllib_parse.urlencode({k: v for k, v in query.items() if v not in (None, "", (), [])})
            if qs:
                url += "?" + qs
        req = urllib_request.Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None, headers=self._headers(), method=method)
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8").strip()
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            error_code = None
            try:
                parsed = json.loads(detail)
                if isinstance(parsed, dict):
                    error_code = str(parsed.get("errorCode") or parsed.get("error") or "") or None
            except json.JSONDecodeError:
                pass
            raise RuntimeClientError(
                f"runtime HTTP {exc.code}: {detail}", status=exc.code, error_code=error_code) from exc
        except urllib_error.URLError as exc:
            raise RuntimeClientError(f"runtime request failed: {exc.reason}") from exc
        return json.loads(text) if text else None

    def register_worker(self, **kwargs: Any) -> dict[str, Any]:
        body = {
            "workerId": kwargs["worker_id"],
            "workerVenue": kwargs["worker_venue"],
            "capacity": kwargs["capacity"],
            "supportedExecutionProfiles": list(kwargs["supported_execution_profiles"]),
            "workloadTypes": list(kwargs["workload_types"]),
            "producerSystems": list(kwargs.get("producer_systems", ())),
            "allowedQueueClasses": list(kwargs.get("allowed_queue_classes", ())),
            "queuePreference": list(kwargs.get("queue_preference", ())),
            "metadataJson": kwargs.get("metadata_json") or {},
        }
        return self._request("POST", "/v1/workers/register", body=body)

    def heartbeat_worker(self, *, worker_id: str, metadata_json: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", "/v1/workers/heartbeat", body={"workerId": worker_id, "metadataJson": metadata_json or {}})

    def claim_job(self, **kwargs: Any) -> dict[str, Any] | None:
        body = {
            "workerId": kwargs["worker_id"],
            "leaseDurationSec": kwargs["lease_duration_sec"],
            "supportedExecutionProfiles": list(kwargs["supported_execution_profiles"]),
            "workloadTypes": list(kwargs["workload_types"]),
            "producerSystems": list(kwargs.get("producer_systems", ("tts", "x-tts"))),
            "allowedQueueClasses": list(kwargs.get("allowed_queue_classes", ("foreground",))),
            "queuePreference": list(kwargs.get("queue_preference", ("foreground",))),
            "workerVenue": kwargs["worker_venue"],
            "workerCapacity": kwargs["capacity"],
            "workerMetadataJson": kwargs.get("metadata_json") or {},
        }
        return self._request("POST", "/v1/claim", body=body)

    def mark_running(self, *, job_id: str, attempt_id: str, lease_token: str) -> dict[str, Any]:
        return self._request("POST", "/v1/running", body={"jobId": job_id, "attemptId": attempt_id, "leaseToken": lease_token})

    def heartbeat(self, *, job_id: str, attempt_id: str, lease_token: str, lease_duration_sec: int) -> dict[str, Any]:
        return self._request("POST", "/v1/heartbeat", body={"jobId": job_id, "attemptId": attempt_id, "leaseToken": lease_token, "leaseDurationSec": lease_duration_sec})

    def finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/finalize", body=payload)

    def append_progress(self, payload: dict[str, Any]) -> Any:
        return self._request("POST", f"/v1/jobs/{payload['jobId']}/progress", body=payload, timeout=5)

    def append_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/deliveries/append", body=payload)

    def list_results(self, *, result_owner: str, limit: int = 100) -> list[dict[str, Any]]:
        # Proven contract (meeting-summary): the endpoint returns a JSON list.
        response = self._request("GET", "/v1/results", query={"resultOwner": result_owner, "limit": limit})
        return list(response or [])

    def consume_result(self, *, result_id: str, result_owner: str) -> dict[str, Any]:
        return self._request("POST", "/v1/results/consume", body={"resultId": result_id, "resultOwner": result_owner})

    def get_job(self, *, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/jobs/{job_id}")
