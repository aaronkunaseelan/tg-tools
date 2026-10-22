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

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            encoded = urllib_parse.urlencode(
                {
                    key: ",".join(value) if isinstance(value, (tuple, list)) else value
                    for key, value in query.items()
                    if value is not None and value != "" and value != () and value != []
                }
            )
            if encoded:
                url = f"{url}?{encoded}"

        request = urllib_request.Request(
            url,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=self._headers() if path.startswith("/v1/") else {"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib_request.urlopen(request, timeout=timeout) as response:
                return self._decode_response(response.read())
        except urllib_error.HTTPError as exc:
            payload = self._decode_response(exc.read())
            message = None
            if isinstance(payload, dict):
                message = str(payload.get("error") or payload.get("message") or "runtime request failed")
            raise RuntimeClientError(message or "runtime request failed", status_code=exc.code, payload=payload) from exc
        except urllib_error.URLError as exc:
            raise RuntimeClientError(f"runtime request failed: {exc.reason}") from exc

    def health(self) -> dict[str, Any]:
        response = self._request("GET", "/healthz")
        if not isinstance(response, dict):
            raise RuntimeClientError("runtime health returned invalid payload", payload=response)
        return response

    def admit_source_receipt(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/source-receipts/admit", body=payload)

    def submit_source_receipt(self, *, receipt_id: str, job_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/source-receipts/submit",
            body={"receiptId": receipt_id, "jobId": job_id},
        )

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/submit", body=payload)

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
        return self._request("POST", "/v1/workers/register", body=body)

    def heartbeat_worker(self, *, worker_id: str, metadata_json: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/workers/heartbeat",
            body={"workerId": worker_id, "metadataJson": metadata_json or {}},
        )

    def claim_job(
        self,
        *,
        worker_id: str,
        lease_duration_sec: int,
        supported_execution_profiles: tuple[str, ...] = ("cpu_media",),
        workload_types: tuple[str, ...] = ("rss_publish",),
        producer_systems: tuple[str, ...] = ("rss-audio",),
        allowed_queue_classes: tuple[str, ...] = ("foreground",),
        queue_preference: tuple[str, ...] = ("foreground",),
        worker_venue: str = "oci",
        capacity: int = 1,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self._request(
            "POST",
            "/v1/claim",
            body={
                "workerId": worker_id,
                "leaseDurationSec": lease_duration_sec,
                "supportedExecutionProfiles": list(supported_execution_profiles),
                "workloadTypes": list(workload_types),
                "producerSystems": list(producer_systems),
                "allowedQueueClasses": list(allowed_queue_classes),
                "queuePreference": list(queue_preference),
                "workerVenue": worker_venue,
                "workerCapacity": capacity,
                "workerMetadataJson": metadata_json or {},
            },
        )

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

    def append_progress(self, payload: dict[str, Any]):
        return self._request("POST", f"/v1/jobs/{payload['jobId']}/progress", body=payload, timeout=5)

    def register_artifact(self, payload):
        return self._request("POST", "/v1/artifacts/register", body=payload, timeout=5)

    def list_results(self, *, result_owner: str, limit: int = 100):
        return self._request("GET", "/v1/results", query={"resultOwner": result_owner, "limit": limit}, timeout=5)

    def get_job(self, *, job_id: str):
        return self._request("GET", f"/v1/jobs/{job_id}", timeout=5)

    def consume_result(self, *, result_id: str, result_owner: str):
        return self._request("POST", "/v1/results/consume", body={"resultId": result_id, "resultOwner": result_owner}, timeout=5)

    def append_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/deliveries/append", body=payload)

    def claim_delivery(
        self,
        *,
        claim_owner: str,
        delivery_provider: str = "telegram",
        result_owner: str = "rss-audio",
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
