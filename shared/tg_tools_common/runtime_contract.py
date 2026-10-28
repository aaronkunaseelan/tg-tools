"""Wire shapes for the runtime controller: source receipts, jobs and deliveries.

Every tg-tools producer and delivery consumer builds payloads here, so the
contract lives in one place. Private text has exactly one home: a job input's
top-level ``sourceText`` (or ``sourceUrl``). Source receipts are metadata only.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from tg_tools_common.delivery_contract import CANONICAL_DELIVERY_KINDS

PRIVATE_TEXT_KEYS = frozenset({
    "sourceText", "sourceUrl", "promptText", "text", "body",
    "messageText", "messageBody", "rawMessage", "rawText",
})
SOURCE_KEYS = frozenset({"sourceText", "sourceUrl"})
# runtime-controller src/types/runtime.ts RESULT_KINDS (not attempt statuses).
TERMINAL_RESULT_KINDS = frozenset({"succeeded", "dead", "cancelled"})


def telegram_source_key(bot_identity: str, chat_id: int, message_id: int) -> str:
    """Idempotency key of one Telegram message: ``tg:<identity>:<chat>:<message>``."""
    return f"tg:{bot_identity}:{chat_id}:{message_id}"


def telegram_channel_id(bot_identity: str, chat_id: int, thread_id: int | None) -> str:
    """Channel of a receipt: ``<identity>:<chat>:<thread>``, thread ``0`` outside topics."""
    return f"{bot_identity}:{chat_id}:{thread_id or 0}"


def _keys(value: Any, *, top: bool = True) -> Iterator[tuple[str, bool]]:
    """Every mapping key in ``value``, with whether it sits at the top level."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), top
            yield from _keys(child, top=False)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _keys(child, top=False)


def source_json_violations(source_json: Mapping[str, Any]) -> set[str]:
    return {key for key, _ in _keys(source_json) if key in PRIVATE_TEXT_KEYS}


def job_input_violations(input_json: Mapping[str, Any]) -> set[str]:
    violations = {key for key, top in _keys(input_json)
                  if key in PRIVATE_TEXT_KEYS and not (top and key in SOURCE_KEYS)}
    text, url = input_json.get("sourceText"), input_json.get("sourceUrl")
    if "sourceText" in input_json and not (isinstance(text, str) and text):
        violations.add("sourceText")
    if "sourceUrl" in input_json and not (isinstance(url, str) and url.startswith(("http://", "https://"))):
        violations.add("sourceUrl")
    if "sourceText" in input_json and "sourceUrl" in input_json:
        violations.update(SOURCE_KEYS)
    return violations


def source_receipt_payload(
    *,
    source_system: str = "telegram",
    source_key: str,
    producer_system: str,
    workload_type: str,
    route_id: str,
    route_kind: str,
    channel_id: str,
    matched_rule_version: str,
    source_json: dict[str, Any],
) -> dict[str, Any]:
    if violations := source_json_violations(source_json):
        raise ValueError(f"source_json carries private text: {', '.join(sorted(violations))}")
    return {
        "sourceSystem": source_system,
        "sourceKey": source_key,
        "producerSystem": producer_system,
        "workloadType": workload_type,
        "routeId": route_id,
        "routeKind": route_kind,
        "channelId": channel_id,
        "matchedRuleVersion": matched_rule_version,
        "sourceJson": source_json,
    }


def submit_payload(
    *,
    producer_system: str,
    producer_ref: str,
    result_owner: str,
    result_route: str,
    workload_type: str,
    queue_class: str = "foreground",
    execution_profile: str = "remote_api",
    execution_shape: str = "local_bounded",
    priority: int = 70,
    input_item_count: int = 1,
    input_json: dict[str, Any],
    output_contract_json: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    effect_key: str | None = None,
    max_attempts: int = 1,
) -> dict[str, Any]:
    if violations := job_input_violations(input_json):
        raise ValueError(f"input_json carries misplaced private text: {', '.join(sorted(violations))}")
    payload: dict[str, Any] = {
        "producerSystem": producer_system,
        "producerRef": producer_ref,
        "resultOwner": result_owner,
        "resultRoute": result_route,
        "workloadType": workload_type,
        "queueClass": queue_class,
        "executionProfile": execution_profile,
        "executionShape": execution_shape,
        "priority": priority,
        "inputItemCount": input_item_count,
        "inputJson": input_json,
        "idempotencyKey": idempotency_key or producer_ref,
        "maxAttempts": max_attempts,
    }
    if effect_key is not None:
        payload["effectKey"] = effect_key
    if output_contract_json is not None:
        payload["outputContractJson"] = output_contract_json
    return payload


def delivery_key(*, owner_scope: str, dedupe_key: str) -> str:
    """Outbox idempotency key: ``<owner>:telegram-final:<dedupe>``, stable per intended delivery."""
    return f"{owner_scope}:telegram-final:{dedupe_key}"


def build_delivery_payload(
    *,
    result_owner: str,
    result_route: str,
    delivery_key_str: str,
    priority: int = 70,
    payload_json: dict[str, Any],
    max_attempts: int = 5,
) -> dict[str, Any]:
    """Outbox row; ``payload_json`` is provider-agnostic (chatId, threadId, text, ...)."""
    delivery_kind = payload_json.get("deliveryKind")
    if delivery_kind is None:
        if payload_json.get("rich") is True:
            delivery_kind = "telegram_rich_message"
        elif payload_json.get("replyToMessageId") is not None:
            delivery_kind = "telegram_reply_text"
        else:
            delivery_kind = "telegram_text"
    if not isinstance(delivery_kind, str):
        raise ValueError("payload deliveryKind must be a string")
    if delivery_kind not in CANONICAL_DELIVERY_KINDS and delivery_kind != "telegram_rich_message":
        raise ValueError(f"unsupported delivery kind: {delivery_kind}")
    return {
        "resultOwner": result_owner,
        "resultRoute": result_route,
        "deliveryProvider": "telegram",
        "deliveryKind": delivery_kind,
        "deliveryKey": delivery_key_str,
        "priority": priority,
        "payloadJson": payload_json,
        "maxAttempts": max_attempts,
    }
