from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


class ResponseLike(Protocol):
    def read(self) -> bytes: ...
    def __enter__(self) -> ResponseLike: ...
    def __exit__(self, exc_type, exc, tb) -> None: ...


class UrlOpen(Protocol):
    def __call__(self, request: urllib_request.Request, timeout: float) -> ResponseLike: ...


@dataclass(frozen=True, slots=True)
class RuntimeClientError(RuntimeError):
    message: str
    status_code: int | None = None
    payload: Any = None

    def __str__(self) -> str:
        if self.status_code is None:
            return self.message
        return f"{self.message} (status={self.status_code})"


class RuntimeClient:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8787",
        bearer_token: str = "",
        timeout: float = 30.0,
        urlopen: UrlOpen | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout = timeout
        self._urlopen = urlopen or urllib_request.urlopen

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    @staticmethod
    def _decode(raw: bytes) -> Any:
        text = raw.decode("utf-8").strip()
        if not text:
            return None
        return json.loads(text)

    def _request(self, method: str, path: str, body: dict[str, Any]) -> Any:
        request = urllib_request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method=method,
        )
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                return self._decode(response.read())
        except urllib_error.HTTPError as exc:
            payload = self._decode(exc.read())
            message = "runtime request failed"
            if isinstance(payload, dict):
                message = str(payload.get("error") or payload.get("message") or message)
            raise RuntimeClientError(message, status_code=exc.code, payload=payload) from exc
        except urllib_error.URLError as exc:
            raise RuntimeClientError(f"runtime request failed: {exc.reason}") from exc

    def _get(self, path: str, query: dict[str, Any]) -> Any:
        encoded = urllib_parse.urlencode({key: value for key, value in query.items() if value is not None})
        suffix = f"?{encoded}" if encoded else ""
        request = urllib_request.Request(
            f"{self.base_url}{path}{suffix}",
            headers=self._headers(),
            method="GET",
        )
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                return self._decode(response.read())
        except urllib_error.HTTPError as exc:
            payload = self._decode(exc.read())
            message = "runtime request failed"
            if isinstance(payload, dict):
                message = str(payload.get("error") or payload.get("message") or message)
            raise RuntimeClientError(message, status_code=exc.code, payload=payload) from exc
        except urllib_error.URLError as exc:
            raise RuntimeClientError(f"runtime request failed: {exc.reason}") from exc

    def health(self) -> dict[str, Any]:
        return self._get("/healthz", {})

    def health_summary(self, *, workloads: list[str], owners: list[str]) -> dict[str, Any]:
        return self._get("/v2/health-summary", {
            "workloadTypes": ",".join(workloads), "resultOwners": ",".join(owners), "windowSec": 21600})

    def admit_source_receipt(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/source-receipts/admit", payload)

    def submit_source_receipt(self, *, receipt_id: str, job_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/source-receipts/submit",
            {"receiptId": receipt_id, "jobId": job_id},
        )

    def get_job(self, *, job_id: str) -> dict[str, Any]:
        return self._get(f"/v1/jobs/{job_id}", {})["job"]

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/submit", payload)

    def list_jobs(
        self,
        *,
        result_owner: str | None = None,
        workload_type: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return self._get(
            "/v1/jobs",
            {
                "resultOwner": result_owner,
                "workloadType": workload_type,
                "status": status,
                "limit": limit,
            },
        )

    def cancel_job(self, *, job_id: str, reason: str) -> dict[str, Any]:
        return self._request("POST", "/v1/cancel", {"jobId": job_id, "reason": reason})

    def claim_job(
        self,
        *,
        worker_id: str,
        supported_execution_profiles: list[str],
        workload_types: list[str] | None = None,
        allowed_queue_classes: list[str] | None = None,
        queue_preference: list[str] | None = None,
        lease_duration_sec: int = 300,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "workerId": worker_id,
            "supportedExecutionProfiles": supported_execution_profiles,
            "leaseDurationSec": lease_duration_sec,
        }
        if workload_types is not None:
            payload["workloadTypes"] = workload_types
        if allowed_queue_classes is not None:
            payload["allowedQueueClasses"] = allowed_queue_classes
        if queue_preference is not None:
            payload["queuePreference"] = queue_preference
        return self._request("POST", "/v1/claim", payload)

    def mark_running(self, *, job_id: str, attempt_id: str, lease_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/running",
            {"jobId": job_id, "attemptId": attempt_id, "leaseToken": lease_token},
        )

    def heartbeat_job(
        self,
        *,
        job_id: str,
        attempt_id: str,
        lease_token: str,
        lease_duration_sec: int = 300,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/heartbeat",
            {
                "jobId": job_id,
                "attemptId": attempt_id,
                "leaseToken": lease_token,
                "leaseDurationSec": lease_duration_sec,
            },
        )

    def finalize_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/finalize", payload)

    def append_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/deliveries/append", payload)

    def get_progress(self, *, job_id: str):
        return self._get(f"/v1/jobs/{job_id}/progress", {})

    def get_delivery(self, *, delivery_id: str):
        return self._get(f"/v1/deliveries/{delivery_id}", {})

    def claim_delivery(
        self,
        *,
        claim_owner: str,
        result_owner: str | None = None,
        delivery_id: str | None = None,
        lease_duration_sec: int = 120,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "claimOwner": claim_owner,
            "deliveryProvider": "telegram",
            "leaseDurationSec": lease_duration_sec,
        }
        if result_owner is not None:
            payload["resultOwner"] = result_owner
        if delivery_id is not None:
            payload["deliveryId"] = delivery_id
        return self._request("POST", "/v1/deliveries/claim", payload)

    def heartbeat_delivery(self, *, delivery_id: str, claim_token: str,
                           lease_duration_sec: int = 120) -> dict[str, Any]:
        return self._request("POST", "/v1/deliveries/heartbeat", {
            "deliveryId": delivery_id, "claimToken": claim_token,
            "leaseDurationSec": lease_duration_sec})

    def finalize_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/deliveries/finalize", payload)
