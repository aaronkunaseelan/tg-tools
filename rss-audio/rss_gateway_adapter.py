from __future__ import annotations

import asyncio
import os
from typing import Any

from config import load_settings
from pipeline import PipelineError, delete_episode, list_recent_episodes
from source_audio import classify_text, stable_url_for_display
from scribe_store import ScribeStore
from tg_gateway.admission import AdmissionInput
from tg_gateway.models import OutboundMessage
from tg_tools_common.telegram_routes import RouteResolution, TelegramUpdateShape, telegram_source_key


_AUDIO_SUFFIXES = (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".webm", ".mp4")


class GatewayAdapter:
    def __init__(self, *, settings: Any | None = None, store: ScribeStore | None = None) -> None:
        self.settings = settings or load_settings()
        self.transcribe_jobs = max(1, int(os.environ.get("TRANSCRIBE_JOBS", "3")))
        self.models = {
            "mini": os.environ.get("TRANSCRIBE_AUDIO_MODEL_MINI", "gpt-4o-mini-transcribe"),
            "full": os.environ.get("TRANSCRIBE_AUDIO_MODEL_FULL", "gpt-4o-transcribe"),
        }
        self.model_key = "mini"
        self.store = store or ScribeStore(self.settings.scribe_db_path)

    async def _reply(self, runtime: Any, message: Any, text: str, *, parse_mode: str | None = None) -> int:
        return await runtime.gateway.send_text(
            OutboundMessage(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to_message_id=message.message_id,
                text=text,
                parse_mode=parse_mode,
            )
        )

    async def handle_command(self, runtime: Any, message: Any, *, command: str, arg: str) -> bool:
        if command == "start":
            await self._reply(runtime, message, "Send a URL or audio file to publish an episode.\nSend audio or a link in the scribe topic to transcribe it.")
            return True
        if command == "where":
            bound = sorted(name for name, value in runtime.settings.route_bindings.items()
                           if value == message.thread_id)
            await self._reply(runtime, message, "\n".join([
                f"chat_id={message.chat_id}",
                f"thread_id={message.thread_id}",
                f"bindings={', '.join(bound) or 'none'}",
            ]))
            return True
        if command == "feed":
            await self._reply(runtime, message, self.settings.feed_url)
            return True
        if command == "list":
            rows = await asyncio.to_thread(list_recent_episodes, self.settings, 10)
            text = "No episodes found." if not rows else "\n".join(f"{slug} - {title}" for slug, title in rows)
            await self._reply(runtime, message, text)
            return True
        if command == "delete":
            slug = arg.strip().split(maxsplit=1)[0] if arg.strip() else ""
            if not slug:
                await self._reply(runtime, message, "Usage: /delete YYYY-MM-DD-slug")
                return True
            try:
                await asyncio.to_thread(delete_episode, self.settings, slug)
            except PipelineError as exc:
                await self._reply(runtime, message, f"Delete failed at {exc.stage}: {exc}")
            else:
                await self._reply(runtime, message, f"Deleted {slug}.")
            return True
        if command == "model":
            requested = arg.strip().lower()
            if requested in self.models:
                self.model_key = requested
                await self._reply(runtime, message, f"Transcribe model -> {self.models[requested]} [azure]")
            else:
                await self._reply(
                    runtime,
                    message,
                    "\n".join(
                        [
                            f"Current: {self.models[self.model_key]} [azure]",
                            "Usage: /model mini | full",
                            f"mini -> {self.models['mini']}",
                            f"full -> {self.models['full']}",
                        ]
                    ),
                )
            return True
        if command == "queue":
            payload = await asyncio.to_thread(
                runtime.runtime_client.list_jobs,
                workload_type=None,
                status="pending,retryable,leased",
                limit=100,
            )
            items = [
                item
                for item in payload.get("items", [])
                if item.get("workloadType") in {"rss_publish", "transcribe_audio"}
            ]
            await self._reply(runtime, message, f"runtime_active={len(items)}")
            return True
        return False

    async def handle_message(self, runtime: Any, message: Any, *, shape: TelegramUpdateShape) -> bool:
        return False

    async def handle_callback(self, runtime: Any, event: Any, callback: Any) -> bool:
        return False

    def prepare_admission(
        self,
        runtime: Any,
        message: Any,
        *,
        shape: TelegramUpdateShape,
        resolution: RouteResolution,
        attachment_refs: tuple[dict[str, Any], ...],
    ) -> AdmissionInput:
        route = resolution.route
        if route is None or route.route_kind != "transcribe_audio_route":
            return AdmissionInput(text=(message.text or "").strip(), attachment_refs=attachment_refs)

        source_body = message.text or ""
        request_text = source_body.strip()
        classified = classify_text(request_text) if request_text else None
        if classified:
            input_kind, source_url = classified
            text = source_url
            input_text = source_url
            title_hint = None
            metadata = {"sourceUrlDisplay": stable_url_for_display(source_url)}
        elif attachment_refs:
            attachment = attachment_refs[0]
            input_kind = "telegram_audio"
            text = ""
            input_text = None
            title_hint = str(attachment.get("fileName") or f"audio_{shape.message_id}")
            metadata = {
                "fileName": title_hint,
                "fileSize": attachment.get("fileSize"),
                "mimeType": attachment.get("mimeType"),
            }
        elif shape.is_reply and request_text:
            input_kind = "telegram_text"
            text = source_body
            input_text = source_body
            title_hint = None
            metadata = {"textChars": len(source_body)}
        else:
            raise ValueError("Send audio or a supported media URL to start scribe.")

        source_key = telegram_source_key(shape)
        if shape.is_reply and shape.reply_to_message_id is not None:
            scribe = self.store.create_append_entry(
                bot_identity="rss-audio",
                chat_id=shape.chat_id,
                message_thread_id=shape.thread_id,
                message_id=shape.message_id,
                reply_to_message_id=shape.reply_to_message_id,
                source_key=source_key,
                input_kind=input_kind,
                input_text=input_text,
                source_metadata=metadata,
            )
            if scribe is None:
                raise ValueError("Reply to an existing scribe message or returned .md file to append.")
        else:
            scribe = self.store.create_start_entry(
                bot_identity="rss-audio",
                chat_id=shape.chat_id,
                message_thread_id=shape.thread_id,
                message_id=shape.message_id,
                sender_user_id=shape.sender_user_id,
                source_key=source_key,
                input_kind=input_kind,
                input_text=input_text,
                title_hint=title_hint,
                source_metadata=metadata,
            )
        scribe_json = scribe.to_job_json()
        scribe_json["inputKind"] = input_kind
        return AdmissionInput(
            text=text,
            attachment_refs=attachment_refs,
            scribe=scribe_json,
            transcribe_options={
                "model": self.models[self.model_key],
                "provider": "azure",
                "jobs": self.transcribe_jobs,
            },
        )

    def after_delivery(self, payload: dict[str, Any], telegram_message_id: int) -> None:
        link = payload.get("scribeLink")
        if not isinstance(link, dict):
            return
        self.store.link_delivery_message(
            bot_identity=str(link.get("botIdentity") or "rss-audio"),
            chat_id=int(payload["chatId"]),
            message_thread_id=payload.get("threadId"),
            telegram_message_id=telegram_message_id,
            thread_id=str(link["threadId"]),
            entry_id=str(link["entryId"]),
            revision_id=str(link["revisionId"]) if link.get("revisionId") else None,
            delivery_id=None,
            role=str(link.get("role") or "bot_markdown_revision"),
        )
