"""Durable TTS result projector.

Projects terminal runtime results for the ``tts`` and ``x-tts`` owners into
one idempotent Telegram delivery each, using only the proven
``GET /v1/results`` + ``POST /v1/results/consume`` contract:

    list ready results -> verify terminal -> idempotently append one
    delivery (stable key) -> consume the result only after append succeeds.

A crash after append and before consume replays the append safely through
the stable delivery key. No TTS-local receipt database exists; concurrent
projectors converge through idempotent append + already-consumed handling.
Telegram delivery remains at-least-once with best-effort deduplication.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from tg_tools_common import runtime_contract
from tg_tools_common.delivery_contract import telegram_reply_text

from .config import feed_title
from .notifications import render_failure_notification

logger = logging.getLogger(__name__)

OWNERS = ("tts", "x-tts")
TERMINAL_KINDS = {"succeeded", "dead", "failed", "terminal_error"}


def render_notification(result_kind: str, result_json: dict[str, Any]) -> str:
    if result_kind == "succeeded" and result_json.get("ok"):
        text = (f"Posted to {feed_title()}:\n{result_json.get('title', '')}\n"
                f"{result_json.get('publicAudioUrl', '')}\n"
                f"Script: {result_json.get('scriptUrl', '')}\n"
                f"Notes: {result_json.get('notesUrl', '')}\n"
                f"Feed: {result_json.get('feedUrl', '')}")
        if result_json.get("xMode"):
            usage = result_json.get("modelUsage") or {}
            text += (f"\nTokens: {usage.get('totalTokens', 'unavailable')}, "
                     f"Hangups: {len(result_json.get('hangups') or [])}")
        return text
    stage = str(result_json.get("stage") or "unknown")
    error_class = str(result_json.get("errorClass") or "unknown")
    return render_failure_notification(stage, error_class, result_json.get("message"))


def delivery_payload(*, owner: str, job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    input_json = dict(job.get("inputJson") or {})
    result_json = dict(result.get("resultJson") or {})
    ref = dict(input_json.get("messageRef") or {})
    text = render_notification(str(result.get("resultKind") or ""), result_json)
    payload_json = telegram_reply_text(
        chat_id=int(ref.get("chat_id") or ref.get("chatId") or 0),
        thread_id=ref.get("message_thread_id") or ref.get("threadId"),
        reply_to_message_id=int(ref.get("message_id") or ref.get("messageId") or 0),
        text=text)
    source_key = str(input_json.get("sourceKey") or job.get("jobId") or "")
    payload = runtime_contract.build_delivery_payload(
        result_owner=owner,
        result_route=str(job.get("resultRoute") or f"telegram.{owner}.confirmation.v1"),
        # Stable key: identical to the worker's direct-append key so the
        # cutover window cannot double-notify.
        delivery_key_str=f"{owner}:telegram-confirmation:{source_key}",
        priority=63, payload_json=payload_json, max_attempts=5)
    payload["deliveryKind"] = payload_json["deliveryKind"]
    payload["jobId"] = job.get("jobId")
    payload["resultId"] = result.get("resultId")
    return payload


class ResultProjector:
    def __init__(self, client: Any, owners: tuple[str, ...] = OWNERS) -> None:
        # Fail closed: explicit owners only, never result_owner=None.
        owners = tuple(o for o in owners if isinstance(o, str) and o.strip())
        if not owners:
            raise ValueError("ResultProjector requires explicit result owners")
        self.client = client
        self.owners = owners

    def project_once(self) -> int:
        """One fair pass across owners; returns projected-result count."""
        projected = 0
        for owner in self.owners:
            try:
                results = self.client.list_results(result_owner=owner, limit=200)
            except Exception:
                logger.exception("result list failed owner=%s", owner)
                continue
            for result in results or []:
                try:
                    projected += 1 if self._project(owner, dict(result)) else 0
                except Exception:
                    logger.exception("projection failed owner=%s result=%s",
                                     owner, result.get("resultId"))
        return projected

    def _project(self, owner: str, result: dict[str, Any]) -> bool:
        result_kind = str(result.get("resultKind") or "")
        if result_kind not in TERMINAL_KINDS:
            # Retry/intermediate rows must never notify; leave unconsumed.
            logger.info("skipping non-terminal result kind=%s id=%s",
                        result_kind, result.get("resultId"))
            return False
        job = self.client.get_job(job_id=str(result["jobId"]))
        job = dict(job.get("job") or job) if isinstance(job, dict) else {}
        # Validate before append: a malformed row must stay unconsumed for
        # repair rather than becoming an undeliverable outbox payload.
        input_json = dict(job.get("inputJson") or {})
        ref = dict(input_json.get("messageRef") or {})
        job_owner = str(job.get("resultOwner") or owner)
        problems = []
        if str(job.get("jobId") or "") != str(result.get("jobId") or ""):
            problems.append("job lookup mismatch")
        if job_owner != owner:
            problems.append(f"job owner {job_owner!r} != {owner!r}")
        if not str(input_json.get("sourceKey") or ""):
            problems.append("empty sourceKey")
        if int(ref.get("chat_id") or ref.get("chatId") or 0) == 0:
            problems.append("missing chat id")
        if int(ref.get("message_id") or ref.get("messageId") or 0) == 0:
            problems.append("missing message id")
        if problems:
            logger.error("unprojectable result id=%s: %s (left unconsumed)",
                         result.get("resultId"), "; ".join(problems))
            return False
        # Idempotent append first; consume only after append succeeds.
        self.client.append_delivery(delivery_payload(owner=owner, job=job, result=result))
        self.client.consume_result(result_id=str(result["resultId"]), result_owner=owner)
        return True


def run_loop(client: Any, *, poll_interval_s: float = 5.0, stop: Any | None = None) -> None:
    projector = ResultProjector(client)
    while stop is None or not stop.is_set():
        if projector.project_once() == 0:
            time.sleep(poll_interval_s)


def main() -> None:  # pragma: no cover - service entry
    from .config import load_settings
    from .runtime_client import RuntimeClient

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = load_settings()
    run_loop(RuntimeClient(settings.runtime_url, settings.runtime_bearer_token))
