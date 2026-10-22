"""Durable progress presentation, using the existing provider outbox authority."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import hashlib
import html
import logging
import json

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)
class StreamBusy(RuntimeError):
    pass
TERMINAL = {"succeeded", "dead", "cancelled"}
LABELS = {
    "acquire_source": "Fetching source", "extract_document": "Extracting document",
    "prepare_reading": "Preparing reading", "write_explanation": "Writing explanation",
    "review_explanation": "Reviewing explanation", "revise_explanation": "Revising explanation",
    "synthesize_speech": "Generating audio", "generation_checkpoint": "Generation checkpoint",
    "publication_checkpoint": "Publication checkpoint",
    "source_metadata": "Reading source details", "download_audio": "Downloading audio",
    "convert_audio": "Preparing audio", "split_audio": "Splitting audio",
    "transcribe_chunks": "Transcribing audio", "transcript_ready": "Preparing transcript",
    "transcript_checkpoint": "Transcript checkpoint", "prepare_paragraphs": "Preparing paragraphs",
    "topic_analysis": "Identifying topics and writing summary", "topic_checkpoint": "Topic checkpoint",
    "reference_extraction": "Extracting references and checking quotes", "reference_checkpoint": "Reference checkpoint",
    "upload_audio": "Uploading audio", "upload_document": "Uploading documents",
    "check_publication": "Checking for an existing episode",
    "publication_retry": "Waiting to retry publication service",
    "publish_episode": "Publishing episode", "source_retry": "Waiting to retry source",
}


def _date(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def render_status(snapshot, *, now=None):
    now = now or _date(snapshot["serverTime"])
    job, attempt = snapshot["job"], snapshot.get("attempt")
    state = job["status"]
    labels = {"pending": "Queued", "admitted": "Queued", "queued": "Queued", "retryable": "Waiting to retry",
              "succeeded": "Processing complete — final reply follows", "dead": "Processing failed — final reply follows", "cancelled": "Cancelled"}
    header = labels.get(state, "Processing")
    lines = [f"<b>{html.escape(header)}</b> · {_duration((now - _date(job['createdAt'])).total_seconds())} elapsed"]
    stages = snapshot.get("stages") or []
    source = next((s.get("facts") or {} for s in reversed(stages) if (s.get("facts") or {}).get("title")), {})
    if source:
        lines.append(html.escape(source["title"]))
        if source.get("audioDurationSeconds"):
            lines.append("Audio length: " + _duration(source["audioDurationSeconds"]))
    completed = [s for s in stages if s["kind"] in {"completed", "reused"}]
    for s in completed[-5:]:
        facts = s.get("facts") or {}
        detail = []
        if "durationSeconds" in facts: detail.append(_duration(facts["durationSeconds"]))
        for key in ("words", "topics", "references"):
            if key in facts: detail.append(f"{int(facts[key]):,} {key}")
        if s["kind"] == "reused": detail.append("reused")
        label = LABELS.get(s["stage"], s["stage"].replace("_", " "))
        lines.append("✓ " + html.escape(label + (" — " + ", ".join(detail) if detail else "")))
    active = [s for s in stages if s["kind"] in {"started", "advanced", "warning"}]
    if state == "leased" and active:
        s = active[-1]
        label = LABELS.get(s["stage"], s["stage"].replace("_", " "))
        lines += ["", f"<b>Working: {html.escape(label)}</b>",
                  f"This stage: {_duration((now - _date(s['startedAt'])).total_seconds())}"]
        f = s.get("facts") or {}
        if "completed" in f and "total" in f:
            lines.append(f"{int(f['completed'])}/{int(f['total'])} {html.escape(str(f.get('units', 'items')))} complete")
        if "retrySeconds" in f: lines.append(f"Source retry in approximately {int(f['retrySeconds'])} seconds.")
    if state == "retryable":
        lines.append("Next attempt: " + html.escape(str(job.get("runnableAt") or "unknown")))
    if state == "leased" and attempt:
        age = (now - _date(attempt.get("heartbeatAt") or attempt["createdAt"])).total_seconds()
        expired = now >= _date(attempt["leaseExpiresAt"])
        lines += ["", "Attempt authority expired; awaiting reconciliation." if expired else
                  f"Worker updates interrupted; last check-in {_duration(age)} ago." if age > 195 else
                  f"Worker check-in {_duration(age)} ago."]
        if active and active[-1]["stage"] in {"topic_analysis", "reference_extraction"}:
            lines.append("Waiting for the analysis result; no intermediate result yet.")
    return "\n".join(lines)[:2000]


class ProgressStore:
    def __init__(self, *, dsn, bot_identity):
        self.dsn, self.bot_identity = dsn, bot_identity

    def validate_schema(self):
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute("SELECT to_regclass('tg_gateway.progress_bindings'),to_regclass('runtime.progress_snapshots'),to_regclass('tg_gateway.delivery_dispatches')").fetchone()
            if not all(row):
                raise RuntimeError("progress enabled before required additive migrations")

    @contextlib.contextmanager
    def locked(self, binding_id):
        # Session ownership persists across local commits and through provider I/O.
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            lock = int.from_bytes(hashlib.sha256(str(binding_id).encode()).digest()[:8], "big", signed=True)
            if not conn.execute("SELECT pg_try_advisory_lock(%s)", (lock,)).fetchone()["pg_try_advisory_lock"]:
                raise StreamBusy("progress stream already has an editor")
            try:
                yield conn
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (lock,))

    @contextlib.contextmanager
    def pre_dispatch(self, payload):
        meta = payload.get("progressBinding")
        if not meta:
            yield True
            return
        with self.locked(meta["id"]) as conn:
            row = conn.execute("SELECT * FROM tg_gateway.progress_bindings WHERE binding_id=%s", (meta["id"],)).fetchone()
            valid = bool(row and row["bot_identity"] == self.bot_identity and not row["quarantined"]
                         and not row["closed"] and row["revision"] == meta["revision"]
                         and row["desired_payload"] == payload and row["target_json"]["chatId"] == payload["chatId"])
            if valid and payload["deliveryKind"] == "telegram_edit":
                handoff = conn.execute("SELECT job_id FROM tg_gateway.source_handoffs WHERE handoff_id=%s", (row["handoff_id"],)).fetchone()
                if handoff["job_id"]:
                    snapshot = self.client.get_progress(job_id=str(handoff["job_id"]))
                    valid = snapshot["version"] == meta["version"] and (snapshot["job"]["status"] in TERMINAL) == meta["terminal"]
            yield valid

    def after_delivery(self, payload, message_id):
        meta = payload.get("progressBinding")
        if not meta: return
        with self.locked(meta["id"]) as conn:
            conn.execute("""UPDATE tg_gateway.progress_bindings SET message_id=%s,
                acknowledged_revision=%s,pending_delivery_id=NULL,closed=%s,
                next_refresh_at=clock_timestamp()+interval '5 seconds'
                WHERE binding_id=%s AND bot_identity=%s AND revision=%s""",
                (message_id,meta["revision"],bool(meta["terminal"]),meta["id"],self.bot_identity,meta["revision"]))


class ProgressProjector:
    def __init__(self, *, store, client):
        self.store, self.client = store, client

    def project_once(self):
        with psycopg.connect(self.store.dsn, row_factory=dict_row) as conn:
            due = conn.execute("""SELECT binding_id FROM tg_gateway.progress_bindings
                WHERE bot_identity=%s AND NOT closed AND NOT quarantined AND next_refresh_at<=clock_timestamp()
                ORDER BY next_refresh_at,binding_id LIMIT 20""",(self.store.bot_identity,)).fetchall()
        for row in due:
            try: self._project(row["binding_id"])
            except StreamBusy: continue
            except Exception: log.exception("progress projection failed binding=%s", row["binding_id"])

    def _project(self, binding_id):
        with self.store.locked(binding_id) as conn:
            row = conn.execute("""SELECT b.*,h.job_id,h.state AS handoff_state,h.intent_json
                FROM tg_gateway.progress_bindings b JOIN tg_gateway.source_handoffs h USING(handoff_id)
                WHERE b.binding_id=%s AND b.bot_identity=%s""",(binding_id,self.store.bot_identity)).fetchone()
            if not row or row["closed"] or row["quarantined"]: return
            # Persist fairness/backoff even if an HTTP request below fails.
            conn.execute("UPDATE tg_gateway.progress_bindings SET next_refresh_at=clock_timestamp()+interval '5 seconds' WHERE binding_id=%s",(binding_id,))
            conn.commit()
            if row["pending_delivery_id"]:
                pending = row["pending_delivery_id"]
                receipt = conn.execute("SELECT telegram_message_id FROM tg_gateway.sent_messages WHERE delivery_id=%s",(pending,)).fetchone()
                if receipt:
                    meta = row["desired_payload"]["progressBinding"]
                    conn.execute("""UPDATE tg_gateway.progress_bindings SET message_id=%s,acknowledged_revision=revision,
                        pending_delivery_id=NULL,closed=%s WHERE binding_id=%s""",(receipt["telegram_message_id"],meta["terminal"],binding_id))
                    return
                dispatch = conn.execute("SELECT 1 FROM tg_gateway.delivery_dispatches WHERE delivery_id=%s",(pending,)).fetchone()
                if dispatch:
                    conn.execute("UPDATE tg_gateway.progress_bindings SET quarantined=true WHERE binding_id=%s",(binding_id,))
                    return
                delivery = self.client.get_delivery(delivery_id=str(pending))["delivery"]
                if delivery["status"] not in {"dead", "cancelled"}: return
                if delivery.get("lastErrorClass") == "product_delivery_superseded":
                    conn.execute("""UPDATE tg_gateway.progress_bindings SET pending_delivery_id=NULL,
                        acknowledged_revision=revision WHERE binding_id=%s""", (binding_id,))
                    return
                # A proven refusal must not produce unbounded fresh sends.
                conn.execute("UPDATE tg_gateway.progress_bindings SET closed=true WHERE binding_id=%s",(binding_id,))
                return
            if row["revision"] > row["acknowledged_revision"] and row["desired_payload"]:
                self._append(conn,row,row["desired_payload"])
                return
            terminal = row["handoff_state"] == "terminal_rejected"
            snapshot = self.client.get_progress(job_id=str(row["job_id"])) if row["job_id"] else None
            text = render_status(snapshot) if snapshot else ("Request could not be admitted." if terminal else "Received — waiting for admission.")
            terminal = terminal or bool(snapshot and snapshot["job"]["status"] in TERMINAL)
            previous = row["desired_payload"]
            # Semantic changes update within 5s; clock-only refreshes every 30s.
            semantic = snapshot["version"] if snapshot else 0
            bucket = int(datetime.now(timezone.utc).timestamp() // 30)
            if previous and previous["progressBinding"].get("version") == semantic and previous["progressBinding"].get("bucket") == bucket:
                return
            target = row["target_json"]
            payload = dict(target, text=text,parseMode="HTML",disableWebPagePreview=True,
                deliveryKind="telegram_edit" if row["message_id"] else "telegram_reply_text",
                progressBinding=dict(id=str(binding_id),revision=row["revision"]+1,terminal=terminal,version=semantic,bucket=bucket))
            if row["message_id"]: payload["messageId"] = row["message_id"]
            row["revision"] += 1
            conn.execute("UPDATE tg_gateway.progress_bindings SET revision=%s,desired_payload=%s::jsonb WHERE binding_id=%s",
                         (row["revision"],json.dumps(payload),binding_id))
            conn.commit()  # Intent must survive an ambiguous append response.
            self._append(conn,row,payload)

    def _append(self, conn, row, payload):
        owner = row["intent_json"]["jobSubmitPayload"]["resultOwner"]
        # Intermediate deliveries have no result/job association per outbox contract.
        response = self.client.append_delivery(dict(resultOwner=owner,resultRoute="progress.telegram.v1",
            deliveryProvider="telegram",deliveryKind=payload["deliveryKind"],
            deliveryKey=f"progress:{self.store.bot_identity}:{row['binding_id']}:{row['revision']}",
            priority=20,maxAttempts=5,payloadJson=payload))
        delivery = response["delivery"]
        conn.execute("UPDATE tg_gateway.progress_bindings SET pending_delivery_id=%s WHERE binding_id=%s",
                     (delivery["deliveryId"],row["binding_id"]))
