"""Native chain for meeting.note.telegram.v1.

Audio upload in the meeting topic -> meeting-summary gateway admission + staged
download -> runtime controller -> meeting worker (ffmpeg chunking, Azure STT,
codex responses summary) -> result consumer (store note, review message,
preview document, accept button) -> gateway delivery -> Bot API -> accept
callback -> final document. Exact get: the final document's bytes are the
committed note's retained Markdown.

Case table
| case             | input                        | proves                                                     |
| audio upload     | 4 s mp3 in the meeting topic | transcript stored, summary rendered into review + preview, |
|                  | then the accept button       | accept sends the committed Markdown replying to the review;|
|                  |                              | the note records the STT transcript's length               |
"""
import asyncio
import hashlib
import json
import time

import pytest

from chain_harness import (CHAT_ID, azure_stt, build_gateway, callback, codex_responses, deliver_all,
                           gateway_env, inbound, start_stack, tone_mp3)

pytestmark = pytest.mark.chain
MEETING_THREAD = 107
TRANSCRIPT = "Ana proposed moving the launch to November. Ben agreed and will draft the notice by Friday."
CHUNK = {"title_hint": "Launch timing", "meeting_date_hint": "", "overview": "The launch moves to November.",
         "attendees": ["Ana", "Ben"], "key_points": ["Launch moves to November"], "decisions": ["Launch in November"],
         "action_items": ["Ben drafts the notice by Friday"], "open_questions": []}
FINAL = {"title": "Launch timing review", "meeting_date": "", "overview": "The team moved the launch to November.",
         "attendees": ["Ana", "Ben"], "key_points": ["Launch moves to November"], "decisions": ["Launch in November"],
         "action_items": ["Ben drafts the notice by Friday"], "open_questions": []}


@pytest.fixture
def stack(tmp_path, monkeypatch):
    stack = start_stack(meeting_schema=True)
    for key, value in azure_stt(stack, TRANSCRIPT).items():
        monkeypatch.setenv(key, value)
    auth = tmp_path / "pi-auth.json"
    auth.write_text(json.dumps({"openai-codex": {"type": "oauth", "accountId": "chain", "access": "chain",
                                                 "expires": int(time.time() * 1000) + 86_400_000}}))
    summaries = lambda body: CHUNK if body["instructions"].startswith("You summarize one meeting transcript chunk") else FINAL
    stack.meeting_env = {
        "MEETING_SUMMARY_DATABASE_URL": stack.dsn("meeting"), "MEETING_SUMMARY_STATE_DIR": str(stack.root / "meeting"),
        "MEETING_SUMMARY_PI_AUTH_PATH": str(auth), "MEETING_SUMMARY_SUMMARY_MODEL": "chain-model",
        "MEETING_SUMMARY_CODEX_RESPONSES_URL": codex_responses(stack, summaries),
        "MEETING_SUMMARY_BOT_TOKEN": "123:chain", "MEETING_SUMMARY_ALLOWED_CHAT_ID": str(CHAT_ID),
        "MEETING_SUMMARY_ALLOWED_THREAD_ID": str(MEETING_THREAD), "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "chain",
        "MEETING_SUMMARY_RUNTIME_CONTROLLER_URL": stack.controller_url,
        "MEETING_SUMMARY_RUNTIME_CONTROLLER_BEARER_TOKEN": "chain-token", "MEETING_SUMMARY_RUNTIME_WORKER_ID": "chain-meeting",
    }
    yield stack
    stack.stop()


async def _lane(stack, monkeypatch, audio):
    from result_consumer import process_results
    from runtime_worker import run_worker
    from tg_gateway import daemon

    runtime = build_gateway(monkeypatch, gateway_env(
        stack, identity="meeting-summary",
        bindings={"meeting_chat_id": CHAT_ID, "meeting_thread_id": MEETING_THREAD},
        extra=stack.meeting_env), {"audio-1": audio})
    await daemon.handle_message(runtime, inbound(thread_id=MEETING_THREAD, message_id=601, attachment={
        "kind": "audio", "file_id": "audio-1", "file_name": "standup.mp3", "mime_type": "audio/mpeg",
        "file_size": len(audio), "display_name": "standup.mp3"}))
    job_id = stack.psql("select job_id from tg_gateway.source_handoffs where state = 'submitted'")
    assert job_id, stack.psql("select state, last_error from tg_gateway.source_handoffs")

    assert await asyncio.to_thread(run_worker, once=True) == 1
    job = stack.job(job_id)["job"]
    assert job["status"] == "succeeded", json.dumps(job, default=str)[:4000]

    # One consumer round per review phase: review message, preview document, accept button.
    for _ in range(4):
        await asyncio.to_thread(process_results, once=True)
        await deliver_all(runtime)
    [accept] = [m for m in stack.edge.sent("editMessageText") if m["payload"].get("reply_markup")]
    markup = accept["payload"]["reply_markup"]
    data = (json.loads(markup) if isinstance(markup, str) else markup)["inline_keyboard"][0][0]["callback_data"]

    await daemon.handle_callback(runtime, callback(thread_id=MEETING_THREAD,
                                                   message_id=accept["payload"]["message_id"], data=data))
    await deliver_all(runtime)
    return accept["payload"]["message_id"]


def test_meeting_audio_reviews_then_accepts_final_document(stack, monkeypatch):
    audio = tone_mp3(stack.root / "standup.mp3", seconds=4).read_bytes()
    review_message_id = asyncio.run(_lane(stack, monkeypatch, audio))

    [review] = [m for m in stack.edge.sent("sendMessage") if m["message_id"] == review_message_id]
    assert review["payload"]["reply_parameters"] == {"message_id": 601}
    assert "Launch timing review" in review["payload"]["text"]
    preview, final = stack.edge.sent("sendDocument")
    assert b"November" in preview["payload"]["document"]["content"]
    assert json.loads(final["payload"]["reply_parameters"])["message_id"] == review_message_id

    content = final["payload"]["document"]["content"]
    retained_sha = stack.psql("select encode(sha256(convert_to(metadata_json->>'retained_markdown', 'UTF8')), 'hex')"
                              " from tg_meeting_summary.meeting_notes", db="meeting")
    assert hashlib.sha256(content).hexdigest() == retained_sha
    assert b"Ben drafts the notice by Friday" in content
    assert stack.psql("select metadata_json->>'transcript_char_count' from tg_meeting_summary.meeting_notes",
                      db="meeting") == str(len(TRANSCRIPT))
    receipts = json.loads(stack.psql(
        "select metadata_json->'final_delivery_receipts' from tg_meeting_summary.meeting_notes", db="meeting"))
    assert [r["telegramMessageId"] for r in receipts.values()] == [final["message_id"]]
