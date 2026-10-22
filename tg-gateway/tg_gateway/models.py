from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InboundAttachment:
    kind: str
    file_id: str
    file_name: str
    mime_type: str | None
    file_size: int | None
    display_name: str


@dataclass(frozen=True)
class InboundMessage:
    receipt_key: str
    chat_id: int
    thread_id: int
    message_id: int
    user_id: int | None
    text: str | None
    attachments: tuple[InboundAttachment, ...] = ()
    reply_to_message_id: int | None = None
    received_at: str = ""


@dataclass(frozen=True)
class InboundCallback:
    receipt_key: str
    chat_id: int
    thread_id: int
    callback_query_id: str
    message_id: int | None
    user_id: int | None
    data: str | None
    received_at: str = ""


@dataclass(frozen=True)
class InlineButton:
    text: str
    callback_data: str


@dataclass(frozen=True)
class OutboundMessage:
    chat_id: int
    thread_id: int | None
    text: str
    reply_to_message_id: int | None = None
    parse_mode: str | None = None
    buttons: tuple[tuple[InlineButton, ...], ...] = ()


@dataclass(frozen=True)
class DownloadedAttachment:
    file_id: str
    path: str
    file_name: str
    mime_type: str | None = None
