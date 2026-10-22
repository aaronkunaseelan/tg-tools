"""
MTProto file downloader using Telethon.

Bypasses the 20 MB Bot API getFile limit.
Downloads files up to 2 GB (4 GB for Premium) via Telegram's native protocol.

Used as a fallback when Bot API getFile would fail due to size.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from pathlib import Path

from telethon import TelegramClient

logger = logging.getLogger(__name__)

# Session file path (persists auth so we don't re-auth every restart)
SESSION_PATH = Path(os.path.expanduser("~/.local/share/telegram-mtproto/rss-audio-bot"))

# Threshold: use MTProto for files above this size
MTPROTO_THRESHOLD = 19 * 1024 * 1024  # 19 MB (safe margin under 20 MB API limit)

# Both retained worker processes use this session. Never unlink the sidecar:
# replacing its inode would let another process acquire a different lock.
SESSION_LOCK_TIMEOUT = 60.0
SESSION_LOCK_POLL_INTERVAL = 0.1
_client: TelegramClient | None = None
_session_lock_fd: int | None = None
_client_lock = asyncio.Lock()


def _mtproto_credentials() -> tuple[int, str, str]:
    try:
        api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    except ValueError:
        api_id = 0
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    bot_token = os.environ.get("RSS_AUDIO_BOT_TOKEN", "").strip()
    return api_id, api_hash, bot_token



def ensure_mtproto_ready() -> None:
    api_id, api_hash, bot_token = _mtproto_credentials()
    missing: list[str] = []
    if api_id <= 0:
        missing.append("TELEGRAM_API_ID")
    if not api_hash:
        missing.append("TELEGRAM_API_HASH")
    if not bot_token:
        missing.append("RSS_AUDIO_BOT_TOKEN")
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    if missing:
        raise RuntimeError("Missing MTProto settings: " + ", ".join(missing))


async def _acquire_session_lock() -> int:
    """Poll a nonblocking OS lock; cancellation never leaves a waiter thread."""
    fd = os.open(str(SESSION_PATH) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    deadline = asyncio.get_running_loop().time() + SESSION_LOCK_TIMEOUT
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for MTProto session ownership")
                await asyncio.sleep(min(SESSION_LOCK_POLL_INTERVAL, remaining))
    except BaseException:
        os.close(fd)
        raise


async def _disconnect_owned_client() -> None:
    """Keep ownership until Telethon has stopped tasks and closed SQLite.

    Telethon 1.45 shields its disconnect task itself. Merely cancelling the
    awaiting caller would otherwise release flock while that task still writes.
    On disconnect failure keep both client and fd, fail closed, and allow an
    explicit disconnect() retry. Process exit also releases the OS lock.
    """
    global _client, _session_lock_fd
    cancelled = False
    if _client is not None:
        task = asyncio.ensure_future(_client.disconnect())
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done():
                    # A cancelled disconnect did not prove SQLite was closed.
                    raise
                cancelled = True
        _client = None
    if _session_lock_fd is not None:
        os.close(_session_lock_fd)
        _session_lock_fd = None
    if cancelled:
        raise asyncio.CancelledError


async def disconnect() -> None:
    """Wait for an active download's cleanup; retry failed disconnect if needed."""
    async with _client_lock:
        await _disconnect_owned_client()


def needs_mtproto(file_size: int | None) -> bool:
    """Check if the file is too large for Bot API getFile."""
    if file_size is None:
        return False  # Unknown size — try Bot API first
    return file_size > MTPROTO_THRESHOLD


async def download_file(
    chat_id: int,
    message_id: int,
    dest_path: str,
    progress_callback=None,
) -> str:
    """
    Download a file via MTProto given the chat and message IDs.

    Returns the local file path.
    """
    global _client, _session_lock_fd
    # Serialize whole lifecycles, not just initialization. A fresh client per
    # download gives the other long-running worker a chance between operations.
    await asyncio.wait_for(_client_lock.acquire(), timeout=SESSION_LOCK_TIMEOUT)
    try:
        if _client is not None or _session_lock_fd is not None:
            raise RuntimeError("Previous MTProto disconnect failed; call disconnect() or restart worker")
        ensure_mtproto_ready()
        _session_lock_fd = await _acquire_session_lock()
        try:
            api_id, api_hash, bot_token = _mtproto_credentials()
            _client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
            await _client.start(bot_token=bot_token)
            message = await _client.get_messages(chat_id, ids=message_id)
            if message is None or message.media is None:
                raise ValueError(f"No media found in message {message_id} of chat {chat_id}")
            path = await _client.download_media(
                message, file=dest_path, progress_callback=progress_callback,
            )
            if path is None:
                raise ValueError("Telethon download_media returned None")
            logger.info("MTProto download complete: %s (%s bytes)", path, os.path.getsize(path))
            return str(path)
        finally:
            await _disconnect_owned_client()
    finally:
        _client_lock.release()
