from __future__ import annotations

from typing import Any, Literal

CanonicalDeliveryKind = Literal[
    "telegram_text",
    "telegram_document",
    "telegram_edit",
    "telegram_reply_text",
    "telegram_reply_document",
]

CANONICAL_DELIVERY_KINDS = frozenset(
    {
        "telegram_text",
        "telegram_document",
        "telegram_edit",
        "telegram_reply_text",
        "telegram_reply_document",
    }
)


def telegram_text(*, chat_id: int, text: str, thread_id: int | None = None, parse_mode: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"deliveryKind": "telegram_text", "chatId": chat_id, "text": text}
    if thread_id is not None:
        payload["threadId"] = thread_id
    if parse_mode is not None:
        payload["parseMode"] = parse_mode
    return payload


def telegram_reply_text(
    *,
    chat_id: int,
    reply_to_message_id: int,
    text: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> dict[str, Any]:
    payload = telegram_text(chat_id=chat_id, text=text, thread_id=thread_id, parse_mode=parse_mode)
    payload["deliveryKind"] = "telegram_reply_text"
    payload["replyToMessageId"] = reply_to_message_id
    return payload


def telegram_document(
    *,
    chat_id: int,
    file_name: str,
    content: str | bytes | None = None,
    document_path: str | None = None,
    thread_id: int | None = None,
    caption: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"deliveryKind": "telegram_document", "chatId": chat_id, "fileName": file_name}
    if content is not None:
        payload["content"] = content
    if document_path is not None:
        payload["documentPath"] = document_path
    if thread_id is not None:
        payload["threadId"] = thread_id
    if caption is not None:
        payload["caption"] = caption
    return payload


def telegram_reply_document(
    *,
    chat_id: int,
    reply_to_message_id: int,
    file_name: str,
    content: str | bytes | None = None,
    document_path: str | None = None,
    thread_id: int | None = None,
    caption: str | None = None,
) -> dict[str, Any]:
    payload = telegram_document(
        chat_id=chat_id,
        file_name=file_name,
        content=content,
        document_path=document_path,
        thread_id=thread_id,
        caption=caption,
    )
    payload["deliveryKind"] = "telegram_reply_document"
    payload["replyToMessageId"] = reply_to_message_id
    return payload


def telegram_edit(*, chat_id: int, message_id: int, text: str, parse_mode: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"deliveryKind": "telegram_edit", "chatId": chat_id, "messageId": message_id, "text": text}
    if parse_mode is not None:
        payload["parseMode"] = parse_mode
    return payload
