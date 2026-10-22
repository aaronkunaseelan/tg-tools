from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from config import Settings


@dataclass(frozen=True)
class RuntimeClientError(RuntimeError):
    message: str
    status_code: int | None = None
    payload: Any = None

    def __str__(self) -> str:
        if self.status_code is None:
            return self.message
        return f"{self.message} (status={self.status_code})"


class RuntimeClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.runtime_controller_url.rstrip("/")
        self.bearer_token = settings.runtime_controller_bearer_token

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def _decode_response(self, payload: bytes) -> Any:
        text = payload.decode("utf-8").strip()
        if not text:
            return None
        return json.loads(text)

    def _request(self, method: str, path: str, *, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            encoded_query = urllib_parse.urlencode(
                {
                    key: ",".join(value) if isinstance(value, (tuple, list)) else value
                    for key, value in query.items()
                    if value is not None and value != "" and value != () and value != []
                }
            )
            if encoded_query:
                url = f"{url}?{encoded_query}"

        request = urllib_request.Request(
            url,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=self._headers() if path.startswith("/v1/") else {"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib_request.urlopen(request, timeout=30) as response:
                return self._decode_response(response.read())
        except urllib_error.HTTPError as exc:
            payload = self._decode_response(exc.read())
            message = None
            if isinstance(payload, dict):
                message = str(payload.get("error") or payload.get("message") or "runtime request failed")
            raise RuntimeClientError(message or "runtime request failed", status_code=exc.code, payload=payload) from exc
        except urllib_error.URLError as exc:
            raise RuntimeClientError(f"runtime request failed: {exc.reason}") from exc

    def _default_worker_touch_result(self, worker_id: str) -> dict[str, Any]:
        return {
            "worker": {"workerId": worker_id},
            "control": {
                "action": "continue",
                "reason": None,
                "deadlineAt": None,
            },
        }

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/submit", body=payload)

    def admit_source_receipt(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/source-receipts/admit", body=payload)

    def submit_source_receipt(self, *, receipt_id: str, job_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/source-receipts/submit",
            body={"receiptId": receipt_id, "jobId": job_id},
        )

    def register_worker(
        self,
        *,
        worker_id: str,
        worker_venue: str,
        capacity: int,
        supported_execution_profiles: tuple[str, ...],
        workload_types: tuple[str, ...],
        producer_systems: tuple[str, ...] = (),
        allowed_queue_classes: tuple[str, ...] = (),
        queue_preference: tuple[str, ...] = (),
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "workerId": worker_id,
            "workerVenue": worker_venue,
            "capacity": capacity,
            "supportedExecutionProfiles": list(supported_execution_profiles),
            "workloadTypes": list(workload_types),
            "metadataJson": metadata_json or {},
        }
        if producer_systems:
            body["producerSystems"] = list(producer_systems)
        if allowed_queue_classes:
            body["allowedQueueClasses"] = list(allowed_queue_classes)
        if queue_preference:
            body["queuePreference"] = list(queue_preference)
        try:
            return self._request("POST", "/v1/workers/register", body=body)
        except RuntimeClientError as exc:
            if exc.status_code == 404:
                return self._default_worker_touch_result(worker_id)
            raise

    def heartbeat_worker(self, *, worker_id: str, metadata_json: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {"workerId": worker_id, "metadataJson": metadata_json or {}}
        try:
            return self._request("POST", "/v1/workers/heartbeat", body=body)
        except RuntimeClientError as exc:
            if exc.status_code == 404:
                return self._default_worker_touch_result(worker_id)
            raise

    def claim_job(
        self,
        *,
        worker_id: str,
        supported_execution_profiles: tuple[str, ...],
        workload_types: tuple[str, ...],
        producer_systems: tuple[str, ...] = (),
        allowed_queue_classes: tuple[str, ...] = (),
        queue_preference: tuple[str, ...] = (),
        worker_venue: str | None = None,
        capacity: int | None = None,
        metadata_json: dict[str, Any] | None = None,
        lease_duration_sec: int,
    ) -> dict[str, Any] | None:
        body: dict[str, Any] = {
            "workerId": worker_id,
            "supportedExecutionProfiles": list(supported_execution_profiles),
            "workloadTypes": list(workload_types),
            "leaseDurationSec": lease_duration_sec,
        }
        if producer_systems:
            body["producerSystems"] = list(producer_systems)
        if allowed_queue_classes:
            body["allowedQueueClasses"] = list(allowed_queue_classes)
        if queue_preference:
            body["queuePreference"] = list(queue_preference)
        if worker_venue:
            body["workerVenue"] = worker_venue
        if capacity is not None:
            body["workerCapacity"] = capacity
        if metadata_json:
            body["workerMetadataJson"] = metadata_json
        return self._request("POST", "/v1/claim", body=body)

    def mark_running(self, *, job_id: str, attempt_id: str, lease_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/running",
            body={"jobId": job_id, "attemptId": attempt_id, "leaseToken": lease_token},
        )

    def heartbeat(self, *, job_id: str, attempt_id: str, lease_token: str, lease_duration_sec: int) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/heartbeat",
            body={
                "jobId": job_id,
                "attemptId": attempt_id,
                "leaseToken": lease_token,
                "leaseDurationSec": lease_duration_sec,
            },
        )

    def finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/finalize", body=payload)

    def list_results(self, *, result_owner: str, limit: int = 100) -> list[dict[str, Any]]:
        response = self._request("GET", "/v1/results", query={"resultOwner": result_owner, "limit": limit})
        return list(response or [])

    def consume_result(self, *, result_id: str, result_owner: str) -> dict[str, Any] | None:
        return self._request("POST", "/v1/results/consume", body={"resultId": result_id, "resultOwner": result_owner})

    def append_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/deliveries/append", body=payload)

    def claim_delivery(
        self,
        *,
        claim_owner: str,
        delivery_provider: str,
        result_owner: str,
        lease_duration_sec: int = 120,
    ) -> dict[str, Any] | None:
        return self._request(
            "POST",
            "/v1/deliveries/claim",
            body={
                "claimOwner": claim_owner,
                "deliveryProvider": delivery_provider,
                "resultOwner": result_owner,
                "leaseDurationSec": lease_duration_sec,
            },
        )

    def finalize_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/deliveries/finalize", body=payload)

    def get_job(self, *, job_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/v1/jobs/{job_id}")
        if not isinstance(response, dict) or not isinstance(response.get("job"), dict):
            raise RuntimeClientError("runtime job lookup returned invalid payload", payload=response)
        return dict(response["job"])

    def list_jobs(self, **query: Any) -> dict[str, Any]:
        response = self._request("GET", "/v1/jobs", query=query)
        if not isinstance(response, dict):
            raise RuntimeClientError("runtime job listing returned invalid payload", payload=response)
        return response

    def health(self) -> dict[str, Any]:
        response = self._request("GET", "/healthz")
        if not isinstance(response, dict):
            raise RuntimeClientError("runtime health returned invalid payload", payload=response)
        return response
