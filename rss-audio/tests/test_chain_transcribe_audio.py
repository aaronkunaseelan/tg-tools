"""Native chain for transcribe.audio.telegram.v1 (scribe).

Audio upload in the transcribe topic starts a scribe; a text reply to it
appends. Each: rss-audio gateway admission (scribe store entry) + staged
download -> runtime controller -> transcribe worker loop (real transcribe.py
subprocess against Azure STT on the edge) -> worker-appended delivery ->
gateway delivery -> Bot API document. Exact get: the delivered document bytes
are the transcript the edge STT returned / the appended text.

Case table
| case          | input                               | proves                                       |
| scribe start  | 4 s mp3 in the transcribe topic     | staged sha; document carries the transcript  |
| scribe append | text reply to the start message     | same scribe; document carries start + append |
"""
import asyncio
import hashlib
import json

import pytest

from chain_harness import (CHAT_ID, FakeTelegram, azure_stt, build_gateway, deliver_all, gateway_env, inbound,
                           run_until, start_stack, tone_mp3)

pytestmark = pytest.mark.chain
TRANSCRIBE_THREAD = 10
TRANSCRIPT = "The committee agreed to publish the particle counts next month."
APPEND = "Correction: the counts publish in November."


@pytest.fixture
def stack(monkeypatch):
    stack = start_stack()
    for key, value in {**stack.rclone_env(), **azure_stt(stack, TRANSCRIPT)}.items():
        monkeypatch.setenv(key, value)
    yield stack
    stack.stop()


async def _admit_and_run(stack, runtime, event, done_jobs):
    from config import load_settings
    from tg_gateway import daemon
    from transcribe_runtime_worker import worker_loop

    await daemon.handle_message(runtime, event)
    jobs = stack.psql("select job_id from tg_gateway.source_handoffs where state = 'submitted' order by created_at").split()
    [job_id] = [j for j in jobs if j not in done_jobs]
    def complete():
        status = stack.job(job_id)["job"]["status"]
        if status != "succeeded":
            return status in {"dead", "failed"}
        # Finalization precedes enqueue: don't cancel the worker between them.
        return stack.psql(f"select count(*) from runtime.delivery_outbox where job_id='{job_id}'") != "0"

    await run_until(lambda: worker_loop(load_settings()),
                    complete,
                    describe=lambda: json.dumps(stack.job(job_id), default=str)[:3000])
    job = stack.job(job_id)["job"]
    assert job["status"] == "succeeded", json.dumps(job, default=str)[:4000]
    done_jobs.append(job_id)
    await deliver_all(runtime)
    return job


async def _lane(stack, monkeypatch, audio):
    runtime = build_gateway(monkeypatch, gateway_env(
        stack, identity="rss-audio",
        bindings={"rss_chat_id": CHAT_ID, "rss_publish_thread_id": 9, "rss_transcribe_thread_id": TRANSCRIBE_THREAD,
                  "tts_chat_id": CHAT_ID, "tts_thread_id": 11, "x_tts_thread_id": 12},
        extra={
            "RUNTIME_CONTROLLER_API_URL": stack.controller_url,
            "RUNTIME_CONTROLLER_BEARER_TOKEN": "chain-token", "RSS_AUDIO_RUNTIME_WORKER_ID": "chain-transcribe",
            "RSS_AUDIO_BOT_TOKEN": "123:chain", "RSS_AUDIO_WORK_DIR": str(stack.root / "rss-work"),
            "RSS_AUDIO_FAIL_LOG_DIR": str(stack.root / "rss-fail"),
            "RSS_AUDIO_SCRIBE_DB_PATH": str(stack.root / "scribe.sqlite3")}), {"audio-1": audio})
    done = []
    start = await _admit_and_run(stack, runtime, inbound(thread_id=TRANSCRIBE_THREAD, message_id=401, attachment={
        "kind": "audio", "file_id": "audio-1", "file_name": "meeting.mp3", "mime_type": "audio/mpeg",
        "file_size": len(audio), "display_name": "meeting.mp3"}), done)
    first_doc = stack.edge.sent("sendDocument")[-1]
    append = await _admit_and_run(stack, runtime, inbound(
        thread_id=TRANSCRIBE_THREAD, message_id=402, text=APPEND, reply_to=first_doc["message_id"]), done)
    return start, append


def test_scribe_start_then_append(stack, monkeypatch):
    audio = tone_mp3(stack.root / "meeting.mp3", seconds=4).read_bytes()
    start, append = asyncio.run(_lane(stack, monkeypatch, audio))
    assert start["inputJson"]["stagedSha256"] == hashlib.sha256(audio).hexdigest()
    assert [r.text for r in FakeTelegram.replies] == ["Received — queued.", "Received — queued."]

    first, second = stack.edge.sent("sendDocument")
    for sent, reply_to in ((first, 401), (second, 402)):
        payload = sent["payload"]
        assert int(payload["chat_id"]) == CHAT_ID and int(payload["message_thread_id"]) == TRANSCRIBE_THREAD
        assert json.loads(payload["reply_parameters"])["message_id"] == reply_to
    first_text = first["payload"]["document"]["content"].decode()
    second_text = second["payload"]["document"]["content"].decode()
    assert TRANSCRIPT in first_text and APPEND not in first_text
    assert TRANSCRIPT in second_text and APPEND in second_text
