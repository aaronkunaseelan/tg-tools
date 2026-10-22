"""Telegram gateway for one bot identity.

Inbound, an allowed message addressed to this bot is either handled by the
identity's product adapter (commands, replies, callbacks) or becomes a durable
source handoff that stages its media and submits the routed runtime job.
Outbound, the gateway drains runtime deliveries for the result owners of its
own routes, and projects job progress into Telegram status messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tg_tools_common.rich_render import RichCapability
from tg_tools_common.telegram_manifest import build_registry_from_manifests, manifests_for_identity
from tg_tools_common.telegram_routes import RouteResolution, TelegramRouteRegistry, TelegramUpdateShape, normalize_command

from .access import is_allowed_callback, is_allowed_message
from .admission import build_route_admission
from .bot_api import BotApiClient
from .delivery import DeliveryDrain, RuntimeDeliveryWorker
from .identity_adapter import IdentityAdapter, load_identity_adapter
from .job_progress import ProgressProjector, ProgressStore
from .models import OutboundMessage
from .pg_state import PostgresGatewayState, PostgresHandledUpdateStore
from .runtime_client import RuntimeClient
from .settings import GatewaySettings, load_settings
from .source_handoff import PostgresSourceHandoffStore, SourceHandoffDrainer, build_intent
from .telethon_gateway import TelethonGateway

logger = logging.getLogger(__name__)

DELIVERY_CAPABILITY = RichCapability(final_rich_supported=True, draft_rich_supported=False)


@dataclass(slots=True)
class GatewayRuntime:
    settings: GatewaySettings
    gateway: TelethonGateway
    runtime_client: RuntimeClient
    registry: TelegramRouteRegistry
    adapter: IdentityAdapter
    handled_updates: PostgresHandledUpdateStore
    source_handoffs: PostgresSourceHandoffStore
    handoff_drainer: SourceHandoffDrainer
    deliveries: RuntimeDeliveryWorker
    progress: ProgressProjector | None


class BotApiSender:
    """The delivery drain's sender, backed by the Bot API."""

    def __init__(self, client: BotApiClient) -> None:
        self._client = client

    def send_text(self, *, chat_id: int, text: str, thread_id: int | None, reply_to_message_id: int | None,
                  parse_mode: str | None = None, reply_markup: dict[str, Any] | None = None,
                  disable_web_page_preview: bool = True) -> int:
        result = self._client.send_message(
            chat_id=chat_id, message_thread_id=thread_id, reply_to_message_id=reply_to_message_id, text=text,
            parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview)
        return int(result.get("message_id") or 0)

    def send_rich(self, *, chat_id: int, rich_message: dict[str, Any], thread_id: int | None,
                  reply_to_message_id: int | None) -> int:
        result = self._client.send_rich_message(
            chat_id=chat_id, message_thread_id=thread_id, reply_to_message_id=reply_to_message_id,
            rich_message=rich_message)
        return int(result.get("message_id") or 0)

    def send_document(self, *, chat_id: int, file_name: str, content: bytes, thread_id: int | None,
                      reply_to_message_id: int | None, caption: str | None, parse_mode: str | None = None,
                      reply_markup: dict[str, Any] | None = None) -> int:
        result = self._client.send_document(
            chat_id=chat_id, message_thread_id=thread_id, reply_to_message_id=reply_to_message_id,
            file_name=file_name, content=content, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
        return int(result.get("message_id") or 0)

    def edit_text(self, *, chat_id: int, message_id: int, text: str, parse_mode: str | None = None,
                  reply_markup: dict[str, Any] | None = None) -> None:
        self._client.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)


def delivery_owners(registry: TelegramRouteRegistry) -> tuple[str, ...]:
    """Result owners this gateway delivers for: exactly the owners of its own routes."""
    owners = tuple(dict.fromkeys(route.owner_system for route in registry.routes))
    if not owners:
        raise RuntimeError("no routes are bound for this bot identity; refusing to claim deliveries")
    return owners


def build_gateway_runtime(settings: GatewaySettings | None = None) -> GatewayRuntime:
    """Wire the gateway; call from the event loop that will run it."""
    settings = settings or load_settings()
    loop = asyncio.get_running_loop()
    runtime_client = RuntimeClient(base_url=settings.runtime_controller_url,
                                   bearer_token=settings.runtime_controller_token)
    registry = build_registry_from_manifests(manifests_for_identity(settings.bot_identity),
                                             bindings=settings.route_bindings)
    adapter = load_identity_adapter(settings.bot_identity)
    gateway = TelethonGateway(session_path=settings.bot_session_path, api_id=settings.telegram_api_id,
                              api_hash=settings.telegram_api_hash, bot_token=settings.bot_token)

    def download(file_id: str, target_path: Path) -> str:
        # The handoff drainer runs in a worker thread; Telethon lives on the loop.
        future = asyncio.run_coroutine_threadsafe(gateway.download_file(file_id, target_path=target_path), loop)
        return str(future.result(timeout=600).path)

    progress_store = None
    if settings.progress_workloads:
        progress_store = ProgressStore(dsn=settings.database_url, bot_identity=settings.bot_identity)
        progress_store.validate_schema()
        progress_store.client = runtime_client

    @contextlib.contextmanager
    def pre_dispatch(payload: dict[str, Any]):
        with contextlib.ExitStack() as stack:
            if progress_store and not stack.enter_context(progress_store.pre_dispatch(payload)):
                yield False
                return
            product_guard = getattr(adapter, "pre_dispatch", None)
            yield stack.enter_context(product_guard(payload)) if product_guard and not payload.get("progressBinding") else True

    def after_delivery(payload: dict[str, Any], message_id: int) -> None:
        if payload.get("progressBinding") and progress_store:
            progress_store.after_delivery(payload, message_id)
        else:
            adapter.after_delivery(payload, message_id)

    source_handoffs = PostgresSourceHandoffStore(dsn=settings.database_url)
    drain = DeliveryDrain(sender=BotApiSender(BotApiClient(token=settings.bot_token)),
                          state=PostgresGatewayState(dsn=settings.database_url),
                          post_send=after_delivery, pre_dispatch=pre_dispatch)
    return GatewayRuntime(
        settings=settings,
        gateway=gateway,
        runtime_client=runtime_client,
        registry=registry,
        adapter=adapter,
        handled_updates=PostgresHandledUpdateStore(dsn=settings.database_url),
        source_handoffs=source_handoffs,
        handoff_drainer=SourceHandoffDrainer(store=source_handoffs, runtime=runtime_client,
                                             staging_root=settings.staging_dir, downloader=download,
                                             bot_identity=settings.bot_identity),
        deliveries=RuntimeDeliveryWorker(client=runtime_client, drain=drain,
                                         result_owners=delivery_owners(registry)),
        progress=ProgressProjector(store=progress_store, client=runtime_client) if progress_store else None,
    )


def _shape(runtime: GatewayRuntime, message: Any) -> TelegramUpdateShape:
    reply_to = getattr(message, "reply_to_message_id", None)
    return TelegramUpdateShape(
        bot_identity=runtime.settings.bot_identity,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        message_id=message.message_id,
        reply_to_message_id=reply_to,
        sender_user_id=message.user_id,
        text=message.text or "",
        command=normalize_command(message.text or ""),
        media_kinds=frozenset(attachment.kind for attachment in message.attachments),
        is_reply=reply_to is not None and reply_to != message.thread_id,
    )


def _command_target(text: str | None) -> str | None:
    """The @bot a command is addressed to, if any."""
    first = (text or "").strip().split(maxsplit=1)[:1]
    if not first or not first[0].startswith("/") or "@" not in first[0]:
        return None
    return first[0].split("@", 1)[1].strip().lower() or None


async def _reply(runtime: GatewayRuntime, message: Any, text: str) -> None:
    await runtime.gateway.send_text(OutboundMessage(
        chat_id=message.chat_id, thread_id=message.thread_id, reply_to_message_id=message.message_id, text=text))


async def handle_message(runtime: GatewayRuntime, event: Any) -> None:
    message = runtime.gateway.normalize_message(event.message)
    if not is_allowed_message(runtime.settings, message):
        logger.info("dropping unauthorized message chat=%s topic=%s user=%s",
                    message.chat_id, message.thread_id, message.user_id)
        return
    target = _command_target(message.text)
    if target is not None and target != runtime.settings.bot_username:
        return
    shape = _shape(runtime, message)
    if not runtime.registry.scoped_route_ids(shape):
        return
    if shape.command is not None:
        update_uid = f"message:{shape.bot_identity}:{message.chat_id}:{message.message_id}"
        if not await asyncio.to_thread(runtime.handled_updates.claim_update, update_uid):
            logger.info("skipping already handled update %s", update_uid)
            return
        parts = (message.text or "").strip().split(maxsplit=1)
        handled = await runtime.adapter.handle_command(
            runtime, message, command=shape.command, arg=parts[1] if len(parts) > 1 else "")
        if not handled:
            await _reply(runtime, message, f"Unsupported command: /{shape.command}")
        return
    if await runtime.adapter.handle_message(runtime, message, shape=shape):
        return
    resolution = runtime.registry.resolve(shape)
    if resolution.matched and resolution.route is not None:
        await _hand_off(runtime, message, shape, resolution)
    else:
        logger.info("no route for update chat=%s topic=%s message=%s status=%s",
                    message.chat_id, message.thread_id, message.message_id, resolution.status)


async def _hand_off(runtime: GatewayRuntime, message: Any, shape: TelegramUpdateShape,
                    resolution: RouteResolution) -> None:
    """Freeze the admission in a durable handoff, then drain it once inline."""
    route = resolution.route
    attachment = None
    if message.attachments:
        first = message.attachments[0]
        attachment = {"kind": first.kind, "fileId": first.file_id, "sourceFileId": first.file_id,
                      "fileName": first.file_name, "mimeType": first.mime_type, "fileSize": first.file_size,
                      "displayName": first.display_name}
    try:
        admission_input = runtime.adapter.prepare_admission(
            runtime, message, shape=shape, resolution=resolution,
            attachment_refs=(attachment,) if attachment else ())
    except ValueError as exc:
        await _reply(runtime, message, str(exc))
        return
    admission = build_route_admission(shape=shape, resolution=resolution, admission_input=admission_input)
    with_progress = (runtime.progress is not None
                     and admission.job_submit_payload.get("workloadType") in runtime.settings.progress_workloads)
    update_uid = f"message:{shape.bot_identity}:{message.chat_id}:{message.message_id}"
    created = await asyncio.to_thread(runtime.source_handoffs.create_handoff, {
        "update_uid": update_uid,
        "source_key": admission.source_receipt_payload["sourceKey"],
        "bot_identity": shape.bot_identity,
        "route_id": route.route_id,
        "route_kind": route.route_kind,
        "rule_version": route.matched_rule_version or "v1",
        "intent_json": build_intent(source_receipt_payload=admission.source_receipt_payload,
                                    job_submit_payload=admission.job_submit_payload, attachment=attachment),
        "progress_target": {"chatId": message.chat_id, "threadId": message.thread_id,
                            "replyToMessageId": message.message_id} if with_progress else None,
    })
    if not created:
        logger.info("skipping already handed-off update %s", update_uid)
        return
    if not with_progress:
        await _reply(runtime, message, "Received — queued.")
    outcome = await asyncio.to_thread(runtime.handoff_drainer.drain_once)
    logger.info("source handoff update=%s drain_outcome=%s", update_uid, outcome)


async def handle_callback(runtime: GatewayRuntime, event: Any) -> None:
    callback = await runtime.gateway.normalize_callback(event)
    if not is_allowed_callback(runtime.settings, callback):
        logger.info("dropping unauthorized callback chat=%s topic=%s user=%s",
                    callback.chat_id, callback.thread_id, callback.user_id)
        return
    await runtime.adapter.handle_callback(runtime, event, callback)


async def _poll(stop: asyncio.Event, name: str, step: Callable[[], bool], idle_seconds: float) -> None:
    """Run ``step`` in a thread until stopped; sleep when it reports no work."""
    while not stop.is_set():
        try:
            busy = await asyncio.to_thread(step)
        except Exception:
            logger.exception("%s failed", name)
            busy = False
        if not busy:
            await asyncio.sleep(idle_seconds)


def _project_progress(runtime: GatewayRuntime) -> bool:
    runtime.progress.project_once()
    return False


def _drain_deliveries(runtime: GatewayRuntime) -> bool:
    outcome = runtime.deliveries.drain_once(capability=DELIVERY_CAPABILITY)
    if outcome.status != "idle":
        logger.info("delivery drain outcome=%s message=%s", outcome.status, outcome.telegram_message_id)
    return outcome.status != "idle"


async def run() -> None:
    runtime = build_gateway_runtime()
    await runtime.gateway.connect()

    async def on_message(event: Any) -> None:
        try:
            await handle_message(runtime, event)
        except Exception:
            logger.exception("message handler failed")

    async def on_callback(event: Any) -> None:
        try:
            await handle_callback(runtime, event)
        except Exception:
            logger.exception("callback handler failed")
        finally:
            await event.answer()

    runtime.gateway.register_handlers(on_message=on_message, on_callback=on_callback)
    stop = asyncio.Event()
    loops = [
        _poll(stop, "source handoff drain", lambda: runtime.handoff_drainer.drain_once() != "idle", 10),
        _poll(stop, "delivery drain", lambda: _drain_deliveries(runtime), 0.5),
    ]
    if runtime.progress is not None:
        loops.append(_poll(stop, "progress projection", lambda: _project_progress(runtime), 1))
    tasks = [asyncio.create_task(loop) for loop in loops]
    logger.info("tg-gateway started identity=%s", runtime.settings.bot_identity)
    try:
        await runtime.gateway.run_until_disconnected()
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.gateway.disconnect()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
