from __future__ import annotations

from typing import Any

from tg_tools_common import runtime_contract
from tg_tools_common.delivery_contract import telegram_reply_document, telegram_reply_text


RESULT_OWNER = "rss-audio"
TRANSCRIBE_RESULT_OWNER = "transcribe-audio"
RESULT_ROUTE = "telegram.rss-audio.confirmation.v1"
TRANSCRIBE_RESULT_ROUTE = "telegram.transcribe-audio.reply.v1"


class DeliveryTargetMissing(ValueError):
    """The job carries no usable Telegram messageRef to reply to.

    Raised instead of a bare ``KeyError``/``TypeError`` so callers can tell
    "this job has nowhere to send a notification" apart from a genuine bug.
    A missing reply target must never fail an otherwise successful publish:
    the episode is live, only the confirmation message is undeliverable.
    """


def message_reference(input_json: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise the Telegram reply target.

    ``messageRef`` arrives from the gateway and is not guaranteed present or
    well-typed. Indexing it raw made a malformed job raise ``KeyError`` deep
    inside delivery construction, where nothing modelled it -- the claimed
    attempt then went unfinalized for the whole lease.
    """
    raw = input_json.get("messageRef")
    if not isinstance(raw, dict):
        raise DeliveryTargetMissing("job has no messageRef")
    try:
        chat_id = int(raw["chat_id"])
        message_id = int(raw["message_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DeliveryTargetMissing(f"messageRef is unusable: {exc}") from exc
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "message_thread_id": raw.get("message_thread_id"),
    }


def confirmation_delivery_payload(
    *,
    job_id: str,
    result_id: str | None,
    input_json: dict[str, Any],
    html_text: str,
    delivery_key_suffix: str | None = None,
) -> dict[str, Any]:
    message_ref = message_reference(input_json)
    source_key = str(input_json.get("sourceKey") or delivery_key_suffix or job_id)
    delivery_key = f"rss-audio:telegram-confirmation:{delivery_key_suffix or source_key}"
    payload_json = telegram_reply_text(
        chat_id=message_ref["chat_id"],
        thread_id=message_ref.get("message_thread_id"),
        reply_to_message_id=message_ref["message_id"],
        text=html_text,
        parse_mode="HTML",
    )
    payload_json["disableWebPagePreview"] = True
    return _delivery_payload(
        result_owner=RESULT_OWNER,
        result_route=RESULT_ROUTE,
        delivery_kind=str(payload_json["deliveryKind"]),
        delivery_key=delivery_key,
        priority=60,
        job_id=job_id,
        result_id=result_id,
        payload_json=payload_json,
    )


def transcript_delivery_payload(
    *,
    job_id: str,
    result_id: str | None,
    input_json: dict[str, Any],
    transcript: str,
    filename: str,
    caption: str,
) -> dict[str, Any]:
    message_ref = message_reference(input_json)
    source_key = str(input_json.get("sourceKey") or job_id)
    payload_json = telegram_reply_document(
        chat_id=message_ref["chat_id"],
        thread_id=message_ref.get("message_thread_id"),
        reply_to_message_id=message_ref["message_id"],
        content=transcript.rstrip() + "\n",
        file_name=filename,
        caption=caption,
    )
    delivery_kind = str(payload_json["deliveryKind"])
    scribe = input_json.get("scribe")
    if isinstance(scribe, dict):
        payload_json["scribeLink"] = {
            "botIdentity": "rss-audio",
            "threadId": scribe.get("threadId"),
            "entryId": scribe.get("entryId"),
            "revisionId": scribe.get("revisionId"),
            "revision": scribe.get("revision"),
            "role": scribe.get("deliveryRole") or "bot_markdown_revision",
        }
    return _delivery_payload(
        result_owner=TRANSCRIBE_RESULT_OWNER,
        result_route=TRANSCRIBE_RESULT_ROUTE,
        delivery_kind=delivery_kind,
        delivery_key=f"transcribe-audio:telegram-transcript:{source_key}",
        priority=65,
        job_id=job_id,
        result_id=result_id,
        payload_json=payload_json,
    )


def scribe_revision_delivery_payload(
    *,
    job_id: str,
    result_id: str | None,
    input_json: dict[str, Any],
    markdown_text: str,
    filename: str,
    caption: str,
    revision_id: str,
    revision: int,
) -> dict[str, Any]:
    scribe = dict(input_json.get("scribe") or {})
    scribe["revisionId"] = revision_id
    scribe["revision"] = revision
    scribe["deliveryRole"] = "bot_markdown_revision"
    enriched = dict(input_json)
    enriched["scribe"] = scribe
    return transcript_delivery_payload(
        job_id=job_id,
        result_id=result_id,
        input_json=enriched,
        transcript=markdown_text,
        filename=filename,
        caption=caption,
    )


def _delivery_payload(
    *,
    result_owner: str,
    result_route: str,
    delivery_kind: str,
    delivery_key: str,
    priority: int,
    job_id: str,
    result_id: str | None,
    payload_json: dict[str, Any],
) -> dict[str, Any]:
    payload = runtime_contract.build_delivery_payload(
        result_owner=result_owner,
        result_route=result_route,
        delivery_key_str=delivery_key,
        priority=priority,
        payload_json=payload_json,
        max_attempts=5,
    )
    payload["deliveryKind"] = delivery_kind
    payload["jobId"] = job_id
    payload["resultId"] = result_id
    return payload
