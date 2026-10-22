from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ResponseLike(Protocol):
    def read(self) -> bytes: ...


class Opener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> ResponseLike: ...


@dataclass(slots=True)
class BotApiError(RuntimeError):
    method: str
    error_code: int | None
    description: str
    parameters: dict[str, Any]

    def __str__(self) -> str:
        code = f" code={self.error_code}" if self.error_code is not None else ""
        return f"Telegram Bot API {self.method} failed{code}: {self.description}"

    @property
    def retry_after(self) -> int | None:
        value = self.parameters.get("retry_after")
        return value if type(value) is int and value > 0 else None


class BotApiClient:
    """Small JSON-only Bot API client.

    The token is kept out of logs and exception strings. Callers should avoid
    printing request URLs because Bot API tokens are path components.
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str | None = None,
        opener: Opener | None = None,
        timeout: float = 20.0,
    ) -> None:
        cleaned = token.strip()
        if not cleaned:
            raise ValueError("bot token is required")
        self._token = cleaned
        # The env override points every gateway send at a local Bot API (chain tests).
        self._base_url = (base_url or os.environ.get("TG_GATEWAY_BOT_API_BASE_URL") or "https://api.telegram.org").rstrip("/")
        self._opener = opener or urlopen
        self._timeout = timeout

    def request(self, method: str, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._base_url}/bot{self._token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._open(method, request)

    def request_multipart(
        self,
        method: str,
        fields: dict[str, Any],
        *,
        file_field: str,
        file_name: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> Any:
        boundary = f"tgtools-{secrets.token_hex(12)}"
        chunks: list[bytes] = []
        for key, value in fields.items():
            if value is None:
                continue
            rendered = json.dumps(value, separators=(",", ":"), ensure_ascii=False) if isinstance(value, (dict, list, bool)) else str(value)
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                    rendered.encode("utf-8"),
                    b"\r\n",
                ]
            )
        safe_name = file_name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = Request(
            f"{self._base_url}/bot{self._token}/{method}",
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        return self._open(method, request)

    def _open(self, method: str, request: Request) -> Any:
        try:
            response = self._opener(request, timeout=self._timeout)
            raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            raise self._error_from_body(method, raw, fallback_code=exc.code) from exc
        payload_response = json.loads(raw.decode("utf-8"))
        if not payload_response.get("ok"):
            raise self._error_from_payload(method, payload_response)
        return payload_response.get("result")

    @staticmethod
    def _error_from_body(method: str, body: bytes, *, fallback_code: int | None) -> BotApiError:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return BotApiError(method=method, error_code=fallback_code, description="HTTP request failed", parameters={})
        return BotApiClient._error_from_payload(method, payload, fallback_code=fallback_code)

    @staticmethod
    def _error_from_payload(
        method: str,
        payload: dict[str, Any],
        *,
        fallback_code: int | None = None,
    ) -> BotApiError:
        parameters = payload.get("parameters")
        return BotApiError(
            method=method,
            error_code=payload.get("error_code") or fallback_code,
            description=str(payload.get("description") or "request failed"),
            parameters=parameters if isinstance(parameters, dict) else {},
        )

    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
        disable_web_page_preview: bool = True,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": disable_web_page_preview},
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.request("sendMessage", payload)

    def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageText", payload)

    def send_document(
        self,
        *,
        chat_id: int,
        file_name: str,
        content: bytes,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id is not None:
            fields["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            fields["reply_parameters"] = {"message_id": reply_to_message_id}
        if caption is not None:
            fields["caption"] = caption
        if parse_mode is not None:
            fields["parse_mode"] = parse_mode
        if reply_markup is not None:
            fields["reply_markup"] = reply_markup
        return self.request_multipart(
            "sendDocument",
            fields,
            file_field="document",
            file_name=file_name,
            content=content,
        )

    def send_rich_message(
        self,
        *,
        chat_id: int,
        rich_message: dict[str, Any],
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        disable_notification: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "rich_message": rich_message}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        if disable_notification is not None:
            payload["disable_notification"] = disable_notification
        return self.request("sendRichMessage", payload)
