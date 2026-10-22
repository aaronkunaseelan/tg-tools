from __future__ import annotations

import argparse
import json
import logging
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from config import ensure_directories, load_settings, validate_settings
from result_receipts import ReceiptStore
from runtime_client import RuntimeClient
from store import StoredRecording, store_telegram_result
from store import (
    load_review_bundle,
    load_review_by_note,
)
from review_flow import (
    build_accept_callback_data,
    preview_filename,
    render_metadata_review_text,
)
from telegram_notify import accept_reply_markup_payload, reply_anchor_message_id
from tg_tools_common import runtime_contract
from tg_tools_common.delivery_contract import telegram_edit, telegram_reply_document, telegram_reply_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meeting-summary-result-consumer")

DELIVERY_PROVIDER = "telegram"
DELIVERY_RESULT_OWNER = "meeting-summary"


def _receipt_path(settings) -> Path:
    return settings.state_dir / "result_receipts.sqlite3"


def _submitted_at(job: dict[str, Any], input_json: dict[str, Any]) -> str:
    return str(input_json.get("submittedAt") or job.get("createdAt") or datetime.now(UTC).isoformat())


def _load_json_file(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    payload = Path(path)
    if not payload.exists():
        return None
    return json.loads(payload.read_text(encoding="utf-8"))


def _load_json_list_file(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    payload = Path(path)
    if not payload.exists():
        return []
    data = json.loads(payload.read_text(encoding="utf-8"))
    return list(data or [])


def _load_text_file(path: str | None) -> str:
    if not path:
        return ""
    payload = Path(path)
    if not payload.exists():
        return ""
    return payload.read_text(encoding="utf-8").strip()


def _store_success_result(
    settings, job: dict[str, Any], result: dict[str, Any], input_json: dict[str, Any]
) -> tuple[StoredRecording, dict[str, Any] | None]:
    result_json = dict(result.get("resultJson") or {})
    # Initial projection must never convert missing retained output into an
    # empty successful note. Once storage_done is durable, replay uses the DB.
    transcript_path = result_json.get("transcriptPath")
    chunks_path = result_json.get("chunksPath")
    if not transcript_path or not Path(transcript_path).is_file():
        raise ValueError("required transcript artifact unavailable")
    transcript_text = _load_text_file(transcript_path)
    if not transcript_text:
        raise ValueError("required transcript artifact is empty")
    if not chunks_path or not Path(chunks_path).is_file():
        raise ValueError("required chunks artifact unavailable")
    chunks_data = json.loads(Path(chunks_path).read_text(encoding="utf-8"))
    if not isinstance(chunks_data, list) or not chunks_data or not all(isinstance(chunk, dict) for chunk in chunks_data):
        raise ValueError("required chunks artifact invalid")
    transcript_chunks = chunks_data
    summary_payload = _load_json_file(result_json.get("summaryPath"))
    if str(result_json.get("summaryStatus") or "ok") == "ok" and (
        not isinstance(summary_payload, dict) or not summary_payload
    ):
        raise ValueError("required successful summary artifact unavailable or invalid")
    summary_error = _load_json_file(result_json.get("summaryErrorPath"))
    summary_error_text = str(summary_error.get("error") or "").strip() or None if summary_error else None
    processed_at = datetime.now(UTC)

    transcribe_provider = str(result_json.get("transcribeProvider") or "azure")
    transcribe_model = str(result_json.get("transcribeModel") or settings.transcribe_model)

    stored = store_telegram_result(
        settings.database_url,
        job_id=str(job["jobId"]),
        submitted_at=_submitted_at(job, input_json),
        processed_at=processed_at,
        chat_id=int(input_json["chatId"]),
        thread_id=input_json.get("threadId"),
        message_id=int(input_json["messageId"]),
        sender_user_id=int(input_json.get("userId") or 0),
        file_id=str(input_json.get("fileId") or ""),
        file_unique_id=str(input_json.get("fileUniqueId") or ""),
        file_name=str(input_json.get("fileName") or "audio"),
        mime_type=str(input_json.get("mimeType") or "application/octet-stream"),
        file_size_bytes=input_json.get("fileSizeBytes"),
        audio_duration_seconds=float(result_json.get("audioDurationSeconds") or 0),
        transcript_text=transcript_text,
        transcript_chunks=transcript_chunks,
        asr_provider=transcribe_provider,
        asr_model=transcribe_model,
        summary=summary_payload,
        summary_status=str(result_json.get("summaryStatus") or "ok"),
        summary_model_display=str(result_json.get("summaryModel") or f"openai-codex/{settings.summary_model}"),
        summary_error_text=summary_error_text,
    )
    return stored, summary_payload


def _append_telegram_delivery(
    *,
    client: RuntimeClient,
    result: dict[str, Any],
    job: dict[str, Any],
    input_json: dict[str, Any],
    phase: str,
    text: str | None = None,
    parse_mode: str | None = None,
    delivery_kind: str = "telegram_reply_text",
    payload_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result_id = str(result["resultId"])
    base = {
        "chat_id": int(input_json["chatId"]),
        "thread_id": input_json.get("threadId"),
    }
    overrides = dict(payload_overrides or {})
    if delivery_kind == "telegram_reply_text":
        payload = telegram_reply_text(
            **base,
            reply_to_message_id=int(overrides.pop("replyToMessageId", reply_anchor_message_id(input_json))),
            text=text or str(overrides.pop("text", "")),
            parse_mode=parse_mode or overrides.pop("parseMode", None),
        )
    elif delivery_kind == "telegram_reply_document":
        document_path = str(overrides.pop("documentPath"))
        payload = telegram_reply_document(
            **base,
            reply_to_message_id=int(overrides.pop("replyToMessageId", reply_anchor_message_id(input_json))),
            document_path=document_path,
            file_name=str(overrides.pop("fileName", overrides.pop("filename", Path(document_path).name))),
            caption=overrides.pop("caption", None),
        )
    elif delivery_kind == "telegram_edit":
        payload = telegram_edit(
            chat_id=base["chat_id"],
            message_id=int(overrides.pop("messageId")),
            text=str(overrides.pop("text", text or "")),
            parse_mode=parse_mode or overrides.pop("parseMode", None),
        )
        if "replyMarkup" in overrides:
            payload["replyMarkup"] = overrides.pop("replyMarkup")
    else:
        raise ValueError(f"unsupported delivery kind: {delivery_kind}")
    payload["phase"] = phase
    payload.update(overrides)
    delivery = runtime_contract.build_delivery_payload(
        result_owner=DELIVERY_RESULT_OWNER,
        result_route=str(result.get("resultRoute") or job.get("resultRoute") or "meeting-summary.telegram.reply.v1"),
        delivery_key_str=f"meeting-summary:telegram:{result_id}:{phase}",
        payload_json=payload,
        max_attempts=8,
    )
    delivery["resultId"] = result_id
    delivery["jobId"] = str(job["jobId"])
    delivery["deliveryKind"] = str(payload["deliveryKind"])
    return client.append_delivery(delivery)


def _write_review_markdown(settings, bundle, *, final: bool = False) -> Path:
    directory = settings.state_dir / "review-previews" / bundle.review.meeting_note_id
    directory.mkdir(parents=True, exist_ok=True)
    name = preview_filename(bundle.review.metadata_json, bundle.review.review_version, final=final)
    path = directory / name
    path.write_text(bundle.markdown, encoding="utf-8")
    return path


def _notify_telegram_success(
    settings,
    receipt_store: ReceiptStore,
    receipt,
    job: dict[str, Any],
    result: dict[str, Any],
    input_json: dict[str, Any],
    summary_payload: dict[str, Any] | None,
) -> None:
    if receipt.notified_done:
        return
    stored = None
    if receipt.stored_schema and receipt.stored_table and receipt.stored_recording_id and receipt.stored_source_message_key:
        meeting_note_id = receipt.stored_recording_id if receipt.stored_table == "meeting_notes" else None
        stored = StoredRecording(
            schema=receipt.stored_schema,
            table=receipt.stored_table,
            recording_id=receipt.stored_recording_id,
            source_message_key=receipt.stored_source_message_key,
            meeting_note_id=meeting_note_id,
        )
    if stored is None:
        raise RuntimeError("stored recording metadata missing from receipt")

    review = (
        load_review_by_note(settings.database_url, stored.meeting_note_id)
        if stored.meeting_note_id else None
    )
    if not summary_payload and review is None:
        text = "Transcript complete and stored, but summary generation failed."
        _append_telegram_delivery(
            client=RuntimeClient(settings),
            result=result,
            job=job,
            input_json=input_json,
            phase="summary-failed",
            text=text,
        )
        receipt_store.record_telegram_phase(
            receipt.result_id,
            phase="done",
            summary_message_id=receipt.telegram_summary_message_id,
            notified_done=True,
        )
        return

    if not stored.meeting_note_id:
        raise RuntimeError("stored meeting note id missing from receipt")

    if review is None:
        raise RuntimeError(f"pending review missing for meeting_note_id={stored.meeting_note_id}")

    client = RuntimeClient(settings)
    review_text = render_metadata_review_text(review.metadata_json)
    if review.review_message_id is None:
        _append_telegram_delivery(
            client=client,
            result=result,
            job=job,
            input_json=input_json,
            phase=f"review-v{review.review_version}",
            text=review_text,
            payload_overrides={
                "meetingReviewLink": {
                    "role": "review_message",
                    "reviewId": review.review_id,
                    "reviewVersion": review.review_version,
                }
            },
        )
        receipt_store.record_telegram_phase(receipt.result_id, phase="review-message-pending")
        return

    bundle = load_review_bundle(settings.database_url, review_id=review.review_id, review_status="preview")
    preview_path = _write_review_markdown(settings, bundle)
    if review.preview_document_status != "sent" or review.preview_content_hash != bundle.markdown_hash:
        _append_telegram_delivery(
            client=client,
            result=result,
            job=job,
            input_json=input_json,
            phase=f"preview-v{review.review_version}",
            delivery_kind="telegram_reply_document",
            payload_overrides={
                "replyToMessageId": review.review_message_id or reply_anchor_message_id(input_json),
                "documentPath": str(preview_path),
                "filename": preview_path.name,
                "caption": f"Preview v{review.review_version}. Review metadata in the chat before accepting.",
                "meetingReviewLink": {
                    "role": "preview_document",
                    "reviewId": review.review_id,
                    "reviewVersion": review.review_version,
                    "previewContentHash": bundle.markdown_hash,
                },
            },
        )
        receipt_store.record_telegram_phase(receipt.result_id, phase="preview-document-pending")
        return

    if review.review_message_id:
        callback_data = build_accept_callback_data(review.accept_token, review.review_version)
        _append_telegram_delivery(
            client=client,
            result=result,
            job=job,
            input_json=input_json,
            phase=f"accept-v{review.review_version}",
            delivery_kind="telegram_edit",
            payload_overrides={
                "messageId": review.review_message_id,
                "text": review_text,
                "replyMarkup": accept_reply_markup_payload(callback_data),
                "meetingReviewLink": {
                    "role": "review_edit", "reviewId": review.review_id,
                    "reviewVersion": review.review_version, "uiState": "pending",
                    "uiPhase": "accept", "previewContentHash": bundle.markdown_hash,
                },
            },
        )
    receipt_store.record_telegram_phase(
        receipt.result_id,
        phase="done",
        summary_message_id=review.review_message_id or receipt.telegram_summary_message_id,
        notified_done=True,
    )


def _notify_telegram_error(settings, receipt_store: ReceiptStore, receipt, job: dict[str, Any], result: dict[str, Any], input_json: dict[str, Any]) -> None:
    if receipt.notified_done:
        return
    error_text = str(result.get("errorText") or job.get("lastErrorText") or result.get("errorClass") or job.get("lastErrorClass") or "Processing failed.")
    client = RuntimeClient(settings)
    _append_telegram_delivery(
        client=client,
        result=result,
        job=job,
        input_json=input_json,
        phase="error",
        text=f"Processing failed: {error_text}",
    )
    receipt_store.record_telegram_phase(
        receipt.result_id,
        phase="done",
        summary_message_id=receipt.telegram_summary_message_id,
        notified_done=True,
    )


def _prune_old_spool(settings) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=settings.retention_days)
    if not settings.spool_dir.exists():
        return
    for child in settings.spool_dir.iterdir():
        if not child.is_dir():
            continue
        modified = datetime.fromtimestamp(child.stat().st_mtime, tz=UTC)
        if modified >= cutoff:
            continue
        shutil.rmtree(child, ignore_errors=True)


def process_results(*, once: bool = False) -> int:
    settings = load_settings()
    ensure_directories(settings)
    validate_settings(settings)
    client = RuntimeClient(settings)
    receipt_store = ReceiptStore(_receipt_path(settings))
    receipt_store.init_db()
    processed = 0
    prune_counter = 0

    while True:
        try:
            results = client.list_results(result_owner="meeting-summary", limit=100)
        except Exception as exc:
            log.warning("result poll failed: %s", exc)
            if once:
                raise
            time.sleep(settings.runtime_result_poll_interval_sec)
            continue
        if not results:
            if once:
                return processed
            time.sleep(settings.runtime_result_poll_interval_sec)
            continue

        consumed_in_batch = 0
        for result in results:
            result_id = str(result["resultId"])
            job: dict[str, Any] = {}
            receipt = None
            try:
                job = client.get_job(job_id=str(result["jobId"]))
                input_json = dict(job.get("inputJson") or {})
                source_kind = str(input_json.get("sourceKind") or "") or None
                result_kind = str(result.get("resultKind") or "unknown")
                receipt = receipt_store.ensure_receipt(
                    result_id=result_id,
                    job_id=str(job["jobId"]),
                    source_kind=source_kind,
                    source_key=str(input_json.get("sourceKey") or "") or None,
                    result_kind=result_kind,
                )
                summary_payload = None
                if not receipt.storage_done:
                    stored = None
                    if result_kind == "succeeded":
                        stored, summary_payload = _store_success_result(settings, job, result, input_json)
                        receipt_store.record_storage(result_id, stored)
                        receipt = receipt_store.get(result_id)
                    else:
                        receipt_store.record_storage(result_id, stored)
                        receipt = receipt_store.get(result_id)

                if source_kind == "telegram":
                    if result_kind == "succeeded":
                        _notify_telegram_success(
                            settings, receipt_store, receipt, job, result, input_json, summary_payload
                        )
                    else:
                        _notify_telegram_error(settings, receipt_store, receipt, job, result, input_json)
                    receipt = receipt_store.get(result_id)
                elif not receipt.notified_done:
                    receipt_store.mark_not_applicable(result_id)
                    receipt = receipt_store.get(result_id)

                if not receipt.consumed_done and receipt.storage_done and receipt.notified_done:
                    client.consume_result(result_id=result_id, result_owner="meeting-summary")
                    receipt_store.mark_consumed(result_id)
                    processed += 1
                    consumed_in_batch += 1
            except Exception as exc:
                job_id = str(job.get("jobId") or result.get("jobId") or "")
                log.warning("result processing failed result_id=%s job_id=%s error=%s", result_id, job_id, exc, exc_info=True)
                if receipt is None:
                    try:
                        receipt = receipt_store.ensure_receipt(
                            result_id=result_id,
                            job_id=job_id,
                            source_kind=None,
                            source_key=None,
                            result_kind=str(result.get("resultKind") or "unknown"),
                        )
                    except Exception:
                        receipt = None
                if receipt is not None:
                    receipt_store.set_last_error(result_id, str(exc))

        prune_counter += 1
        if prune_counter >= 10:
            _prune_old_spool(settings)
            prune_counter = 0

        if once:
            return processed
        if consumed_in_batch == 0:
            time.sleep(settings.runtime_result_poll_interval_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="meeting-summary runtime result consumer")
    parser.add_argument("--once", action="store_true", help="process the current ready-result set once")
    parser.add_argument("--json", action="store_true", help="emit structured output")
    args = parser.parse_args()

    started = time.time()
    processed = process_results(once=args.once)
    if args.json:
        print(json.dumps({"ok": True, "processed": processed, "elapsed_seconds": round(time.time() - started, 3)}))


if __name__ == "__main__":
    main()
