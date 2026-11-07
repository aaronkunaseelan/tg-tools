"""The episodes pipeline: a Telegram file or link in the publish topic becomes an episode of the `audio` feed.

Chain test: ``tests/test_chain_rss_publish.py``. The publish job ends with the episode live (mp3, script and
segments) and one Telegram reply; the notes job it submits (``notes_job.py``) adds the notes and the page.
"""
from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path
from typing import Any

from download_names import _safe_download_name
from mtproto_dl import download_file as mtproto_download
from mtproto_dl import ensure_mtproto_ready, needs_mtproto
from pipeline import (JobOptions, PipelineError, RuntimeExecutionResult, execution_result, ingest_local_audio,
                      publish_link, source_link)
from runtime_jobs import DeliveryTargetMissing, message_reference


async def run(settings, bot, input_json: dict[str, Any], options: JobOptions,
              work_dir: Path | None) -> RuntimeExecutionResult:
    link = source_link(input_json)
    if link is not None:
        return await asyncio.to_thread(publish_link, settings, input_json, link, work_dir, options)
    async with telegram_file(bot, input_json, work_dir) as audio:
        published = await asyncio.to_thread(
            ingest_local_audio, settings, audio, str(input_json.get("titleHint") or audio.stem),
            work_dir=work_dir, options=options)
    return execution_result(published, None, options.feed)


@contextlib.asynccontextmanager
async def telegram_file(bot, input_json: dict[str, Any], work_dir: Path | None):
    """The message's audio: the gateway's staged copy, else a download from Telegram."""
    local = input_json.get("localSourcePath")
    if isinstance(local, str) and Path(local).is_file():
        yield Path(local)
        return
    file_id = input_json.get("telegramFileId")
    if not (isinstance(file_id, str) and file_id):
        raise PipelineError("input", "Unsupported runtime RSS input")
    with _download_dir(work_dir) as directory:
        target = directory / _safe_download_name(str(input_json.get("telegramFileName") or "telegram-audio"))
        file_size = input_json.get("fileSize")
        if needs_mtproto(file_size if isinstance(file_size, int) else None):
            await _mtproto_download(input_json, target)
        else:
            file_obj = await bot.get_file(file_id)
            await file_obj.download_to_drive(custom_path=str(target))
        yield target


async def _mtproto_download(input_json: dict[str, Any], target: Path) -> None:
    """Files above the Bot API's limit come through MTProto, which needs the originating message."""
    try:
        ensure_mtproto_ready()
    except Exception as exc:
        raise PipelineError("download", f"Large Telegram file requires MTProto credentials: {exc}") from exc
    try:
        message_ref = message_reference(input_json)
    except DeliveryTargetMissing:
        message_ref = {}
    if not message_ref:   # terminal: nothing else can fetch the file
        raise PipelineError("input", "Large Telegram file requires a messageRef to download via MTProto")
    await mtproto_download(chat_id=message_ref["chat_id"], message_id=message_ref["message_id"], dest_path=str(target))


@contextlib.contextmanager
def _download_dir(work_dir: Path | None):
    """Durable staging when a work dir is supplied, else a temp dir."""
    if work_dir is not None:
        target = Path(work_dir) / "download"
        target.mkdir(parents=True, exist_ok=True)
        yield target
        return
    with tempfile.TemporaryDirectory(prefix="rss-audio-runtime-telegram-") as tmpdir:
        yield Path(tmpdir)
