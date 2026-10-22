from __future__ import annotations

from .models import InboundCallback, InboundMessage
from .settings import GatewaySettings


def is_allowed_scope(
    settings: GatewaySettings,
    *,
    chat_id: int,
    thread_id: int | None,
    user_id: int | None,
) -> bool:
    if settings.allowed_chat_id is not None and chat_id != settings.allowed_chat_id:
        return False
    if settings.allowed_thread_id is not None and thread_id != settings.allowed_thread_id:
        return False
    if settings.allowed_user_ids:
        if user_id is None or user_id not in settings.allowed_user_ids:
            return False
    return True


def is_allowed_message(settings: GatewaySettings, message: InboundMessage) -> bool:
    return is_allowed_scope(
        settings,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        user_id=message.user_id,
    )


def is_allowed_callback(settings: GatewaySettings, callback: InboundCallback) -> bool:
    return is_allowed_scope(
        settings,
        chat_id=callback.chat_id,
        thread_id=callback.thread_id,
        user_id=callback.user_id,
    )
