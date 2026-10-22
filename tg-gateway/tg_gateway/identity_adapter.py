"""Product adapters: the per-identity behaviour a gateway delegates to."""

from __future__ import annotations

import importlib
from typing import Any, Protocol

from tg_tools_common.telegram_routes import RouteResolution, TelegramUpdateShape

from .admission import AdmissionInput

ADAPTER_MODULES = {
    "rss-audio": "rss_gateway_adapter",
    "meeting-summary": "meeting_gateway_adapter",
}


class IdentityAdapter(Protocol):
    async def handle_command(self, runtime: Any, message: Any, *, command: str, arg: str) -> bool: ...

    async def handle_message(self, runtime: Any, message: Any, *, shape: TelegramUpdateShape) -> bool: ...

    async def handle_callback(self, runtime: Any, event: Any, callback: Any) -> bool: ...

    def prepare_admission(
        self,
        runtime: Any,
        message: Any,
        *,
        shape: TelegramUpdateShape,
        resolution: RouteResolution,
        attachment_refs: tuple[dict[str, Any], ...],
    ) -> AdmissionInput: ...

    def after_delivery(self, payload: dict[str, Any], telegram_message_id: int) -> None: ...


def load_identity_adapter(bot_identity: str) -> IdentityAdapter:
    try:
        module = importlib.import_module(ADAPTER_MODULES[bot_identity])
    except KeyError:
        raise RuntimeError(f"no product adapter for bot identity {bot_identity!r}") from None
    return module.GatewayAdapter()
