from __future__ import annotations

from contextlib import ExitStack, nullcontext
from typing import ContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from pathlib import Path
from functools import partial
from uuid import uuid4

from .bot_api import BotApiError
from tg_tools_common.rich_render import RichCapability, render_rich_text


class TelegramSender(Protocol):
    def send_text(self, *, chat_id: int, text: str, thread_id: int | None, reply_to_message_id: int | None, parse_mode: str | None = None, reply_markup: dict[str, Any] | None = None, disable_web_page_preview: bool = True) -> int: ...
    def send_rich(self, *, chat_id: int, rich_message: dict[str, Any], thread_id: int | None, reply_to_message_id: int | None) -> int: ...
    def send_document(self, *, chat_id: int, file_name: str, content: bytes, thread_id: int | None, reply_to_message_id: int | None, caption: str | None, parse_mode: str | None = None, reply_markup: dict[str, Any] | None = None) -> int: ...
    def edit_text(self, *, chat_id: int, message_id: int, text: str, parse_mode: str | None = None, reply_markup: dict[str, Any] | None = None) -> None: ...


class GatewayDeliveryState(Protocol):
    def sent_message_id(self, delivery_key: str, *, delivery_id: str) -> int | None: ...
    def begin_dispatch(self, *, delivery_id: str, delivery_key: str, dispatch_token: str) -> bool: ...
    def clear_dispatch(self, *, delivery_id: str, delivery_key: str, dispatch_token: str) -> None: ...
    def remember_sent(
        self,
        *,
        delivery_id: str,
        delivery_key: str,
        chat_id: int,
        topic_id: int | None,
        telegram_message_id: int,
    ) -> None: ...
    def set_flood_wait(self, chat_id: int, retry_after_seconds: int) -> None: ...


@dataclass(frozen=True, slots=True)
class DeliveryRow:
    delivery_id: str
    delivery_key: str
    delivery_kind: str
    payload_json: dict[str, Any]


@dataclass(slots=True)
class MemoryGatewayState:
    sent_messages: dict[str, tuple[str, int]] = field(default_factory=dict)
    flood_state: dict[int, datetime] = field(default_factory=dict)
    dispatches: dict[str, str] = field(default_factory=dict)

    def sent_message_id(self, delivery_key: str, *, delivery_id: str) -> int | None:
        receipt = self.sent_messages.get(delivery_id)
        if receipt is not None and receipt[0] != delivery_key:
            raise ValueError("delivery receipt identity mismatch")
        return receipt[1] if receipt is not None else None

    def begin_dispatch(self, *, delivery_id: str, delivery_key: str, dispatch_token: str) -> bool:
        if delivery_id in self.dispatches:
            return False
        self.dispatches[delivery_id] = dispatch_token
        return True

    def clear_dispatch(self, *, delivery_id: str, delivery_key: str, dispatch_token: str) -> None:
        if self.dispatches.get(delivery_id) == dispatch_token:
            del self.dispatches[delivery_id]

    def remember_sent(
        self,
        *,
        delivery_id: str,
        delivery_key: str,
        chat_id: int,
        topic_id: int | None,
        telegram_message_id: int,
    ) -> None:
        self.sent_messages[delivery_id] = (delivery_key, telegram_message_id)

    def set_flood_wait(self, chat_id: int, retry_after_seconds: int) -> None:
        self.flood_state[chat_id] = datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    status: str
    telegram_message_id: int | None = None
    retry_after_seconds: int | None = None
    error_class: str | None = None


class DeliveryDrain:
    def __init__(
        self,
        *,
        sender: TelegramSender,
        state: GatewayDeliveryState,
        post_send: Callable[[dict[str, Any], int], None] | None = None,
        pre_dispatch: Callable[[dict[str, Any]], ContextManager[bool]] | None = None,
    ) -> None:
        self._sender = sender
        self._state = state
        self._post_send = post_send
        self._pre_dispatch = pre_dispatch

    def drain_row(self, row: DeliveryRow, *, capability: RichCapability = RichCapability(),
                  authorize: Callable[[], None] | None = None) -> DeliveryOutcome:
        existing = self._state.sent_message_id(row.delivery_key, delivery_id=row.delivery_id)
        if existing is not None:
            return self._replay_receipt(row, existing)
        # Validate and read/render files before recording dispatch. Each plan makes
        # exactly one provider call: a later chunk refusal cannot replay earlier chunks.
        send = self._prepare_send(row, capability=capability)
        outcome = self._dispatch(row, send, authorize)
        # Product lock is released before receipt projection (which may take the
        # same lock). Semantic checking and the provider operation are serialized.
        if outcome.status != "sent":
            return outcome
        if self._post_send is not None:
            try:
                self._post_send(row.payload_json, outcome.telegram_message_id)
            except Exception:
                return DeliveryOutcome(status="retryable", telegram_message_id=outcome.telegram_message_id,
                                       error_class="telegram_projection_failed")
        return outcome

    def _dispatch(self, row: DeliveryRow, send: Callable[[], int],
                  authorize: Callable[[], None] | None) -> DeliveryOutcome:
        token = str(uuid4())
        if not self._state.begin_dispatch(delivery_id=row.delivery_id,
                                          delivery_key=row.delivery_key, dispatch_token=token):
            existing = self._state.sent_message_id(row.delivery_key, delivery_id=row.delivery_id)
            if existing is not None:
                return self._replay_receipt(row, existing)
            return DeliveryOutcome(status="uncertain", error_class="telegram_delivery_uncertain")
        with ExitStack() as stack:
            try:
                guard = self._pre_dispatch(row.payload_json) if self._pre_dispatch else nullcontext(True)
                current = stack.enter_context(guard)
            except Exception:
                self._state.clear_dispatch(delivery_id=row.delivery_id, delivery_key=row.delivery_key, dispatch_token=token)
                raise
            if not current:
                self._state.clear_dispatch(delivery_id=row.delivery_id, delivery_key=row.delivery_key, dispatch_token=token)
                return DeliveryOutcome(status="superseded", error_class="product_delivery_superseded")
            return self._authorized_send(row, send, authorize, token)

    def _authorized_send(self, row: DeliveryRow, send: Callable[[], int],
                         authorize: Callable[[], None] | None, token: str) -> DeliveryOutcome:
        if authorize is not None:
            try:
                authorize()
            except Exception:
                # No provider call has occurred. Only this dispatcher may clear
                # its marker, and only before invoking the external operation.
                self._state.clear_dispatch(delivery_id=row.delivery_id, delivery_key=row.delivery_key, dispatch_token=token)
                raise
        try:
            message_id = send()
        except BotApiError as exc:
            # Only an explicit 4xx refusal proves the operation was rejected.
            # 5xx, timeouts, malformed success replies and process death leave intent.
            if type(exc.error_code) is int and 400 <= exc.error_code < 500:
                self._state.clear_dispatch(delivery_id=row.delivery_id, delivery_key=row.delivery_key, dispatch_token=token)
                if exc.error_code == 429 and exc.retry_after is not None:
                    self._state.set_flood_wait(_required_int(row.payload_json, "chatId"), exc.retry_after)
                    return DeliveryOutcome(status="retryable", retry_after_seconds=exc.retry_after,
                                           error_class="telegram_retry_after")
                return DeliveryOutcome(status="dead", error_class="telegram_api_error")
            return DeliveryOutcome(status="uncertain", error_class="telegram_delivery_uncertain")
        except Exception:
            return DeliveryOutcome(status="uncertain", error_class="telegram_delivery_uncertain")
        if type(message_id) is not int or message_id <= 0:
            return DeliveryOutcome(status="uncertain", error_class="telegram_delivery_uncertain")
        try:
            self._state.remember_sent(
                delivery_id=row.delivery_id, delivery_key=row.delivery_key,
                chat_id=_required_int(row.payload_json, "chatId"),
                topic_id=_int_payload(row.payload_json, "threadId"),
                telegram_message_id=message_id,
            )
        except Exception:
            return DeliveryOutcome(status="uncertain", telegram_message_id=message_id,
                                   error_class="telegram_delivery_uncertain")
        return DeliveryOutcome(status="sent", telegram_message_id=message_id)

    def _replay_receipt(self, row: DeliveryRow, message_id: int) -> DeliveryOutcome:
        if self._post_send is not None:
            try:
                self._post_send(row.payload_json, message_id)
            except Exception:
                return DeliveryOutcome(status="retryable", telegram_message_id=message_id,
                                       error_class="telegram_projection_failed")
        return DeliveryOutcome(status="already_sent", telegram_message_id=message_id)

    def _prepare_send(self, row: DeliveryRow, *, capability: RichCapability) -> Callable[[], int]:
        kind = row.delivery_kind
        payload = dict(row.payload_json)
        embedded_kind = payload.get("deliveryKind")
        if embedded_kind is not None and embedded_kind != kind:
            raise ValueError("delivery kind does not match payload")
        chat_id = _required_int(payload, "chatId")
        thread_id = _int_payload(payload, "threadId")
        reply_to = _int_payload(payload, "replyToMessageId")
        if kind in {"telegram_text", "telegram_reply_text"}:
            return partial(self._sender.send_text,
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to,
                text=str(payload.get("text") or ""),
                parse_mode=payload.get("parseMode") if isinstance(payload.get("parseMode"), str) else None,
                reply_markup=payload.get("replyMarkup") if isinstance(payload.get("replyMarkup"), dict) else None,
                disable_web_page_preview=bool(payload.get("disableWebPagePreview", True)),
            )
        if kind == "telegram_rich_message":
            rendered = render_rich_text(str(payload.get("text") or ""), capability)
            if rendered.delivery_kind == "telegram_rich_message" and rendered.rich_message is not None:
                return partial(self._sender.send_rich,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to_message_id=reply_to,
                    rich_message=rendered.rich_message,
                )
            if rendered.delivery_kind == "telegram_document":
                return partial(self._sender.send_document,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    file_name=str(payload.get("fileName") or "pi-response.md"),
                    content=(rendered.document_text or "").encode("utf-8"),
                    reply_to_message_id=reply_to,
                    caption="Pi response",
                )
            if len(rendered.html_chunks) != 1:
                return partial(self._sender.send_document,
                    chat_id=chat_id, thread_id=thread_id, reply_to_message_id=reply_to,
                    file_name=str(payload.get("fileName") or "pi-response.md"),
                    content=str(payload.get("text") or "").encode("utf-8"), caption="Pi response")
            return partial(self._sender.send_text, chat_id=chat_id, thread_id=thread_id,
                           reply_to_message_id=reply_to, text=rendered.html_chunks[0])
        if kind in {"telegram_document", "telegram_reply_document"}:
            content = payload.get("content")
            if isinstance(content, str):
                raw = content.encode("utf-8")
            elif isinstance(content, bytes):
                raw = content
            elif isinstance(payload.get("documentPath"), str):
                raw = Path(str(payload["documentPath"])).read_bytes()
            else:
                raw = b""
            return partial(self._sender.send_document,
                chat_id=chat_id,
                thread_id=thread_id,
                file_name=str(payload.get("fileName") or payload.get("filename") or "document.txt"),
                content=raw,
                reply_to_message_id=reply_to,
                caption=payload.get("caption") if isinstance(payload.get("caption"), str) else None,
                parse_mode=payload.get("parseMode") if isinstance(payload.get("parseMode"), str) else None,
                reply_markup=payload.get("replyMarkup") if isinstance(payload.get("replyMarkup"), dict) else None,
            )
        if kind == "telegram_edit":
            message_id = _required_int(payload, "messageId")
            edit = partial(self._sender.edit_text,
                chat_id=chat_id,
                message_id=message_id,
                text=str(payload.get("text") or ""),
                parse_mode=payload.get("parseMode") if isinstance(payload.get("parseMode"), str) else None,
                reply_markup=payload.get("replyMarkup") if isinstance(payload.get("replyMarkup"), dict) else None,
            )
            def send_edit() -> int:
                edit()
                return message_id
            return send_edit
        raise ValueError(f"unsupported delivery kind: {kind}")


class RuntimeDeliveryClient(Protocol):
    def claim_delivery(
        self,
        *,
        claim_owner: str,
        result_owner: str | None = None,
        lease_duration_sec: int = 120,
    ) -> dict[str, Any] | None: ...
    def heartbeat_delivery(self, *, delivery_id: str, claim_token: str, lease_duration_sec: int = 120) -> dict[str, Any]: ...
    def finalize_delivery(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class RuntimeDeliveryWorker:
    def __init__(
        self,
        *,
        client: RuntimeDeliveryClient,
        drain: DeliveryDrain,
        claim_owner: str = "tg-gateway-delivery",
        result_owner: str | None = None,
        result_owners: tuple[str, ...] | None = None,
        lease_duration_sec: int = 120,
    ) -> None:
        self._client = client
        self._drain = drain
        self._claim_owner = claim_owner
        # Each gateway instance must only claim deliveries for identities it
        # can actually send as. Claiming with owner=None grabs other
        # instances' deliveries and kills them with the wrong bot identity,
        # so owner filtering fails closed: an explicit non-empty owner set
        # is required.
        owners: tuple[str, ...] = tuple(
            o for o in (result_owners or (result_owner,)) if isinstance(o, str) and o.strip()
        )
        if not owners:
            raise ValueError(
                "RuntimeDeliveryWorker requires explicit result_owners; "
                "claiming with result_owner=None is not allowed")
        self._result_owners: tuple[str, ...] = owners
        self._lease_duration_sec = lease_duration_sec

    def drain_once(self, *, capability: RichCapability = RichCapability()) -> DeliveryOutcome:
        delivery: dict[str, Any] | None = None
        for owner in self._result_owners:
            claimed = self._client.claim_delivery(
                claim_owner=self._claim_owner,
                result_owner=owner,
                lease_duration_sec=self._lease_duration_sec,
            )
            candidate = claimed.get("delivery") if isinstance(claimed, dict) else None
            if isinstance(candidate, dict):
                claimed_owner = candidate.get("resultOwner")
                if claimed_owner is not None and str(claimed_owner) not in self._result_owners:
                    # Never act on a row for an identity we did not request.
                    continue
                delivery = candidate
                break
        if not isinstance(delivery, dict):
            return DeliveryOutcome(status="idle")
        payload_json = dict(delivery.get("payloadJson") or {})
        delivery_kind = str(delivery.get("deliveryKind") or payload_json.get("deliveryKind") or "")
        row = DeliveryRow(
            delivery_id=str(delivery["deliveryId"]),
            delivery_key=str(delivery["deliveryKey"]),
            delivery_kind=delivery_kind,
            payload_json=payload_json,
        )
        try:
            def authorize() -> None:
                renewed = self._client.heartbeat_delivery(
                    delivery_id=delivery["deliveryId"], claim_token=delivery["claimToken"],
                    lease_duration_sec=self._lease_duration_sec)
                current = renewed.get("delivery") if isinstance(renewed, dict) else None
                if (not isinstance(current, dict)
                        or current.get("deliveryId") != delivery["deliveryId"]
                        or current.get("claimToken") != delivery["claimToken"]
                        or current.get("status") != "claimed"):
                    raise ValueError("invalid delivery heartbeat authority")
            outcome = self._drain.drain_row(row, capability=capability, authorize=authorize)
        except ValueError as exc:
            # Payload contract violations are deterministic; retrying cannot help.
            outcome = DeliveryOutcome(status="dead", error_class=exc.__class__.__name__)
        except Exception as exc:
            # Dispatch exceptions are contained by the drain. Remaining failures
            # precede dispatch or replay a durable receipt, so retry is safe.
            outcome = DeliveryOutcome(status="retryable", error_class=exc.__class__.__name__)
        self._client.finalize_delivery(_finalize_payload(delivery, outcome))
        return outcome


def _finalize_payload(delivery: dict[str, Any], outcome: DeliveryOutcome) -> dict[str, Any]:
    base: dict[str, Any] = {
        "deliveryId": delivery["deliveryId"],
        "claimToken": delivery["claimToken"],
    }
    if outcome.status in {"sent", "already_sent"}:
        base.update(
            {
                "outcome": "sent",
                "providerReceiptJson": {"telegramMessageId": outcome.telegram_message_id},
            }
        )
        return base
    if outcome.status == "retryable":
        base.update(
            {
                "outcome": "retryable_error",
                "errorClass": outcome.error_class or "telegram_retryable",
                "errorText": "Telegram delivery retry requested",
                "backoffSec": outcome.retry_after_seconds or 60,
            }
        )
        return base
    base.update(
        {
            "outcome": "terminal_error",
            "errorClass": outcome.error_class or "telegram_delivery_failed",
            "errorText": ("Telegram send outcome is uncertain; reconcile dispatch before retry"
                          if outcome.status == "uncertain" else
                          "Product delivery superseded before provider dispatch" if outcome.status == "superseded" else
                          "Telegram delivery failed"),
        }
    )
    if outcome.telegram_message_id is not None:
        base["providerReceiptJson"] = {"telegramMessageId": outcome.telegram_message_id}
    return base


def _int_payload(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    return int(value)


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = _int_payload(payload, key)
    if value is None:
        raise ValueError(f"missing required payload key: {key}")
    return value
