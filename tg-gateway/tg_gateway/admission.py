from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from tg_tools_common.intent_contract import RuntimeJobIntent, TelegramSourceIdentity
from tg_tools_common import telegram_routes


@dataclass(frozen=True, slots=True)
class RouteAdmission:
    source_receipt_payload: dict[str, Any]
    job_submit_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AdmissionInput:
    text: str = ""
    attachment_refs: tuple[dict[str, Any], ...] = ()
    status_message_id: int | None = None
    submitted_at: str | None = None
    tts_options: dict[str, Any] | None = None
    transcribe_options: dict[str, Any] | None = None
    scribe: dict[str, Any] | None = None


def build_route_admission(
    *,
    shape: telegram_routes.TelegramUpdateShape,
    resolution: telegram_routes.RouteResolution,
    admission_input: AdmissionInput | None = None,
) -> RouteAdmission:
    if resolution.route is None or not resolution.matched:
        raise ValueError("route resolution must be matched")
    route = resolution.route
    data = admission_input or AdmissionInput(text=shape.text or "")
    builders: dict[str, Callable[[telegram_routes.TelegramUpdateShape, telegram_routes.MatchedRoute, AdmissionInput], RouteAdmission]] = {
        "meeting_note_route": _build_meeting_note_admission,
        "rss_publish_route": _build_rss_publish_admission,
        "transcribe_audio_route": _build_transcribe_audio_admission,
        "tts_synthesize_route": _build_tts_synthesize_admission,
        "x_tts_explain_route": _build_x_tts_explain_admission,
    }
    try:
        builder = builders[route.route_kind]
    except KeyError as exc:
        raise ValueError(f"unsupported route kind: {route.route_kind}") from exc
    return builder(shape, route, data)


def _source(shape: telegram_routes.TelegramUpdateShape) -> TelegramSourceIdentity:
    return TelegramSourceIdentity(
        bot_identity=shape.bot_identity,
        chat_id=shape.chat_id,
        thread_id=shape.thread_id,
        message_id=shape.message_id,
        reply_to_message_id=shape.reply_to_message_id,
    )


def _message_ref(shape: telegram_routes.TelegramUpdateShape) -> dict[str, Any]:
    return {
        "chat_id": shape.chat_id,
        "message_thread_id": shape.thread_id,
        "message_id": shape.message_id,
        "reply_to_message_id": shape.reply_to_message_id,
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _attachment(data: AdmissionInput) -> dict[str, Any]:
    return dict(data.attachment_refs[0]) if data.attachment_refs else {}


def _receipt(shape: telegram_routes.TelegramUpdateShape, route: telegram_routes.MatchedRoute, source_json: dict[str, Any]) -> dict[str, Any]:
    return _source(shape).to_source_receipt_payload(
        producer_system=route.owner_system,
        workload_type=route.workload_type,
        route_id=route.route_id,
        route_kind=route.route_kind,
        matched_rule_version=route.matched_rule_version,
        source_json={
            "schemaVersion": 1,
            "botIdentity": shape.bot_identity,
            "chatId": shape.chat_id,
            "threadId": shape.thread_id,
            "messageId": shape.message_id,
            "senderUserId": shape.sender_user_id,
            **source_json,
        },
    )


def _admission(receipt: dict[str, Any], job: dict[str, Any]) -> RouteAdmission:
    return RouteAdmission(source_receipt_payload=receipt, job_submit_payload=job)


def _build_meeting_note_admission(shape: telegram_routes.TelegramUpdateShape, route: telegram_routes.MatchedRoute, data: AdmissionInput) -> RouteAdmission:
    source_key = _source(shape).source_key
    att = _attachment(data)
    file_name = str(att.get("fileName") or att.get("file_name") or f"audio_{shape.message_id}")
    mime_type = str(att.get("mimeType") or att.get("mime_type") or "application/octet-stream")
    file_size = att.get("fileSizeBytes", att.get("fileSize", att.get("file_size")))
    receipt = _receipt(shape, route, {"statusMessageId": data.status_message_id, "userId": shape.sender_user_id or 0, "fileName": file_name, "mimeType": mime_type, "fileSizeBytes": file_size})
    input_json = {
        "version": 1,
        "sourceKind": "telegram",
        "sourceKey": source_key,
        "jobLabel": file_name,
        "submittedAt": data.submitted_at or _now_iso(),
        "chatId": shape.chat_id,
        "threadId": shape.thread_id,
        "messageId": shape.message_id,
        "statusMessageId": data.status_message_id,
        "userId": shape.sender_user_id or 0,
        "fileId": att.get("fileId") or att.get("file_id"),
        "fileUniqueId": att.get("fileUniqueId") or att.get("file_unique_id"),
        "fileName": file_name,
        "mimeType": mime_type,
        "fileSizeBytes": file_size,
        "localSourcePath": att.get("localSourcePath") or att.get("sourcePath"),
        "localWorkDir": att.get("localWorkDir"),
    }
    input_json = {k: v for k, v in input_json.items() if v is not None}
    job = RuntimeJobIntent(
        producer_system="meeting-summary",
        producer_ref=source_key,
        result_owner="meeting-summary",
        result_route="meeting-summary.recording.processed.v1",
        workload_type="meeting_summary",
        queue_class="foreground",
        execution_profile="cpu_media",
        execution_shape="local_bounded",
        input_json=input_json,
        idempotency_key=source_key,
        effect_key=source_key,
        max_attempts=3,
    ).to_submit_payload()
    return _admission(receipt, job)


def _job_common(shape: telegram_routes.TelegramUpdateShape, data: AdmissionInput) -> tuple[str, dict[str, Any], dict[str, Any], str]:
    source_key = _source(shape).source_key
    att = _attachment(data)
    text = data.text or shape.text or ""
    input_kind = "telegram_audio" if att else ("url" if shape.has_url else "text")
    return source_key, att, {"kind": "rss", "sourceKey": source_key, "messageRef": _message_ref(shape), "inputKind": input_kind}, text


def _build_rss_publish_admission(shape: telegram_routes.TelegramUpdateShape, route: telegram_routes.MatchedRoute, data: AdmissionInput) -> RouteAdmission:
    source_key, att, base, text = _job_common(shape, data)
    use_url = base["inputKind"] == "url"
    source_url, _ = _extract_single_url(text) if use_url else (None, text)
    if use_url and source_url is None:
        raise ValueError("RSS sources accept exactly one URL per message")
    if not source_url and not att:
        raise ValueError("RSS sources require a supported URL or audio attachment")
    source_attachment = att if not use_url else {}
    input_json = {
        **base,
        "status": {"chatId": shape.chat_id, "threadId": shape.thread_id, "messageId": data.status_message_id},
        "sourceUrl": source_url,
        "telegramFileId": source_attachment.get("fileId"),
        "telegramFileName": source_attachment.get("fileName"),
        "titleHint": source_attachment.get("titleHint") or source_attachment.get("fileName"),
        "fileSize": source_attachment.get("fileSize"),
        "mimeType": source_attachment.get("mimeType"),
    }
    input_json = {k: v for k, v in input_json.items() if v is not None}
    receipt = _receipt(shape, route, {"inputKind": base["inputKind"], "jobKind": "rss"})
    job = RuntimeJobIntent(
        producer_system="rss-audio",
        producer_ref=source_key,
        result_owner="rss-audio",
        result_route="telegram.rss-audio.confirmation.v1",
        workload_type="rss_publish",
        queue_class="foreground",
        execution_profile="cpu_media",
        execution_shape="local_bounded",
        priority=60,
        input_json=input_json,
        output_contract_json={"version": 1, "result": "rss episode publication plus telegram confirmation delivery"},
        idempotency_key=source_key,
        effect_key=f"rss-audio:feed-publish:{source_key}",
        max_attempts=3,
    ).to_submit_payload()
    return _admission(receipt, job)


def _build_transcribe_audio_admission(shape: telegram_routes.TelegramUpdateShape, route: telegram_routes.MatchedRoute, data: AdmissionInput) -> RouteAdmission:
    source_key, att, base, text = _job_common(shape, data)
    input_kind = (data.scribe or {}).get("inputKind") or base["inputKind"]
    use_url = input_kind in {"url", "direct_audio_url"}
    source_url, _ = _extract_single_url(text) if use_url else (None, text)
    if use_url and source_url is None:
        raise ValueError("Transcription sources accept exactly one URL per message")
    source_attachment = att if input_kind == "telegram_audio" else {}
    input_json = {
        **base,
        "inputKind": input_kind,
        "kind": "transcribe",
        "status": {"chatId": shape.chat_id, "threadId": shape.thread_id, "messageId": data.status_message_id},
        "sourceUrl": source_url,
        "sourceText": text if text and input_kind == "telegram_text" else None,
        "telegramFileId": source_attachment.get("fileId"),
        "telegramFileName": source_attachment.get("fileName"),
        "titleHint": source_attachment.get("titleHint") or source_attachment.get("fileName"),
        "fileSize": source_attachment.get("fileSize"),
        "mimeType": source_attachment.get("mimeType"),
        "scribe": data.scribe,
        **(data.transcribe_options or {"model": "gpt-4o-mini-transcribe", "provider": "azure", "jobs": 3}),
    }
    input_json = {k: v for k, v in input_json.items() if v is not None}
    receipt = _receipt(shape, route, {"inputKind": base["inputKind"], "jobKind": "transcribe"})
    job = RuntimeJobIntent(
        producer_system="rss-audio",
        producer_ref=source_key,
        result_owner="transcribe-audio",
        result_route="telegram.transcribe-audio.reply.v1",
        workload_type="transcribe_audio",
        queue_class="foreground",
        execution_profile="cpu_media",
        execution_shape="local_bounded",
        priority=65,
        input_json=input_json,
        output_contract_json={"version": 1, "result": "scribe markdown revision plus telegram delivery"},
        idempotency_key=source_key,
        effect_key=f"transcribe-audio:transcript:{source_key}",
        max_attempts=2,
    ).to_submit_payload()
    return _admission(receipt, job)


def _tts_input_kind(shape: telegram_routes.TelegramUpdateShape, data: AdmissionInput) -> str:
    # A reply to an earlier document is not admitted: there is no durable
    # lookup of replied documents.
    if data.attachment_refs:
        return "telegram_document"
    return "url" if shape.has_url else "text"


def _build_tts_synthesize_admission(shape: telegram_routes.TelegramUpdateShape, route: telegram_routes.MatchedRoute, data: AdmissionInput) -> RouteAdmission:
    return _build_tts_like(shape, route, data, explain=False)


def _build_x_tts_explain_admission(shape: telegram_routes.TelegramUpdateShape, route: telegram_routes.MatchedRoute, data: AdmissionInput) -> RouteAdmission:
    return _build_tts_like(shape, route, data, explain=True)


_SINGLE_URL_RE = re.compile(r"https?://\S+")


def _extract_single_url(text: str) -> tuple[str | None, str]:
    """Return (URL, surrounding text) when exactly one URL is present."""
    urls = _SINGLE_URL_RE.findall(text)
    if len(urls) != 1:
        return None, text
    remainder = text.replace(urls[0], "").strip()
    return urls[0], remainder


def _build_tts_like(shape: telegram_routes.TelegramUpdateShape, route: telegram_routes.MatchedRoute, data: AdmissionInput, *, explain: bool) -> RouteAdmission:
    source_key = _source(shape).source_key
    att = _attachment(data)
    text = data.text or shape.text or ""
    url: str | None = None
    source_text: str | None = None
    if not att and shape.has_url:
        url, _surrounding_text = _extract_single_url(text)
        if url is None:
            raise ValueError("TTS sources accept exactly one URL per message")
    elif not att:
        source_text = text
    input_json = {
        "schemaVersion": 1,
        "sourceKey": source_key,
        "messageRef": _message_ref(shape),
        "inputKind": _tts_input_kind(shape, data),
        "telegramFileId": att.get("fileId") or att.get("sourceFileId"),
        "telegramFileName": att.get("fileName") or att.get("displayName"),
        "mimeType": att.get("mimeType"),
        "localSourcePath": att.get("localSourcePath") or att.get("sourcePath") or att.get("path"),
        "sourceUrl": url,
        "sourceText": source_text,
    }
    options = dict(data.tts_options or {})
    forbidden = {"length", "model", "backend", "promptSelector"}.intersection(options)
    if explain and forbidden:
        raise ValueError("x-tts explain does not accept selectors: " +
                         ", ".join(sorted(forbidden)))
    if explain:
        input_json.update({"mode": "explain", "promptVersion": "reader-v4"})
    else:
        input_json.update({"speakingRate": "normal"})
    input_json.update(options)
    input_json = {k: v for k, v in input_json.items() if v is not None}
    required = [
        "x_tts_source_markdown",
        "x_tts_script_markdown",
        "x_tts_final_speech_text",
        "x_tts_audio_mp3",
        "x_tts_primary_enriched_markdown",
        "x_tts_episode_manifest",
    ] if explain else ["tts_source_markdown", "tts_speech_text", "tts_audio_mp3", "tts_episode_manifest"]
    owner = "x-tts" if explain else "tts"
    receipt = _receipt(shape, route, {"inputKind": input_json["inputKind"], "attachmentCount": len(data.attachment_refs)})
    job = RuntimeJobIntent(
        producer_system=owner,
        producer_ref=source_key,
        result_owner=owner,
        result_route=f"telegram.{owner}.confirmation.v1",
        workload_type="x_tts_explain" if explain else "tts_synthesize",
        queue_class="foreground",
        execution_profile="model_tts_io" if explain else "cpu_io",
        execution_shape="local_bounded",
        priority=63 if explain else 62,
        input_json=input_json,
        output_contract_json={"schemaVersion": 1, "requiredArtifacts": required, "result": "explained audio episode plus telegram confirmation" if explain else "tts audio episode plus telegram confirmation"},
        idempotency_key=source_key,
        effect_key=f"{owner}:feed-publish:{source_key}",
        max_attempts=2 if explain else 1,
    ).to_submit_payload()
    return _admission(receipt, job)
