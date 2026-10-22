"""Typed intents over :mod:`tg_tools_common.runtime_contract` payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from tg_tools_common.runtime_contract import (
    source_receipt_payload,
    submit_payload,
    telegram_channel_id,
    telegram_source_key,
)


@dataclass(frozen=True)
class TelegramSourceIdentity:
    bot_identity: str
    chat_id: int
    message_id: int
    thread_id: int | None = None
    reply_to_message_id: int | None = None

    @property
    def source_key(self) -> str:
        return telegram_source_key(self.bot_identity, self.chat_id, self.message_id)

    @property
    def channel_id(self) -> str:
        return telegram_channel_id(self.bot_identity, self.chat_id, self.thread_id)

    def metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "botIdentity": self.bot_identity,
            "chatId": self.chat_id,
            "messageId": self.message_id,
        }
        if self.thread_id is not None:
            payload["threadId"] = self.thread_id
        if self.reply_to_message_id is not None:
            payload["replyToMessageId"] = self.reply_to_message_id
        return payload

    def to_source_receipt_payload(
        self,
        *,
        producer_system: str,
        workload_type: str,
        route_id: str,
        route_kind: str,
        matched_rule_version: str,
        source_json: Mapping[str, Any] | None = None,
        source_system: str = "telegram",
    ) -> dict[str, Any]:
        return source_receipt_payload(
            source_system=source_system,
            source_key=self.source_key,
            producer_system=producer_system,
            workload_type=workload_type,
            route_id=route_id,
            route_kind=route_kind,
            channel_id=self.channel_id,
            matched_rule_version=matched_rule_version,
            source_json={"telegram": self.metadata(), **dict(source_json or {})},
        )


@dataclass(frozen=True)
class RuntimeJobIntent:
    producer_system: str
    result_owner: str
    result_route: str
    workload_type: str
    input_json: Mapping[str, Any]
    producer_ref: str
    queue_class: str = "foreground"
    execution_profile: str = "remote_api"
    execution_shape: str = "local_bounded"
    priority: int = 70
    input_item_count: int = 1
    output_contract_json: Mapping[str, Any] | None = None
    idempotency_key: str | None = None
    effect_key: str | None = None
    max_attempts: int = 1

    def to_submit_payload(self) -> dict[str, Any]:
        return submit_payload(
            producer_system=self.producer_system,
            producer_ref=self.producer_ref,
            result_owner=self.result_owner,
            result_route=self.result_route,
            workload_type=self.workload_type,
            queue_class=self.queue_class,
            execution_profile=self.execution_profile,
            execution_shape=self.execution_shape,
            priority=self.priority,
            input_item_count=self.input_item_count,
            input_json=dict(self.input_json),
            output_contract_json=dict(self.output_contract_json) if self.output_contract_json is not None else None,
            idempotency_key=self.idempotency_key,
            effect_key=self.effect_key,
            max_attempts=self.max_attempts,
        )
