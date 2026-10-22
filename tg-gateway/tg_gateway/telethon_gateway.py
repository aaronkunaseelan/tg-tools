from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tg_tools_common.attachments import audio_suffix_from_mime, classify_prompt_document

from .models import (
    DownloadedAttachment,
    InboundAttachment,
    InboundCallback,
    InboundMessage,
    OutboundMessage,
)


_FILE_ID_PREFIX = "mtmsg"
_AUDIO_DOCUMENT_SUFFIXES = (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".webm", ".mp4")


def encode_message_file_id(chat_id: int, message_id: int) -> str:
    return f"{_FILE_ID_PREFIX}:{chat_id}:{message_id}"


def decode_message_file_id(file_id: str) -> tuple[int, int]:
    parts = str(file_id or "").split(":")
    if len(parts) != 3 or parts[0] != _FILE_ID_PREFIX:
        raise ValueError(f"Unsupported MTProto file id: {file_id}")
    try:
        return int(parts[1]), int(parts[2])
    except ValueError as exc:
        raise ValueError(f"Invalid MTProto file id: {file_id}") from exc


class TelethonGateway:
    def __init__(
        self,
        *,
        client: object | None = None,
        session_path: str | Path | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
        bot_token: str | None = None,
    ) -> None:
        self._session_path = str(Path(session_path).expanduser().resolve()) if session_path is not None else None
        self._api_id = api_id
        self._api_hash = api_hash
        self._bot_token = bot_token
        self._client = client or self._build_client()
        self._registered = False
        self._entity_cache: dict[int, Any] = {}

    @property
    def client(self) -> object:
        return self._client

    def _build_client(self):
        if self._session_path is None or self._api_id is None or not self._api_hash:
            raise ValueError("session_path, api_id, and api_hash are required when no client is provided")
        from telethon import TelegramClient

        return TelegramClient(self._session_path, self._api_id, self._api_hash)

    async def connect(self) -> None:
        if self._session_path is not None:
            Path(self._session_path).parent.mkdir(parents=True, exist_ok=True)
        if self._bot_token:
            await self._client.start(bot_token=self._bot_token)
        else:
            await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def run_until_disconnected(self) -> None:
        await self._client.run_until_disconnected()

    def register_handlers(self, *, on_message, on_callback) -> None:
        if self._registered:
            return
        self._registered = True
        from telethon import events

        @self._client.on(events.NewMessage(incoming=True))
        async def _handle_message(event) -> None:
            await on_message(event)

        @self._client.on(events.CallbackQuery())
        async def _handle_callback(event) -> None:
            await on_callback(event)

    async def _get_entity(self, chat_id: int):
        cached = self._entity_cache.get(chat_id)
        if cached is not None:
            return cached
        entity = await self._client.get_input_entity(chat_id)
        self._entity_cache[chat_id] = entity
        return entity

    @staticmethod
    def message_thread_id_from_raw(raw_message: Any) -> int | None:
        action = getattr(raw_message, "action", None)
        if action is not None and action.__class__.__name__ == "MessageActionTopicCreate":
            return int(getattr(raw_message, "id", 0) or 0) or None
        reply = getattr(raw_message, "reply_to", None)
        if reply is not None:
            top_id = getattr(reply, "reply_to_top_id", None)
            if top_id:
                return int(top_id)
            if getattr(reply, "forum_topic", False):
                msg_id = getattr(reply, "reply_to_msg_id", None)
                if msg_id:
                    return int(msg_id)
        return None

    @classmethod
    def _raw_chat_id(cls, raw_message: Any) -> int:
        raw_chat_id = getattr(raw_message, "chat_id", None)
        if raw_chat_id not in (None, 0):
            return int(raw_chat_id)
        chat = getattr(raw_message, "chat", None)
        return int(getattr(chat, "id", 0) or 0)

    @classmethod
    def _extract_attachment(cls, raw_message: Any, chat_id: int) -> tuple[InboundAttachment, ...]:
        file_obj = getattr(raw_message, "file", None)
        if file_obj is None:
            return ()
        file_id = encode_message_file_id(chat_id, int(getattr(raw_message, "id", 0) or 0))
        mime_type = getattr(file_obj, "mime_type", None)
        file_size = getattr(file_obj, "size", None)
        file_name = getattr(file_obj, "name", None) or f"attachment-{getattr(raw_message, 'id', 0)}"
        if getattr(raw_message, "voice", None) is not None:
            return (
                InboundAttachment(
                    kind="voice",
                    file_id=file_id,
                    file_name=f"voice-note-{getattr(raw_message, 'id', 0)}{audio_suffix_from_mime(mime_type)}",
                    mime_type=mime_type or "audio/ogg",
                    file_size=file_size,
                    display_name="voice note transcript",
                ),
            )
        if getattr(raw_message, "audio", None) is not None:
            return (
                InboundAttachment(
                    kind="audio",
                    file_id=file_id,
                    file_name=file_name,
                    mime_type=mime_type,
                    file_size=file_size,
                    display_name=f"{file_name} transcript",
                ),
            )
        if getattr(raw_message, "document", None) is not None:
            lowered_name = file_name.lower()
            lowered_mime = (mime_type or "").lower()
            if lowered_mime.startswith("audio/") or lowered_name.endswith(_AUDIO_DOCUMENT_SUFFIXES):
                kind = "document_audio"
            else:
                kind = classify_prompt_document(file_name, mime_type) or "document"
            return (
                InboundAttachment(
                    kind=kind,
                    file_id=file_id,
                    file_name=file_name,
                    mime_type=mime_type,
                    file_size=file_size,
                    display_name=file_name,
                ),
            )
        return ()

    @classmethod
    def normalize_message(cls, raw_message: Any, *, received_at: str = "") -> InboundMessage:
        chat_id = cls._raw_chat_id(raw_message)
        thread_id = cls.message_thread_id_from_raw(raw_message) or int(getattr(raw_message, "id", 0) or 0)
        # Telethon stores both plain message text and media captions in
        # ``message``. ``caption`` exists on some test/facade objects only.
        text = getattr(raw_message, "message", None)
        if text is None:
            text = getattr(raw_message, "caption", None)
        reply = getattr(raw_message, "reply_to", None)
        reply_to_message_id = int(getattr(reply, "reply_to_msg_id", 0) or 0) or None
        return InboundMessage(
            receipt_key=f"msg:{chat_id}:{int(getattr(raw_message, 'id', 0) or 0)}",
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=int(getattr(raw_message, "id", 0) or 0),
            user_id=int(getattr(raw_message, "sender_id", 0) or 0) or None,
            text=text,
            attachments=cls._extract_attachment(raw_message, chat_id),
            reply_to_message_id=reply_to_message_id,
            received_at=received_at,
        )

    @classmethod
    async def normalize_callback(cls, event: Any, *, received_at: str = "") -> InboundCallback:
        raw_message = await event.get_message()
        chat_id = cls._raw_chat_id(raw_message)
        thread_id = cls.message_thread_id_from_raw(raw_message) or int(getattr(raw_message, "id", 0) or 0)
        data = getattr(event, "data", None)
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        query = getattr(event, "query", SimpleNamespace(query_id=getattr(event, "id", "")))
        return InboundCallback(
            receipt_key=f"cb:{getattr(query, 'query_id', getattr(event, 'id', ''))}",
            chat_id=chat_id,
            thread_id=thread_id,
            callback_query_id=str(getattr(query, "query_id", getattr(event, "id", ""))),
            message_id=int(getattr(raw_message, "id", 0) or 0) or None,
            user_id=int(getattr(event, "sender_id", 0) or 0) or None,
            data=data,
            received_at=received_at,
        )

    async def send_text(self, message: OutboundMessage) -> int:
        buttons = None
        if message.buttons:
            from telethon import Button

            buttons = [[Button.inline(button.text, data=button.callback_data.encode("utf-8")) for button in row] for row in message.buttons]
        entity = await self._get_entity(message.chat_id)
        reply_to = message.reply_to_message_id if message.reply_to_message_id is not None else message.thread_id
        result = await self._client.send_message(
            entity,
            message.text,
            reply_to=reply_to,
            link_preview=False,
            parse_mode=message.parse_mode,
            buttons=buttons,
        )
        return int(getattr(result, "id", 0) or 0)

    async def download_file(self, file_id: str, *, target_path: str | Path) -> DownloadedAttachment:
        chat_id, message_id = decode_message_file_id(file_id)
        entity = await self._get_entity(chat_id)
        message = await self._client.get_messages(entity, ids=message_id)
        if message is None or getattr(message, "media", None) is None:
            raise ValueError(f"No media found for file id {file_id}")
        path = await self._client.download_media(message, file=str(target_path))
        if path is None:
            raise RuntimeError("download_media returned None")
        file_obj = getattr(message, "file", None)
        return DownloadedAttachment(
            file_id=file_id,
            path=str(path),
            file_name=getattr(file_obj, "name", None) or Path(path).name,
            mime_type=getattr(file_obj, "mime_type", None),
        )
