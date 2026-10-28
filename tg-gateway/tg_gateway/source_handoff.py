"""Gateway-owned transactional source-handoff outbox.

The gateway cannot atomically pair Telegram receipt persistence with
runtime-controller calls, so routed non-command work is first recorded as
one durable handoff row, then drained to an idempotent runtime source receipt
+ job submit.
The handoff ends at job submission; it never mirrors runtime job, attempt,
or retry state.

Handoff states: pending -> staged -> submitted | terminal_rejected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

INTENT_SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_TELEGRAM_BYTES = 50 * 1024 * 1024

# Raw text is prohibited except for the one canonical generic-source field.
_FORBIDDEN_INTENT_KEYS = {
    "prompt_text", "promptText", "promptBody", "rawPrompt",
    "body", "text", "receiptText", "deliveryText",
    "promptRef", "promptTextSha256", "promptTextLength",
    "textRef", "textSha256", "textLength", "privateInputRef",
    "sourceTextSha256", "sourceTextLength",
    "attemptId", "leaseToken",
}
_SOURCE_TEXT_PATH = "$.jobSubmitPayload.inputJson.sourceText"
_SOURCE_URL_PATH = "$.jobSubmitPayload.inputJson.sourceUrl"


class HandoffRejected(RuntimeError):
    """Deterministic staging/admission rejection (terminal for this update)."""

    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


class HandoffLeaseLost(RuntimeError):
    """A fenced mutation found the claim token stale; another drainer owns
    the row. Abort without further mutation."""


def validate_intent(intent: dict[str, Any]) -> None:
    if intent.get("schemaVersion") != INTENT_SCHEMA_VERSION:
        raise ValueError("intent schemaVersion mismatch")
    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}.{key}"
                if key == "sourceText":
                    if child_path != _SOURCE_TEXT_PATH:
                        raise ValueError(f"sourceText is forbidden at {child_path}")
                    if not isinstance(value, str) or not value:
                        raise ValueError("sourceText must be a nonempty string")
                if key == "sourceUrl":
                    if child_path != _SOURCE_URL_PATH:
                        raise ValueError(f"sourceUrl is forbidden at {child_path}")
                    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
                        raise ValueError("sourceUrl must be an HTTP(S) string")
                if key in _FORBIDDEN_INTENT_KEYS:
                    raise ValueError(f"forbidden intent key {key!r} at {child_path}")
                _walk(value, child_path)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                _walk(value, f"{path}[{i}]")
    _walk(intent, "$")


def build_intent(
    *,
    source_receipt_payload: dict[str, Any],
    job_submit_payload: dict[str, Any],
    attachment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Versioned frozen admission payloads plus an attachment spec."""
    intent = {
        "schemaVersion": INTENT_SCHEMA_VERSION,
        "sourceReceiptPayload": source_receipt_payload,
        "jobSubmitPayload": job_submit_payload,
        "attachment": attachment or None,
    }
    validate_intent(intent)
    return intent


class HandoffStore(Protocol):
    """All mutations after claim are fenced by the claim's lease_token and
    must raise HandoffLeaseLost when the token is stale."""

    def create_handoff(self, row: dict[str, Any]) -> bool: ...
    def claim_next(self, *, owner: str, bot_identity: str, lease_seconds: int, update_uid: str | None = None) -> dict[str, Any] | None: ...
    def record_staged(self, handoff_id: int, *, lease_token: str, path: str, sha256: str, size: int, intent_json: dict[str, Any]) -> None: ...
    def record_receipt(self, handoff_id: int, receipt_id: str, *, lease_token: str) -> None: ...
    def record_job(self, handoff_id: int, job_id: str, *, lease_token: str) -> None: ...
    def mark_submitted(self, handoff_id: int, *, lease_token: str) -> None: ...
    def mark_terminal_rejected(self, handoff_id: int, error: str, *, lease_token: str) -> None: ...


class RuntimeHandoffClient(Protocol):
    def admit_source_receipt(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def submit_source_receipt(self, *, receipt_id: str, job_id: str) -> dict[str, Any]: ...
    def append_delivery(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def _sha256_file(path: Path, cap: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > cap:
                raise HandoffRejected("source_too_large", f"staged file exceeds {cap} bytes")
            digest.update(chunk)
    return digest.hexdigest(), size


def _safe_staged_path(staging_root: Path, source_key: str, file_name: str) -> Path:
    safe_key = "".join(c if c.isalnum() or c in "-_." else "_" for c in source_key)
    name = Path(file_name or "attachment.bin").name
    if not name or name.startswith("."):
        name = "attachment.bin"
    target = (staging_root / safe_key / name).resolve()
    if staging_root.resolve() not in target.parents:
        raise HandoffRejected("source_type_mismatch", "staged path escapes staging root")
    return target


@dataclass
class SourceHandoffDrainer:
    store: HandoffStore
    runtime: RuntimeHandoffClient
    staging_root: Path
    # downloader(file_id, target_path) -> local path (str); must write exact bytes.
    downloader: Callable[[str, Path], str]
    owner: str = "tg-gateway"
    bot_identity: str = "rss-audio"
    max_telegram_bytes: int = DEFAULT_MAX_TELEGRAM_BYTES
    lease_seconds: int = DEFAULT_LEASE_SECONDS

    def drain_once(self, *, update_uid: str | None = None) -> str:
        """Process at most one pending/staged handoff. Returns outcome."""
        handoff = self.store.claim_next(
            owner=self.owner, bot_identity=self.bot_identity, lease_seconds=self.lease_seconds,
            update_uid=update_uid)
        if handoff is None:
            return "idle"
        handoff_id = int(handoff["handoff_id"])
        lease_token = str(handoff.get("lease_token") or "")
        intent = dict(handoff["intent_json"])
        try:
            validate_intent(intent)
            intent = self._ensure_staged(handoff_id, handoff, intent, lease_token)
            self._submit(handoff_id, handoff, intent, lease_token)
        except HandoffLeaseLost:
            logger.warning("handoff %s lease lost during drain; aborting", handoff_id)
            return "lease_lost"
        except HandoffRejected as exc:
            self._reject(handoff_id, handoff, intent, exc, lease_token)
            return "terminal_rejected"
        return "submitted"

    # -- staging -------------------------------------------------------------

    def _ensure_staged(self, handoff_id: int, handoff: dict[str, Any], intent: dict[str, Any], lease_token: str) -> dict[str, Any]:
        attachment = intent.get("attachment")
        if not attachment:
            return intent
        job_input = dict(intent["jobSubmitPayload"].get("inputJson") or {})
        if handoff.get("staged_sha256") and handoff.get("staged_path"):
            staged = Path(str(handoff["staged_path"]))
            # Reclaim: verify or safely re-download the same source.
            if staged.is_file() and not staged.is_symlink():
                sha, size = _sha256_file(staged, self.max_telegram_bytes)
                if sha == handoff["staged_sha256"]:
                    return intent
        file_id = str(attachment.get("fileId") or attachment.get("sourceFileId") or "")
        if not file_id:
            local = attachment.get("localSourcePath") or attachment.get("sourcePath")
            if local:
                staged = Path(str(local))
                sha, size = _sha256_file(staged, self.max_telegram_bytes)
            else:
                raise HandoffRejected("source_type_mismatch", "attachment has no file id or path")
        else:
            declared = attachment.get("fileSize") or attachment.get("fileSizeBytes")
            if declared is not None and int(declared) > self.max_telegram_bytes:
                raise HandoffRejected(
                    "source_too_large",
                    f"attachment declares {declared} bytes over cap {self.max_telegram_bytes}")
            target = _safe_staged_path(
                self.staging_root, str(handoff["source_key"]),
                str(attachment.get("fileName") or ""))
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = Path(self.downloader(file_id, target))
            if staged.is_symlink() or not staged.is_file():
                raise HandoffRejected("source_type_mismatch", "staged attachment is not a regular file")
            sha, size = _sha256_file(staged, self.max_telegram_bytes)
        # Persist bytes and their directory entry before committing retention.
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        directory = os.open(staged.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        job_input.update({
            "localSourcePath": str(staged),
            "stagedSha256": sha,
            "stagedSizeBytes": size,
        })
        intent = dict(intent)
        intent["jobSubmitPayload"] = {**intent["jobSubmitPayload"], "inputJson": job_input}
        # Freeze payloads after staging; persist before any runtime call.
        self.store.record_staged(
            handoff_id, lease_token=lease_token, path=str(staged), sha256=sha,
            size=size, intent_json=intent)
        return intent

    # -- submission ----------------------------------------------------------

    def _submit(self, handoff_id: int, handoff: dict[str, Any], intent: dict[str, Any], lease_token: str) -> None:
        receipt_id = handoff.get("receipt_id")
        if not receipt_id:
            receipt_result = self.runtime.admit_source_receipt(intent["sourceReceiptPayload"])
            receipt = receipt_result.get("receipt") if isinstance(receipt_result, dict) else None
            receipt_id = str(receipt.get("receiptId")) if isinstance(receipt, dict) and receipt.get("receiptId") else None
            if not receipt_id:
                # A submitted handoff must have a linked receipt; leave the
                # row claimable and retry rather than submitting unlinked.
                raise RuntimeError("runtime admit_source_receipt returned no receiptId")
            self.store.record_receipt(handoff_id, receipt_id, lease_token=lease_token)
        job_id = handoff.get("job_id")
        if not job_id:
            job_result = self.runtime.submit_job(intent["jobSubmitPayload"])
            job = job_result.get("job") if isinstance(job_result, dict) else None
            job_id = str(job.get("jobId")) if isinstance(job, dict) and job.get("jobId") else None
            if not job_id:
                raise RuntimeError("runtime job submit returned no jobId")
            self.store.record_job(handoff_id, job_id, lease_token=lease_token)
        self.runtime.submit_source_receipt(receipt_id=str(receipt_id), job_id=str(job_id))
        self.store.mark_submitted(handoff_id, lease_token=lease_token)

    # -- rejection -----------------------------------------------------------

    def _reject(self, handoff_id: int, handoff: dict[str, Any], intent: dict[str, Any], exc: HandoffRejected, lease_token: str) -> None:
        job_payload = intent.get("jobSubmitPayload") or {}
        owner = str(job_payload.get("resultOwner") or self.bot_identity)
        input_json = dict(job_payload.get("inputJson") or {})
        ref = dict(input_json.get("messageRef") or {})
        source_key = str(handoff.get("source_key") or "")
        delivery = {
            "resultOwner": owner,
            "resultRoute": str(job_payload.get("resultRoute") or f"telegram.{owner}.confirmation.v1"),
            "deliveryKey": f"{owner}:handoff-rejected:{source_key}",
            "deliveryKind": "telegram_reply_text",
            "priority": 63,
            "maxAttempts": 5,
            "payloadJson": {
                "deliveryKind": "telegram_reply_text",
                "chatId": int(ref.get("chat_id") or 0),
                "threadId": ref.get("message_thread_id"),
                "replyToMessageId": ref.get("message_id"),
                "text": f"Input rejected at staging: {exc.error_class}: {exc}",
            },
        }
        # Durable delivery intent first; only then terminal state.
        self.runtime.append_delivery(delivery)
        self.store.mark_terminal_rejected(handoff_id, f"{exc.error_class}: {exc}", lease_token=lease_token)


class PostgresSourceHandoffStore:
    """Postgres source-handoff store."""

    def __init__(self, *, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self):  # pragma: no cover - thin wrapper
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def create_handoff(self, row: dict[str, Any]) -> bool:
        validate_intent(dict(row["intent_json"]))
        with self._connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO tg_gateway.source_handoffs
                    (update_uid, source_key, bot_identity, route_id, route_kind,
                     rule_version, intent_json, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'pending')
                ON CONFLICT (update_uid) DO NOTHING
                RETURNING handoff_id
                """,
                (
                    str(row["update_uid"]), str(row["source_key"]),
                    str(row["bot_identity"]), str(row["route_id"]),
                    str(row["route_kind"]), str(row["rule_version"]),
                    json.dumps(row["intent_json"], separators=(",", ":"), ensure_ascii=True),
                ),
            ).fetchone()
            if inserted is None:
                return False
            if row.get("progress_target"):
                conn.execute("""INSERT INTO tg_gateway.progress_bindings
                    (handoff_id,bot_identity,source_key,target_json) VALUES(%s,%s,%s,%s::jsonb)
                    ON CONFLICT(bot_identity,source_key) DO NOTHING""",
                    (inserted["handoff_id"],row["bot_identity"],row["source_key"],
                     json.dumps(row["progress_target"])))
        return True

    def claim_next(self, *, owner: str, bot_identity: str, lease_seconds: int, update_uid: str | None = None) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT handoff_id FROM tg_gateway.source_handoffs
                 WHERE bot_identity = %s
                   AND state IN ('pending', 'staged')
                   AND (%s::text IS NULL OR update_uid = %s)
                   AND (lease_expires_at IS NULL OR lease_expires_at < now())
                 ORDER BY handoff_id
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
                """,
                (bot_identity, update_uid, update_uid),
            ).fetchone()
            if row is None:
                return None
            lease_token = secrets.token_hex(16)
            claimed = conn.execute(
                """
                UPDATE tg_gateway.source_handoffs
                   SET lease_owner = %s,
                       lease_token = %s,
                       lease_expires_at = %s,
                       attempts = attempts + 1,
                       updated_at = now()
                 WHERE handoff_id = %s
                RETURNING handoff_id, update_uid, source_key, bot_identity,
                          route_id, route_kind, rule_version, intent_json,
                          state, staged_path, staged_sha256, staged_size,
                          receipt_id, job_id, attempts, lease_token
                """,
                (owner, lease_token, now + timedelta(seconds=lease_seconds), int(row["handoff_id"])),
            ).fetchone()
        return dict(claimed) if claimed else None

    def record_staged(self, handoff_id: int, *, lease_token: str, path: str, sha256: str, size: int, intent_json: dict[str, Any]) -> None:
        self._update(
            "SET state = 'staged', staged_path = %s, staged_sha256 = %s, staged_size = %s, intent_json = %s::jsonb, updated_at = now()",
            (path, sha256, size, json.dumps(intent_json, separators=(",", ":"), ensure_ascii=True)), handoff_id, lease_token)

    def record_receipt(self, handoff_id: int, receipt_id: str, *, lease_token: str) -> None:
        self._update("SET receipt_id = %s, updated_at = now()", (receipt_id,), handoff_id, lease_token)

    def record_job(self, handoff_id: int, job_id: str, *, lease_token: str) -> None:
        self._update("SET job_id = %s, updated_at = now()", (job_id,), handoff_id, lease_token)

    def mark_submitted(self, handoff_id: int, *, lease_token: str) -> None:
        self._update("SET state = 'submitted', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = now()", (), handoff_id, lease_token)

    def mark_terminal_rejected(self, handoff_id: int, error: str, *, lease_token: str) -> None:
        self._update("SET state = 'terminal_rejected', last_error = %s, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = now()", (error[:2000],), handoff_id, lease_token)

    def _update(self, set_clause: str, params: tuple, handoff_id: int, lease_token: str) -> None:
        """Fenced mutation: only the current claim token may mutate, and only
        while the row is still in an in-flight state."""
        with self._connect() as conn:
            row = conn.execute(
                f"""UPDATE tg_gateway.source_handoffs {set_clause}
                 WHERE handoff_id = %s
                   AND lease_token = %s
                   AND state IN ('pending', 'staged')
                RETURNING handoff_id""",
                (*params, handoff_id, lease_token)).fetchone()
        if row is None:
            raise HandoffLeaseLost(f"handoff {handoff_id} claim token is stale")
