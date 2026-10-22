from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from review_flow import (
    apply_metadata_edit,
    content_hash,
    date_column_value,
    initial_review_metadata,
    normalize_metadata,
    render_meeting_markdown,
)

RECORDING_NAMESPACE = uuid.UUID("1f8a6b52-30c4-4b40-b39f-93d9cd4f6f61")
log = logging.getLogger("meeting-summary-store")


@dataclass(frozen=True)
class StoredRecording:
    schema: str
    table: str
    source_message_key: str
    recording_id: str
    meeting_input_id: str | None = None
    meeting_note_id: str | None = None
    document_url: str | None = None
    review_id: str | None = None
    review_version: int | None = None


@dataclass(frozen=True)
class ReviewRecord:
    review_id: str
    meeting_note_id: str
    review_version: int
    status: str
    metadata_json: dict[str, Any]
    chat_id: int
    thread_id: int | None
    review_message_id: int | None
    latest_preview_document_message_id: int | None
    latest_preview_document_file_id: str | None
    preview_document_status: str
    preview_content_hash: str | None
    accept_token: str
    meeting_note_review_status: str
    meeting_note_review_version: int
    notes_json: dict[str, Any]


@dataclass(frozen=True)
class ReviewBundle:
    review: ReviewRecord
    appends: list[dict[str, Any]]
    markdown: str
    markdown_hash: str


def telegram_source_message_key(*, chat_id: int, message_id: int) -> str:
    return f"tg:{chat_id}:{message_id}"


def recording_id_for_source_message_key(source_message_key: str) -> uuid.UUID:
    return uuid.uuid5(RECORDING_NAMESPACE, source_message_key)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def _new_accept_token() -> str:
    return secrets.token_urlsafe(9).rstrip("=")


def _merge_note_metadata(notes_json: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    merged = dict(notes_json or {})
    normalized = normalize_metadata(metadata)
    merged["meeting_date"] = normalized["meeting_date"]
    merged["title"] = normalized["title"]
    merged["attendees"] = list(normalized["attendees"])
    return merged


def _review_row_to_record(row: Any) -> ReviewRecord:
    return ReviewRecord(
        review_id=str(row[0]),
        meeting_note_id=str(row[1]),
        review_version=int(row[2]),
        status=str(row[3]),
        metadata_json=dict(row[4] or {}),
        chat_id=int(row[5]),
        thread_id=row[6],
        review_message_id=row[7],
        latest_preview_document_message_id=row[8],
        latest_preview_document_file_id=row[9],
        preview_document_status=str(row[10]),
        preview_content_hash=row[11],
        accept_token=str(row[12]),
        meeting_note_review_status=str(row[13]),
        meeting_note_review_version=int(row[14] or 0),
        notes_json=dict(row[15] or {}),
    )


def _select_review_sql(where_clause: str) -> str:
    return f"""
        SELECT
            r.review_id,
            r.meeting_note_id,
            r.review_version,
            r.status,
            r.metadata_json,
            r.chat_id,
            r.thread_id,
            r.review_message_id,
            r.latest_preview_document_message_id,
            r.latest_preview_document_file_id,
            r.preview_document_status,
            r.preview_content_hash,
            r.accept_token,
            mn.review_status,
            mn.review_version,
            mn.notes_json
        FROM tg_meeting_summary.meeting_note_reviews r
        JOIN tg_meeting_summary.meeting_notes mn ON mn.meeting_note_id = r.meeting_note_id
        {where_clause}
    """


@contextmanager
def review_dispatch_guard(database_url: str, link: dict[str, Any]):
    """Hold the existing mutation locks through the provider effect, not hooks.

    Missing state is an error, never permission to dispatch. A remote provider
    timeout can still apply later; this is serialization, not provider fencing.
    """
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(_select_review_sql("WHERE r.review_id = %s::uuid FOR UPDATE OF r, mn"),
                        (str(link["reviewId"]),))
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("MEET dispatch review state unavailable")
            review = _review_row_to_record(row)
            expected = "accepted" if link.get("uiState") == "accepted" else "pending"
            current = (review.review_version == int(link["reviewVersion"])
                       and review.review_version == review.meeting_note_review_version
                       and review.status == expected
                       and review.meeting_note_review_status == expected)
            if link.get("uiPhase") == "awaiting_preview":
                current = current and review.preview_document_status != "sent"
            if link.get("previewContentHash") and link.get("role") == "review_edit":
                current = (current and review.preview_document_status == "sent"
                           and review.preview_content_hash == link["previewContentHash"])
            yield current


def _load_review_appends(cur, meeting_note_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            append_version,
            note_text,
            chat_id,
            thread_id,
            source_message_id,
            created_by_user_id,
            markdown_content_hash,
            document_url,
            preview_document_message_id,
            preview_document_file_id,
            created_at
        FROM tg_meeting_summary.meeting_note_appends
        WHERE meeting_note_id = %s::uuid
        ORDER BY append_version
        """,
        (meeting_note_id,),
    )
    rows = cur.fetchall()
    return [
        {
            "append_version": int(row[0]),
            "note_text": row[1],
            "chat_id": row[2],
            "thread_id": row[3],
            "source_message_id": row[4],
            "created_by_user_id": row[5],
            "markdown_content_hash": row[6],
            "document_url": row[7],
            "preview_document_message_id": row[8],
            "preview_document_file_id": row[9],
            "created_at": row[10].astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if row[10] else "",
        }
        for row in rows
    ]


def _build_review_bundle(cur, review: ReviewRecord, *, review_status: str) -> ReviewBundle:
    appends = _load_review_appends(cur, review.meeting_note_id)
    markdown = render_meeting_markdown(
        metadata=review.metadata_json,
        notes_json=review.notes_json,
        appends=appends,
        review_status=review_status,
        review_version=review.review_version,
    )
    markdown_hash = content_hash({"markdown": markdown})
    return ReviewBundle(review=review, appends=appends, markdown=markdown, markdown_hash=markdown_hash)


def _parse_timestamp(raw: str) -> datetime:
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _summary_json(summary: dict[str, Any] | None) -> dict[str, Any]:
    return summary if isinstance(summary, dict) else {}


def _summary_title(summary: dict[str, Any] | None) -> str | None:
    value = str((summary or {}).get("title") or "").strip()
    return value or None


def _summary_overview(summary: dict[str, Any] | None) -> str | None:
    value = str((summary or {}).get("overview") or "").strip()
    return value or None


def _chat_summary_text(summary: dict[str, Any] | None) -> str | None:
    summary_json = _summary_json(summary)
    title = _summary_title(summary_json)
    overview = _summary_overview(summary_json)
    action_items = summary_json.get("action_items")
    lines: list[str] = []
    if title:
        lines.append(title)
    if overview:
        lines.append(overview)
    if isinstance(action_items, list) and action_items:
        lines.append("Action items:")
        for item in action_items[:8]:
            if isinstance(item, dict):
                description = str(item.get("description") or "").strip()
            else:
                description = str(item or "").strip()
            if description:
                lines.append(f"- {description}")
    return "\n".join(lines).strip() or None


def _telegram_transport_thread_ref(*, chat_id: int, thread_id: int | None) -> str:
    if thread_id is not None:
        return f"tg:{chat_id}:thread:{thread_id}"
    return f"tg:{chat_id}"


def _first_chunk_language(transcript_chunks: list[dict[str, Any]]) -> str | None:
    for chunk in transcript_chunks:
        value = str(chunk.get("language") or chunk.get("language_code") or "").strip()
        if value:
            return value
    return None


def _upsert_telegram_meeting_note(
    cur,
    *,
    recording_id: uuid.UUID | None,
    source_message_key: str,
    job_id: str,
    submitted_at: datetime,
    chat_id: int,
    thread_id: int | None,
    message_id: int,
    sender_user_id: int,
    file_id: str,
    file_unique_id: str,
    file_name: str,
    mime_type: str,
    file_size_bytes: int | None,
    audio_duration_seconds: float,
    transcript_text: str,
    transcript_chunks: list[dict[str, Any]],
    asr_provider: str,
    asr_model: str,
    summary: dict[str, Any] | None,
    summary_status: str,
    summary_model_display: str,
    summary_error_text: str | None,
    source_content_hash: str,
) -> tuple[str, str, str | None, str | None, int | None]:
    summary_json = _summary_json(summary)
    review_metadata, meeting_date_source = initial_review_metadata(
        summary_json,
        fallback_date=submitted_at.astimezone(UTC),
    )
    note_notes_json = _merge_note_metadata(summary_json, review_metadata)
    input_content_hash = _content_hash(
        {
            "source_message_key": source_message_key,
            "file_unique_id": file_unique_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "file_size_bytes": file_size_bytes,
            "audio_duration_seconds": audio_duration_seconds,
        }
    )
    transcript_content_hash = _content_hash(
        {
            "text": transcript_text,
            "chunks": transcript_chunks,
            "provider": asr_provider,
            "model": asr_model,
        }
    )
    notes_content_hash = _content_hash(
        {
            "summary_json": note_notes_json,
            "summary_status": summary_status,
            "summary_model": summary_model_display,
            "error_text": summary_error_text,
        }
    )
    external_thread_id = _telegram_transport_thread_ref(chat_id=chat_id, thread_id=thread_id)
    raw_payload_json = {
        "source_kind": "telegram",
        "source_message_key": source_message_key,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "message_id": message_id,
        "sender_user_id": sender_user_id,
        "file": {
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "file_name": file_name or None,
            "mime_type": mime_type or None,
            "file_size_bytes": file_size_bytes,
            "audio_duration_seconds": audio_duration_seconds,
        },
    }
    input_metadata_json = {
        "job_id": job_id,
        "route_kind": "meeting_note_route",
        "route_id": "meeting.note.telegram.v1",
    }
    if recording_id is not None:
        input_metadata_json["recording_id"] = str(recording_id)
    cur.execute(
        """
        INSERT INTO tg_meeting_summary.meeting_inputs (
            source_channel,
            external_thread_id,
            external_message_id,
            external_sender_ref,
            input_kind,
            input_content_hash,
            received_at,
            raw_payload_json,
            metadata_json,
            status,
            updated_at
        ) VALUES (
            'telegram',
            %s,
            %s,
            %s,
            'audio',
            %s,
            %s,
            %s::jsonb,
            %s::jsonb,
            'completed',
            NOW()
        )
        ON CONFLICT (source_channel, external_message_id) DO UPDATE SET
            external_thread_id = EXCLUDED.external_thread_id,
            external_sender_ref = EXCLUDED.external_sender_ref,
            input_kind = EXCLUDED.input_kind,
            input_content_hash = EXCLUDED.input_content_hash,
            received_at = EXCLUDED.received_at,
            raw_payload_json = EXCLUDED.raw_payload_json,
            metadata_json = EXCLUDED.metadata_json,
            status = EXCLUDED.status,
            updated_at = NOW()
        RETURNING input_id
        """,
        (
            external_thread_id,
            source_message_key,
            str(sender_user_id),
            input_content_hash,
            submitted_at.astimezone(UTC),
            _json_dumps(raw_payload_json),
            _json_dumps(input_metadata_json),
        ),
    )
    input_id = str(cur.fetchone()[0])

    # The input upsert holds the source-row lock until commit. A terminal
    # result replay must not reset corrections or erase accepted output.
    cur.execute(
        """
        SELECT meeting_note_id, document_url
        FROM tg_meeting_summary.meeting_notes
        WHERE input_id = %s::uuid
        """,
        (input_id,),
    )
    existing = cur.fetchone()
    if existing:
        return input_id, str(existing[0]), existing[1], None, None

    note_metadata_json = {
        "job_id": job_id,
        "source_message_key": source_message_key,
        "transcript_content_hash": transcript_content_hash,
        "transcript_char_count": len(transcript_text),
        "transcript_chunk_count": len(transcript_chunks),
        "asr_provider": asr_provider,
        "asr_model": asr_model,
        "summary_error_text": summary_error_text,
        "meeting_date_source": meeting_date_source,
    }
    if recording_id is not None:
        note_metadata_json["recording_id"] = str(recording_id)
    note_status = "synthesized" if summary_status == "ok" and summary_json else "summary_failed"
    materialisation_state = "pending" if note_status == "synthesized" else "not_requested"
    review_status = "pending" if note_status == "synthesized" else "not_required"
    review_version = 1 if note_status == "synthesized" else 0
    cur.execute(
        """
        INSERT INTO tg_meeting_summary.meeting_notes (
            input_id,
            recording_id,
            transcript_ref,
            title,
            overview,
            notes_json,
            chat_summary_text,
            notes_content_hash,
            source_content_hash,
            synthesis_model,
            prompt_version,
            notes_schema_version,
            review_status,
            review_version,
            meeting_date,
            materialisation_state,
            status,
            metadata_json,
            updated_at
        ) VALUES (
            %s::uuid,
            %s::uuid,
            %s,
            %s,
            %s,
            %s::jsonb,
            %s,
            %s,
            %s,
            %s,
            'meeting-summary-notes-v1',
            1,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s::jsonb,
            NOW()
        )
        ON CONFLICT (input_id) DO UPDATE SET
            recording_id = EXCLUDED.recording_id,
            transcript_ref = EXCLUDED.transcript_ref,
            title = EXCLUDED.title,
            overview = EXCLUDED.overview,
            notes_json = EXCLUDED.notes_json,
            chat_summary_text = EXCLUDED.chat_summary_text,
            notes_content_hash = EXCLUDED.notes_content_hash,
            source_content_hash = EXCLUDED.source_content_hash,
            synthesis_model = EXCLUDED.synthesis_model,
            prompt_version = EXCLUDED.prompt_version,
            notes_schema_version = EXCLUDED.notes_schema_version,
            review_status = CASE
                WHEN tg_meeting_summary.meeting_notes.materialisation_state = 'committed'
                THEN tg_meeting_summary.meeting_notes.review_status
                ELSE EXCLUDED.review_status
            END,
            review_version = CASE
                WHEN tg_meeting_summary.meeting_notes.materialisation_state = 'committed'
                THEN tg_meeting_summary.meeting_notes.review_version
                ELSE EXCLUDED.review_version
            END,
            meeting_date = CASE
                WHEN tg_meeting_summary.meeting_notes.materialisation_state = 'committed'
                THEN tg_meeting_summary.meeting_notes.meeting_date
                ELSE EXCLUDED.meeting_date
            END,
            materialisation_state = CASE
                WHEN tg_meeting_summary.meeting_notes.materialisation_state = 'committed'
                THEN tg_meeting_summary.meeting_notes.materialisation_state
                ELSE EXCLUDED.materialisation_state
            END,
            status = CASE
                WHEN tg_meeting_summary.meeting_notes.materialisation_state = 'committed'
                THEN tg_meeting_summary.meeting_notes.status
                ELSE EXCLUDED.status
            END,
            metadata_json = EXCLUDED.metadata_json,
            updated_at = NOW()
        RETURNING meeting_note_id
        """,
        (
            input_id,
            recording_id,
            f"sha256:{transcript_content_hash}",
            review_metadata["title"],
            _summary_overview(note_notes_json),
            _json_dumps(note_notes_json),
            _chat_summary_text(note_notes_json),
            notes_content_hash,
            source_content_hash,
            summary_model_display,
            review_status,
            review_version,
            date_column_value(review_metadata["meeting_date"]),
            materialisation_state,
            note_status,
            _json_dumps(note_metadata_json),
        ),
    )
    meeting_note_id = str(cur.fetchone()[0])
    document_url: str | None = None
    review_id: str | None = None
    created_review_version: int | None = None
    if note_status == "synthesized":
        cur.execute(
            """
            INSERT INTO tg_meeting_summary.meeting_note_reviews (
                meeting_note_id,
                review_version,
                status,
                metadata_json,
                chat_id,
                thread_id,
                preview_document_status,
                accept_token,
                created_by_user_id,
                updated_at
            ) VALUES (
                %s::uuid,
                1,
                'pending',
                %s::jsonb,
                %s,
                %s,
                'pending',
                %s,
                %s,
                NOW()
            )
            ON CONFLICT (meeting_note_id, review_version) DO UPDATE SET
                metadata_json = CASE
                    WHEN tg_meeting_summary.meeting_note_reviews.status = 'pending'
                    THEN EXCLUDED.metadata_json
                    ELSE tg_meeting_summary.meeting_note_reviews.metadata_json
                END,
                chat_id = EXCLUDED.chat_id,
                thread_id = EXCLUDED.thread_id,
                updated_at = NOW()
            RETURNING review_id, review_version
            """,
            (
                meeting_note_id,
                _json_dumps(review_metadata),
                chat_id,
                thread_id,
                _new_accept_token(),
                sender_user_id,
            ),
        )
        review_row = cur.fetchone()
        review_id = str(review_row[0])
        created_review_version = int(review_row[1])
    return input_id, meeting_note_id, document_url, review_id, created_review_version


def store_telegram_result(
    database_url: str,
    *,
    job_id: str,
    submitted_at: str,
    processed_at: datetime,
    chat_id: int,
    thread_id: int | None,
    message_id: int,
    sender_user_id: int,
    file_id: str,
    file_unique_id: str,
    file_name: str,
    mime_type: str,
    file_size_bytes: int | None,
    audio_duration_seconds: float,
    transcript_text: str,
    transcript_chunks: list[dict[str, Any]],
    asr_provider: str,
    asr_model: str,
    summary: dict[str, Any] | None,
    summary_status: str,
    summary_model_display: str,
    summary_error_text: str | None,
) -> StoredRecording:
    source_message_key = telegram_source_message_key(chat_id=chat_id, message_id=message_id)
    submitted_dt = _parse_timestamp(submitted_at)
    summary_json = _summary_json(summary)
    source_content_hash = _content_hash(
        {
            "source_message_key": source_message_key,
            "file": {
                "file_id": file_id,
                "file_unique_id": file_unique_id,
                "file_name": file_name,
                "mime_type": mime_type,
                "file_size_bytes": file_size_bytes,
                "audio_duration_seconds": audio_duration_seconds,
            },
            "transcript": {
                "text": transcript_text,
                "chunks": transcript_chunks,
                "provider": asr_provider,
                "model": asr_model,
            },
            "summary": {
                "summary_json": summary_json,
                "summary_status": summary_status,
                "summary_model": summary_model_display,
                "error_text": summary_error_text,
            },
        }
    )
    meeting_input_id: str | None = None
    meeting_note_id: str | None = None
    review_id: str | None = None
    review_version: int | None = None

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            meeting_input_id, meeting_note_id, document_url, review_id, review_version = _upsert_telegram_meeting_note(
                cur,
                recording_id=None,
                source_message_key=source_message_key,
                job_id=job_id,
                submitted_at=submitted_dt,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                sender_user_id=sender_user_id,
                file_id=file_id,
                file_unique_id=file_unique_id,
                file_name=file_name,
                mime_type=mime_type,
                file_size_bytes=file_size_bytes,
                audio_duration_seconds=audio_duration_seconds,
                transcript_text=transcript_text,
                transcript_chunks=transcript_chunks,
                asr_provider=asr_provider,
                asr_model=asr_model,
                summary=summary,
                summary_status=summary_status,
                summary_model_display=summary_model_display,
                summary_error_text=summary_error_text,
                source_content_hash=source_content_hash,
            )
        conn.commit()

    return StoredRecording(
        schema="tg_meeting_summary",
        table="meeting_notes",
        source_message_key=source_message_key,
        recording_id=meeting_note_id or "",
        meeting_input_id=meeting_input_id,
        meeting_note_id=meeting_note_id,
        document_url=document_url,
        review_id=review_id,
        review_version=review_version,
    )


def load_review_by_note(database_url: str, meeting_note_id: str) -> ReviewRecord | None:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                _select_review_sql(
                    """
                    WHERE r.meeting_note_id = %s::uuid
                      AND r.status IN ('pending', 'accepted')
                    ORDER BY r.review_version DESC
                    LIMIT 1
                    """
                ),
                (meeting_note_id,),
            )
            row = cur.fetchone()
            return _review_row_to_record(row) if row else None


def load_review_by_anchor(
    database_url: str,
    *,
    chat_id: int,
    thread_id: int | None,
    review_message_id: int,
) -> ReviewRecord | None:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                _select_review_sql(
                    """
                    WHERE r.meeting_note_id = (
                        SELECT anchor.meeting_note_id
                        FROM tg_meeting_summary.meeting_note_reviews anchor
                        WHERE anchor.chat_id = %s
                          AND anchor.thread_id IS NOT DISTINCT FROM %s
                          AND (
                              anchor.review_message_id = %s
                              OR anchor.latest_preview_document_message_id = %s
                          )
                        ORDER BY anchor.review_version DESC
                        LIMIT 1
                    )
                      AND r.status IN ('pending', 'accepted')
                    ORDER BY r.review_version DESC
                    LIMIT 1
                    """
                ),
                (chat_id, thread_id, review_message_id, review_message_id),
            )
            row = cur.fetchone()
            return _review_row_to_record(row) if row else None


def load_review_bundle(
    database_url: str,
    *,
    review_id: str,
    review_status: str = "preview",
) -> ReviewBundle:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                _select_review_sql("WHERE r.review_id = %s::uuid"),
                (review_id,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"review not found: {review_id}")
            return _build_review_bundle(cur, _review_row_to_record(row), review_status=review_status)


def record_review_message_delivery(
    database_url: str,
    *,
    review_id: str,
    review_version: int,
    review_message_id: int,
) -> None:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tg_meeting_summary.meeting_note_reviews
                SET review_message_id = %s,
                    updated_at = NOW()
                WHERE review_id = %s::uuid
                  AND review_version = %s
                """,
                (review_message_id, review_id, review_version),
            )
        conn.commit()


def record_preview_document_delivery(
    database_url: str,
    *,
    review_id: str,
    review_version: int,
    status: str,
    preview_content_hash: str,
    document_message_id: int | None = None,
    document_file_id: str | None = None,
) -> None:
    if status not in {"sent", "failed"}:
        raise ValueError("preview delivery status must be sent or failed")
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tg_meeting_summary.meeting_note_reviews
                SET latest_preview_document_message_id = %s,
                    latest_preview_document_file_id = %s,
                    preview_document_status = %s,
                    preview_content_hash = %s,
                    updated_at = NOW()
                WHERE review_id = %s::uuid
                  AND review_version = %s
                """,
                (
                    document_message_id,
                    document_file_id,
                    status,
                    preview_content_hash,
                    review_id,
                    review_version,
                ),
            )
        conn.commit()


def _insert_next_review_version(
    cur,
    *,
    prior: ReviewRecord,
    metadata: dict[str, Any],
    created_by_user_id: int,
) -> ReviewRecord:
    next_version = prior.review_version + 1
    notes_json = _merge_note_metadata(prior.notes_json, metadata)
    notes_content_hash = content_hash({"notes_json": notes_json, "review_version": next_version})
    cur.execute(
        """
        UPDATE tg_meeting_summary.meeting_note_reviews
        SET status = 'superseded',
            updated_at = NOW()
        WHERE meeting_note_id = %s::uuid
          AND status = 'pending'
        """,
        (prior.meeting_note_id,),
    )
    cur.execute(
        """
        INSERT INTO tg_meeting_summary.meeting_note_reviews (
            meeting_note_id,
            review_version,
            status,
            metadata_json,
            chat_id,
            thread_id,
            review_message_id,
            preview_document_status,
            accept_token,
            created_by_user_id,
            updated_at
        ) VALUES (
            %s::uuid,
            %s,
            'pending',
            %s::jsonb,
            %s,
            %s,
            %s,
            'pending',
            %s,
            %s,
            NOW()
        )
        RETURNING review_id
        """,
        (
            prior.meeting_note_id,
            next_version,
            _json_dumps(metadata),
            prior.chat_id,
            prior.thread_id,
            prior.review_message_id,
            _new_accept_token(),
            created_by_user_id,
        ),
    )
    next_review_id = str(cur.fetchone()[0])
    cur.execute(
        """
        UPDATE tg_meeting_summary.meeting_notes
        SET title = %s,
            meeting_date = %s,
            notes_json = %s::jsonb,
            chat_summary_text = %s,
            notes_content_hash = %s,
            review_status = 'pending',
            review_version = %s,
            materialisation_state = 'pending',
            status = 'synthesized',
            document_url = NULL,
            updated_at = NOW()
        WHERE meeting_note_id = %s::uuid
        """,
        (
            metadata["title"],
            date_column_value(metadata["meeting_date"]),
            _json_dumps(notes_json),
            _chat_summary_text(notes_json),
            notes_content_hash,
            next_version,
            prior.meeting_note_id,
        ),
    )
    cur.execute(
        _select_review_sql("WHERE r.review_id = %s::uuid"),
        (next_review_id,),
    )
    return _review_row_to_record(cur.fetchone())


def create_metadata_review_version(
    database_url: str,
    *,
    current_review: ReviewRecord,
    edits: dict[str, Any],
    source_message_id: int,
    created_by_user_id: int,
) -> dict[str, Any]:
    source_marker = str(source_message_id)
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                _select_review_sql(
                    """
                    WHERE r.review_id = %s::uuid
                      AND r.status = 'pending'
                    FOR UPDATE OF r, mn
                    """
                ),
                (current_review.review_id,),
            )
            row = cur.fetchone()
            if row:
                locked = _review_row_to_record(row)
            else:
                cur.execute(
                    _select_review_sql(
                        """
                        WHERE r.meeting_note_id = %s::uuid
                          AND r.metadata_json ->> '_gateway_source_message_id' = %s
                        ORDER BY r.review_version DESC
                        LIMIT 1
                        """
                    ),
                    (current_review.meeting_note_id, source_marker),
                )
                duplicate = cur.fetchone()
                if duplicate:
                    review = _review_row_to_record(duplicate)
                    return {
                        "status": "duplicate",
                        "bundle": _build_review_bundle(cur, review, review_status="preview"),
                    }
                raise RuntimeError("current pending review was replaced")

            cur.execute(
                _select_review_sql(
                    """
                    WHERE r.meeting_note_id = %s::uuid
                      AND r.metadata_json ->> '_gateway_source_message_id' = %s
                    ORDER BY r.review_version DESC
                    LIMIT 1
                    """
                ),
                (locked.meeting_note_id, source_marker),
            )
            duplicate = cur.fetchone()
            if duplicate:
                review = _review_row_to_record(duplicate)
                return {
                    "status": "duplicate",
                    "bundle": _build_review_bundle(cur, review, review_status="preview"),
                }

            metadata = apply_metadata_edit(locked.metadata_json, edits)
            metadata["_gateway_source_message_id"] = source_marker
            next_review = _insert_next_review_version(
                cur,
                prior=locked,
                metadata=metadata,
                created_by_user_id=created_by_user_id,
            )
            bundle = _build_review_bundle(cur, next_review, review_status="preview")
        conn.commit()
    return {"status": "created", "bundle": bundle}


def _insert_manual_append(
    cur,
    *,
    meeting_note_id: str,
    note_text: str,
    chat_id: int,
    thread_id: int | None,
    source_message_id: int,
    created_by_user_id: int,
) -> tuple[dict[str, Any], bool]:
    cur.execute(
        """
        SELECT append_id,
               append_version,
               note_text,
               created_at
        FROM tg_meeting_summary.meeting_note_appends
        WHERE meeting_note_id = %s::uuid
          AND chat_id = %s
          AND source_message_id = %s
        """,
        (meeting_note_id, chat_id, source_message_id),
    )
    existing = cur.fetchone()
    if existing:
        return (
            {
                "append_id": str(existing[0]),
                "append_version": int(existing[1]),
                "note_text": existing[2],
                "created_at": existing[3],
            },
            False,
        )
    cur.execute(
        """
        SELECT COALESCE(MAX(append_version), 0) + 1
        FROM tg_meeting_summary.meeting_note_appends
        WHERE meeting_note_id = %s::uuid
        """,
        (meeting_note_id,),
    )
    append_version = int(cur.fetchone()[0])
    cur.execute(
        """
        INSERT INTO tg_meeting_summary.meeting_note_appends (
            meeting_note_id,
            append_version,
            note_text,
            chat_id,
            thread_id,
            source_message_id,
            created_by_user_id
        ) VALUES (
            %s::uuid,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s
        )
        RETURNING append_id, append_version, note_text, created_at
        """,
        (
            meeting_note_id,
            append_version,
            note_text,
            chat_id,
            thread_id,
            source_message_id,
            created_by_user_id,
        ),
    )
    row = cur.fetchone()
    return (
        {
            "append_id": str(row[0]),
            "append_version": int(row[1]),
            "note_text": row[2],
            "created_at": row[3],
        },
        True,
    )


def create_pending_append_review_version(
    database_url: str,
    *,
    current_review: ReviewRecord,
    note_text: str,
    source_message_id: int,
    created_by_user_id: int,
) -> dict[str, Any]:
    if current_review.status != "pending":
        raise RuntimeError("pending append requires a pending review")
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                _select_review_sql(
                    """
                    WHERE r.review_id = %s::uuid
                      AND r.status = 'pending'
                    FOR UPDATE OF r, mn
                    """
                ),
                (current_review.review_id,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("current pending review was replaced")
            locked = _review_row_to_record(row)
            append_row, created = _insert_manual_append(
                cur,
                meeting_note_id=locked.meeting_note_id,
                note_text=note_text,
                chat_id=locked.chat_id,
                thread_id=locked.thread_id,
                source_message_id=source_message_id,
                created_by_user_id=created_by_user_id,
            )
            if not created:
                bundle = _build_review_bundle(cur, locked, review_status="preview")
                conn.commit()
                return {"status": "duplicate", "append": append_row, "bundle": bundle}
            next_review = _insert_next_review_version(
                cur,
                prior=locked,
                metadata=locked.metadata_json,
                created_by_user_id=created_by_user_id,
            )
            bundle = _build_review_bundle(cur, next_review, review_status="preview")
            cur.execute(
                """
                UPDATE tg_meeting_summary.meeting_note_appends
                SET markdown_content_hash = %s
                WHERE meeting_note_id = %s::uuid
                  AND append_version = %s
                """,
                (bundle.markdown_hash, locked.meeting_note_id, append_row["append_version"]),
            )
        conn.commit()
    return {"status": "created", "append": append_row, "bundle": bundle}


def accept_review_version(
    database_url: str,
    *,
    accept_token: str,
    review_version: int,
    accepted_by_user_id: int,
) -> dict[str, Any]:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                _select_review_sql(
                    """
                    WHERE r.accept_token = %s
                      AND r.review_version = %s
                    FOR UPDATE OF r, mn
                    """
                ),
                (accept_token, review_version),
            )
            row = cur.fetchone()
            if not row:
                return {"status": "not_found"}
            review = _review_row_to_record(row)
            if (
                review.status == "accepted"
                and review.meeting_note_review_status == "accepted"
                and review.review_version == review.meeting_note_review_version
            ):
                # Lost callback acknowledgement: return the retained current
                # projection, not a misleading 'replaced preview' response.
                bundle = _build_review_bundle(cur, review, review_status="accepted")
                return {"status": "accepted", "review": review, "bundle": bundle}
            if (
                review.status != "pending"
                or review.meeting_note_review_status != "pending"
                or review.review_version != review.meeting_note_review_version
            ):
                return {"status": "stale", "review": review}
            if review.preview_document_status != "sent":
                return {"status": "preview_not_sent", "review": review}
            bundle = _build_review_bundle(cur, review, review_status="accepted")
            materialisation_hash = bundle.markdown_hash
            materialisation_id = uuid.uuid5(
                RECORDING_NAMESPACE,
                f"meeting-note-materialisation:{review.meeting_note_id}:{materialisation_hash}",
            )
            document_url = f"cot://tg_meeting_summary/meeting_notes/{review.meeting_note_id}?v={review.review_version}"
            source_content_hash = content_hash(
                {
                    "meeting_note_id": review.meeting_note_id,
                    "review_version": review.review_version,
                    "metadata": review.metadata_json,
                    "markdown_hash": materialisation_hash,
                    "appends": bundle.appends,
                }
            )
            notes_content_hash = content_hash(
                {
                    "notes_json": review.notes_json,
                    "metadata": review.metadata_json,
                    "appends": bundle.appends,
                }
            )
            notes_json = _merge_note_metadata(review.notes_json, review.metadata_json)
            cur.execute(
                """
                UPDATE tg_meeting_summary.meeting_notes
                SET title = %s,
                    meeting_date = %s,
                    notes_json = %s::jsonb,
                    chat_summary_text = %s,
                    notes_content_hash = %s,
                    source_content_hash = %s,
                    review_status = 'accepted',
                    review_version = %s,
                    materialisation_id = %s::uuid,
                    materialisation_hash = %s,
                    document_url = %s,
                    materialisation_state = 'committed',
                    status = 'ready',
                    ready_at = COALESCE(ready_at, NOW()),
                    metadata_json = metadata_json || %s::jsonb,
                    updated_at = NOW()
                WHERE meeting_note_id = %s::uuid
                """,
                (
                    review.metadata_json["title"],
                    date_column_value(review.metadata_json["meeting_date"]),
                    _json_dumps(notes_json),
                    _chat_summary_text(notes_json),
                    notes_content_hash,
                    source_content_hash,
                    review.review_version,
                    materialisation_id,
                    materialisation_hash,
                    document_url,
                    _json_dumps(
                        {
                            "materialisation_contract": "meeting-summary-notes-v1.cot-ref",
                            "accepted_review_id": review.review_id,
                            "accepted_review_version": review.review_version,
                            "accepted_preview_content_hash": review.preview_content_hash,
                            "retained_markdown": bundle.markdown,
                            "retained_markdown_hash": materialisation_hash,
                        }
                    ),
                    review.meeting_note_id,
                ),
            )
            cur.execute(
                """
                UPDATE tg_meeting_summary.meeting_note_reviews
                SET status = 'accepted',
                    accepted_by_user_id = %s,
                    accepted_at = NOW(),
                    updated_at = NOW()
                WHERE review_id = %s::uuid
                """,
                (accepted_by_user_id, review.review_id),
            )
        conn.commit()
    return {"status": "accepted", "review": review, "bundle": bundle, "document_url": document_url}


def append_to_accepted_note(
    database_url: str,
    *,
    current_review: ReviewRecord,
    note_text: str,
    source_message_id: int,
    created_by_user_id: int,
) -> dict[str, Any]:
    if current_review.status != "accepted":
        raise RuntimeError("accepted append requires an accepted review")
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                _select_review_sql(
                    """
                    WHERE r.review_id = %s::uuid
                      AND r.status = 'accepted'
                    FOR UPDATE OF r, mn
                    """
                ),
                (current_review.review_id,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("accepted review was replaced")
            locked = _review_row_to_record(row)
            append_row, created = _insert_manual_append(
                cur,
                meeting_note_id=locked.meeting_note_id,
                note_text=note_text,
                chat_id=locked.chat_id,
                thread_id=locked.thread_id,
                source_message_id=source_message_id,
                created_by_user_id=created_by_user_id,
            )
            bundle = _build_review_bundle(cur, locked, review_status="accepted")
            if not created:
                conn.commit()
                return {"status": "duplicate", "append": append_row, "bundle": bundle}
            materialisation_hash = bundle.markdown_hash
            materialisation_id = uuid.uuid5(
                RECORDING_NAMESPACE,
                f"meeting-note-materialisation:{locked.meeting_note_id}:{materialisation_hash}",
            )
            document_url = (
                f"cot://tg_meeting_summary/meeting_notes/{locked.meeting_note_id}"
                f"?v={locked.review_version}&append={append_row['append_version']}"
            )
            source_content_hash = content_hash(
                {
                    "meeting_note_id": locked.meeting_note_id,
                    "review_version": locked.review_version,
                    "metadata": locked.metadata_json,
                    "markdown_hash": materialisation_hash,
                    "appends": bundle.appends,
                }
            )
            notes_content_hash = content_hash(
                {
                    "notes_json": locked.notes_json,
                    "metadata": locked.metadata_json,
                    "appends": bundle.appends,
                }
            )
            cur.execute(
                """
                UPDATE tg_meeting_summary.meeting_notes
                SET notes_content_hash = %s,
                    source_content_hash = %s,
                    materialisation_id = %s::uuid,
                    materialisation_hash = %s,
                    document_url = %s,
                    materialisation_state = 'committed',
                    status = 'ready',
                    metadata_json = metadata_json || %s::jsonb,
                    updated_at = NOW()
                WHERE meeting_note_id = %s::uuid
                """,
                (
                    notes_content_hash,
                    source_content_hash,
                    materialisation_id,
                    materialisation_hash,
                    document_url,
                    _json_dumps({
                        "latest_append_version": append_row["append_version"],
                        "retained_markdown": bundle.markdown,
                        "retained_markdown_hash": materialisation_hash,
                    }),
                    locked.meeting_note_id,
                ),
            )
            cur.execute(
                """
                UPDATE tg_meeting_summary.meeting_note_appends
                SET markdown_content_hash = %s,
                    document_url = %s
                WHERE meeting_note_id = %s::uuid
                  AND append_version = %s
                """,
                (materialisation_hash, document_url, locked.meeting_note_id, append_row["append_version"]),
            )
        conn.commit()
    return {"status": "created", "append": append_row, "bundle": bundle, "document_url": document_url}


def record_append_document_delivery(
    database_url: str,
    *,
    meeting_note_id: str,
    append_version: int,
    document_message_id: int | None,
    document_file_id: str | None,
) -> None:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tg_meeting_summary.meeting_note_appends
                SET preview_document_message_id = %s,
                    preview_document_file_id = %s
                WHERE meeting_note_id = %s::uuid
                  AND append_version = %s
                """,
                (document_message_id, document_file_id, meeting_note_id, append_version),
            )
        conn.commit()


