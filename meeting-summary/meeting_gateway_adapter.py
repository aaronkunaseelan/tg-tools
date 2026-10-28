from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import psycopg
from tg_gateway.admission import AdmissionInput
from tg_gateway.models import InlineButton, OutboundMessage
from tg_tools_common import runtime_contract
from tg_tools_common.delivery_contract import telegram_edit, telegram_reply_document
from tg_tools_common.telegram_routes import RouteResolution, TelegramUpdateShape

from config import Settings, load_settings
from review_flow import (
    MetadataParseError,
    build_accept_callback_data,
    classify_review_reply,
    content_hash,
    parse_accept_callback_data,
    parse_metadata_edit_block,
    preview_filename,
    render_metadata_review_text,
)
from runtime_client import RuntimeClient
from store import (
    accept_review_version,
    append_to_accepted_note,
    create_metadata_review_version,
    create_pending_append_review_version,
    load_review_by_anchor,
    load_review_bundle,
    load_review_by_note,
    record_preview_document_delivery,
    record_review_message_delivery,
    review_dispatch_guard,
)
from telegram_notify import accept_reply_markup_payload


def source_work_dir(settings: Settings, source_key: str) -> Path:
    """Spool directory for one Telegram source, stable per source key."""
    return settings.spool_dir / hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:20]


class GatewayAdapter:
    def __init__(self, *, settings: Any | None = None) -> None:
        self.settings = settings or load_settings()

    async def _reply(self, runtime: Any, message: Any, text: str) -> int:
        return await runtime.gateway.send_text(
            OutboundMessage(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to_message_id=message.message_id,
                text=text,
            )
        )

    async def handle_command(self, runtime: Any, message: Any, *, command: str, arg: str) -> bool:
        if command == "start":
            await self._reply(runtime, message, "Send a meeting audio file. Use /status and /queue to inspect runtime state.")
            return True
        if command == "status":
            counts = {}
            for status in ("pending", "retryable", "leased"):
                payload = await asyncio.to_thread(
                    runtime.runtime_client.list_jobs, result_owner="meeting-summary",
                    workload_type="meeting_summary", status=status, limit=1)
                # Missing producer evidence must not become a healthy zero.
                counts[status] = int(payload["count"])
            await self._reply(runtime, message, "\n".join(
                f"{status}={count}" for status, count in counts.items()))
            return True
        if command in {"queue", "run"}:
            payload = await asyncio.to_thread(
                runtime.runtime_client.list_jobs,
                result_owner="meeting-summary",
                workload_type="meeting_summary",
                status="pending,retryable,leased",
                limit=20,
            )
            items = list(payload.get("items") or [])
            if command == "queue":
                text = "Queue is empty." if not items else "\n".join(
                    f"{row.get('status')} {str(row.get('jobId') or '')[:8]} {row.get('producerRef') or ''}" for row in items
                )
            else:
                text = f"runtime_ok=true\nqueue_visible={len(items)}\nworkers and consumers are long-running services."
            await self._reply(runtime, message, text)
            return True
        return False

    async def handle_message(self, runtime: Any, message: Any, *, shape: TelegramUpdateShape) -> bool:
        if not shape.is_reply or shape.reply_to_message_id is None or not (message.text or "").strip():
            return False
        review = await asyncio.to_thread(
            load_review_by_anchor,
            self.settings.database_url,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            review_message_id=shape.reply_to_message_id,
        )
        if review is None:
            return False

        text = (message.text or "").strip()
        classification = classify_review_reply(text)
        if classification == "metadata":
            if review.status != "pending":
                await self._reply(runtime, message, "Metadata is already accepted. Reply with additional notes to append them.")
                return True
            try:
                edits = parse_metadata_edit_block(text)
            except MetadataParseError as exc:
                await self._reply(runtime, message, f"Metadata edit rejected: {exc}")
                return True
            result = await asyncio.to_thread(
                create_metadata_review_version,
                self.settings.database_url,
                current_review=review,
                edits=edits,
                source_message_id=message.message_id,
                created_by_user_id=message.user_id or 0,
            )
            await self._publish_review_bundle(runtime, result["bundle"])
            if result["status"] == "duplicate":
                await self._reply(runtime, message, "That metadata edit was already applied.")
            else:
                await self._reply(runtime, message, "Metadata updated. Preview refreshed.")
            return True

        if classification == "append":
            if review.status == "pending":
                result = await asyncio.to_thread(
                    create_pending_append_review_version,
                    self.settings.database_url,
                    current_review=review,
                    note_text=text,
                    source_message_id=message.message_id,
                    created_by_user_id=message.user_id or 0,
                )
                await self._publish_review_bundle(runtime, result["bundle"])
                if result["status"] == "duplicate":
                    await self._reply(runtime, message, "That note was already appended.")
                else:
                    await self._reply(runtime, message, "Note appended. Preview refreshed.")
                return True
            if review.status == "accepted":
                result = await asyncio.to_thread(
                    append_to_accepted_note,
                    self.settings.database_url,
                    current_review=review,
                    note_text=text,
                    source_message_id=message.message_id,
                    created_by_user_id=message.user_id or 0,
                )
                # Both new and duplicate appends repair the durable publication
                # intent. Never repeat paid processing or send documents directly.
                await self._send_accepted_append_document(runtime, result)
                if result["status"] == "duplicate":
                    await self._reply(runtime, message, "That note was already appended.")
                else:
                    await self._reply(runtime, message, "Note appended to the accepted Markdown.")
                return True

        await self._reply(runtime, message, "Reply with Date, Title, Attendees, or a note longer than 10 characters.")
        return True

    async def handle_callback(self, runtime: Any, event: Any, callback: Any) -> bool:
        data = (callback.data or "").strip()
        if not data.startswith("ms:"):
            return False
        try:
            parsed = parse_accept_callback_data(data)
        except ValueError:
            return True
        if parsed.action != "a":
            return True
        result = await asyncio.to_thread(
            accept_review_version,
            self.settings.database_url,
            accept_token=parsed.token,
            review_version=parsed.version,
            accepted_by_user_id=callback.user_id or 0,
        )
        status = result.get("status")
        if status == "accepted":
            review = result["review"]
            await self._enqueue_final_document(runtime, review)
            payload = telegram_edit(chat_id=review.chat_id, message_id=review.review_message_id,
                text=render_metadata_review_text(review.metadata_json, accepted=True))
            payload["replyMarkup"] = {"inline_keyboard": []}
            payload["meetingReviewLink"] = {"role": "review_edit", "reviewId": review.review_id,
                "reviewVersion": review.review_version, "uiState": "accepted"}
            await asyncio.to_thread(self._append_review_delivery, runtime.runtime_client, review,
                payload, "accepted")
        elif status == "preview_not_sent":
            await runtime.gateway.send_text(
                OutboundMessage(
                    chat_id=callback.chat_id,
                    thread_id=callback.thread_id,
                    reply_to_message_id=callback.message_id,
                    text="The preview is not ready yet.",
                )
            )
        else:
            await runtime.gateway.send_text(
                OutboundMessage(
                    chat_id=callback.chat_id,
                    thread_id=callback.thread_id,
                    reply_to_message_id=callback.message_id,
                    text="This preview was replaced. Use the latest message.",
                )
            )
        return True

    def prepare_admission(
        self,
        runtime: Any,
        message: Any,
        *,
        shape: TelegramUpdateShape,
        resolution: RouteResolution,
        attachment_refs: tuple[dict[str, Any], ...],
    ) -> AdmissionInput:
        enriched_refs = attachment_refs
        if attachment_refs:
            source_key = f"tg:{shape.bot_identity}:{shape.chat_id}:{shape.message_id}"
            attachment = dict(attachment_refs[0])
            attachment["localWorkDir"] = str(source_work_dir(self.settings, source_key))
            enriched_refs = (attachment, *attachment_refs[1:])
        return AdmissionInput(text=(message.text or "").strip(), attachment_refs=enriched_refs)

    def pre_dispatch(self, payload: dict[str, Any]):
        link = payload.get("meetingReviewLink")
        if not isinstance(link, dict) or link.get("role") not in {"review_message", "preview_document", "review_edit"}:
            return nullcontext(True)
        return review_dispatch_guard(self.settings.database_url, link)

    def after_delivery(self, payload: dict[str, Any], telegram_message_id: int) -> None:
        link = payload.get("meetingReviewLink")
        if not isinstance(link, dict):
            return
        role = str(link.get("role") or "")
        if role == "review_message":
            record_review_message_delivery(
                self.settings.database_url,
                review_id=str(link["reviewId"]),
                review_version=int(link["reviewVersion"]),
                review_message_id=telegram_message_id,
            )
        elif role in {"final_document", "append_document"}:
            self._record_final_delivery(link, telegram_message_id)
        elif role == "preview_document":
            record_preview_document_delivery(
                self.settings.database_url,
                review_id=str(link["reviewId"]),
                review_version=int(link["reviewVersion"]),
                status="sent",
                preview_content_hash=str(link["previewContentHash"]),
                document_message_id=telegram_message_id,
            )
            if link.get("repairAccept"):
                bundle = load_review_bundle(self.settings.database_url, review_id=str(link["reviewId"]), review_status="preview")
                review = bundle.review
                if review.status == "pending" and bundle.markdown_hash == link["previewContentHash"]:
                    payload = telegram_edit(chat_id=review.chat_id, message_id=review.review_message_id,
                        text=render_metadata_review_text(review.metadata_json))
                    payload["replyMarkup"] = accept_reply_markup_payload(build_accept_callback_data(review.accept_token, review.review_version))
                    self._append_review_delivery(RuntimeClient(self.settings), review, payload,
                        f"accept:{bundle.markdown_hash}")

    def _write_review_markdown(self, bundle: Any, *, final: bool = False) -> Path:
        directory = self.settings.state_dir / "review-previews" / bundle.review.meeting_note_id
        directory.mkdir(parents=True, exist_ok=True)
        name = preview_filename(bundle.review.metadata_json, bundle.review.review_version, final=final)
        path = directory / name
        path.write_text(bundle.markdown, encoding="utf-8")
        return path

    @staticmethod
    def _accept_buttons(bundle: Any) -> tuple[tuple[InlineButton, ...], ...]:
        data = build_accept_callback_data(bundle.review.accept_token, bundle.review.review_version)
        return ((InlineButton("Accept", data),),)

    async def _publish_review_bundle(self, runtime: Any, bundle: Any) -> None:
        review = bundle.review
        if review.status != "pending" or review.review_version != review.meeting_note_review_version:
            current = await asyncio.to_thread(load_review_by_note, self.settings.database_url, review.meeting_note_id)
            if current is None or current.status != "pending":
                return
            bundle = await asyncio.to_thread(load_review_bundle, self.settings.database_url,
                review_id=current.review_id, review_status="preview")
            review = bundle.review
        review_text = render_metadata_review_text(review.metadata_json)
        review_message_id = review.review_message_id
        if review_message_id is None:
            # A missing anchor still has the inherited uncertain-send window,
            # but a concurrent correction/accept cannot race this UI operation.
            with review_dispatch_guard(self.settings.database_url, {
                "reviewId": review.review_id, "reviewVersion": review.review_version,
                "role": "review_message",
            }) as current:
                if not current:
                    return
                review_message_id = await runtime.gateway.send_text(
                    OutboundMessage(chat_id=review.chat_id, thread_id=review.thread_id, text=review_text)
                )
            await asyncio.to_thread(
                record_review_message_delivery,
                self.settings.database_url,
                review_id=review.review_id,
                review_version=review.review_version,
                review_message_id=review_message_id,
            )
        else:
            payload = telegram_edit(chat_id=review.chat_id, message_id=review_message_id, text=review_text)
            payload["replyMarkup"] = {"inline_keyboard": []}
            await asyncio.to_thread(self._append_review_delivery, runtime.runtime_client, review,
                payload, "review")

        payload = telegram_reply_document(chat_id=review.chat_id, thread_id=review.thread_id,
            reply_to_message_id=review_message_id,
            document_path=f"cot://tg_meeting_summary/meeting_notes/{review.meeting_note_id}?preview={review.review_version}",
            file_name=preview_filename(review.metadata_json, review.review_version),
            caption=f"Preview v{review.review_version}. Review metadata before accepting.")
        payload["content"] = bundle.markdown
        payload["meetingReviewLink"] = {"role": "preview_document", "reviewId": review.review_id,
            "reviewVersion": review.review_version, "previewContentHash": bundle.markdown_hash,
            "repairAccept": True}
        await asyncio.to_thread(self._append_review_delivery, runtime.runtime_client, review,
            payload, f"preview:{bundle.markdown_hash}")

    def _append_review_delivery(self, client: Any, review: Any, payload: dict[str, Any], phase: str) -> None:
        if payload.get("deliveryKind") == "telegram_edit" and "meetingReviewLink" not in payload:
            payload["meetingReviewLink"] = {"role": "review_edit", "reviewId": review.review_id,
                "reviewVersion": review.review_version, "uiState": "pending",
                "uiPhase": "accept" if phase.startswith("accept:") else "awaiting_preview"}
            if phase.startswith("accept:"):
                payload["meetingReviewLink"]["previewContentHash"] = phase.removeprefix("accept:")
        with psycopg.connect(self.settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT metadata_json->>'job_id' FROM tg_meeting_summary.meeting_notes WHERE meeting_note_id=%s::uuid",
                            (review.meeting_note_id,))
                row = cur.fetchone()
        if not row or not row[0]:
            raise RuntimeError("original MEET job association unavailable")
        job = client.get_job(job_id=row[0])
        if not job.get("terminalResultId") or job.get("resultOwner") != "meeting-summary":
            raise RuntimeError("original MEET terminal result association unavailable")
        delivery = runtime_contract.build_delivery_payload(result_owner="meeting-summary",
            result_route=job["resultRoute"],
            delivery_key_str=f"meeting-summary:review:{review.meeting_note_id}:v{review.review_version}:{phase}",
            payload_json=payload, max_attempts=8)
        delivery.update(resultId=job["terminalResultId"], jobId=job["jobId"], deliveryKind=payload["deliveryKind"])
        client.append_delivery(delivery)

    def _load_final_projection(self, meeting_note_id: str) -> dict[str, Any]:
        with psycopg.connect(self.settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT metadata_json, materialisation_hash, review_version
                       FROM tg_meeting_summary.meeting_notes
                       WHERE meeting_note_id = %s::uuid
                         AND materialisation_state = 'committed'""",
                    (meeting_note_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("committed meeting document unavailable")
        metadata = dict(row[0] or {})
        markdown = metadata.get("retained_markdown")
        if not isinstance(markdown, str) or not markdown:
            raise RuntimeError("retained final Markdown unavailable; repair required")
        digest = content_hash({"markdown": markdown})
        if digest != row[1] or digest != metadata.get("retained_markdown_hash"):
            raise RuntimeError("retained final Markdown hash mismatch")
        return {"markdown": markdown, "hash": digest, "jobId": metadata["job_id"],
                "reviewVersion": int(row[2]),
                "appendVersion": int(metadata.get("latest_append_version") or 0)}

    async def _enqueue_final_document(self, runtime: Any, review: Any) -> None:
        projection = await asyncio.to_thread(self._load_final_projection, review.meeting_note_id)
        job = await asyncio.to_thread(runtime.runtime_client.get_job, job_id=projection["jobId"])
        result_id = job.get("terminalResultId")
        if not result_id or job.get("resultOwner") != "meeting-summary":
            raise RuntimeError("original MEET terminal result association unavailable")
        version = projection["reviewVersion"]
        append_version = projection["appendVersion"]
        digest = projection["hash"]
        key = f"meeting-summary:final:{review.meeting_note_id}:v{version}:a{append_version}:{digest}"
        # Inline exact retained bytes in the existing outbox: delivery remains
        # reconstructible even if host-local review preview files are pruned.
        payload = telegram_reply_document(
            chat_id=review.chat_id, thread_id=review.thread_id,
            reply_to_message_id=review.review_message_id,
            document_path=f"cot://tg_meeting_summary/meeting_notes/{review.meeting_note_id}",
            file_name=preview_filename(review.metadata_json, version, final=True),
            caption="Accepted meeting notes." if not append_version else "Updated accepted meeting notes.",
        )
        payload["content"] = projection["markdown"]
        payload["contentSha256"] = hashlib.sha256(projection["markdown"].encode("utf-8")).hexdigest()
        payload["meetingReviewLink"] = {
            "role": "append_document" if append_version else "final_document",
            "meetingNoteId": review.meeting_note_id, "reviewVersion": version,
            "appendVersion": append_version, "contentHash": digest, "deliveryKey": key,
        }
        delivery = runtime_contract.build_delivery_payload(
            result_owner="meeting-summary", result_route=job["resultRoute"],
            delivery_key_str=key, payload_json=payload, max_attempts=8,
        )
        delivery.update(resultId=result_id, jobId=job["jobId"], deliveryKind=payload["deliveryKind"])
        await asyncio.to_thread(runtime.runtime_client.append_delivery, delivery)

    def _record_final_delivery(self, link: dict[str, Any], message_id: int) -> None:
        # Replay-safe receipt projection, including the content identity. The
        # gateway calls this for sent AND already_sent after a lost hook ack.
        with psycopg.connect(self.settings.database_url) as conn:
            with conn.cursor() as cur:
                receipt = {"deliveryKey": link["deliveryKey"], "contentHash": link["contentHash"],
                           "telegramMessageId": message_id, "reviewVersion": link["reviewVersion"],
                           "appendVersion": link["appendVersion"]}
                cur.execute(
                    """UPDATE tg_meeting_summary.meeting_notes
                       SET metadata_json = jsonb_set(metadata_json,
                           '{final_delivery_receipts}',
                           COALESCE(metadata_json->'final_delivery_receipts', '{}'::jsonb) || %s::jsonb),
                           updated_at = NOW()
                       WHERE meeting_note_id = %s::uuid""",
                    (json.dumps({link["deliveryKey"]: receipt}), link["meetingNoteId"]),
                )
                if int(link["appendVersion"]):
                    cur.execute(
                        """UPDATE tg_meeting_summary.meeting_note_appends
                           SET preview_document_message_id = %s
                           WHERE meeting_note_id = %s::uuid AND append_version = %s
                             AND markdown_content_hash = %s""",
                        (message_id, link["meetingNoteId"], link["appendVersion"], link["contentHash"]),
                    )
            conn.commit()

    async def _send_accepted_append_document(self, runtime: Any, result: dict[str, Any]) -> None:
        await self._enqueue_final_document(runtime, result["bundle"].review)
