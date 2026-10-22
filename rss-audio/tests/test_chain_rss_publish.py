"""Native chain for rss.publish.telegram.v1.

Audio upload in the publish topic -> rss-audio gateway admission + staged
download -> runtime controller -> rss-audio worker loop (ffmpeg/ffprobe,
Azure STT, topic/mention enrichment, rclone upload, episode API) -> in-loop
result projector -> gateway delivery -> Bot API. Exact get: the reply's link
and the episode row resolve to the uploaded bucket bytes.

Case table
| case             | input                      | proves                                                    |
| audio upload     | 6 s mp3 in the rss topic   | staged sha, STT transcript as script, manifest names the  |
|                  |                            | published script sha, reply links the mp3                 |
"""
import asyncio
import hashlib
import json
import os
import subprocess

import pytest

from chain_harness import (CHAT_ID, FakeTelegram, azure_stt, build_gateway, deliver_all, gateway_env, inbound,
                           install_fake_models, model_calls, run_until, start_stack, tone_mp3)

pytestmark = pytest.mark.chain
RSS_THREAD = 9
TRANSCRIPT = ("Researchers heated water in polypropylene bottles and counted the particles released.\n\n"
              "The count reached sixteen million particles per litre at seventy degrees.")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    from pi_harness import preflight
    stack = start_stack()
    monkeypatch.setattr(preflight, "CODEX_AUTH_PATH", install_fake_models(tmp_path / "bin", stack.root / "models.jsonl"))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    for key, value in {**stack.rclone_env(), **azure_stt(stack, TRANSCRIPT)}.items():
        monkeypatch.setenv(key, value)
    yield stack
    stack.stop()


def _rss_env(stack):
    return {
        "RUNTIME_CONTROLLER_API_URL": stack.controller_url, "RUNTIME_CONTROLLER_BEARER_TOKEN": "chain-token",
        "RSS_AUDIO_RUNTIME_WORKER_ID": "chain-rss", "RSS_AUDIO_BOT_TOKEN": "123:chain",
        "RSS_AUDIO_WORK_DIR": str(stack.root / "rss-work"), "RSS_AUDIO_FAIL_LOG_DIR": str(stack.root / "rss-fail"),
        "RSS_AUDIO_SCRIBE_DB_PATH": str(stack.root / "scribe.sqlite3"),
        "RSS_AUDIO_B2_REMOTE": "chainfs", "RSS_AUDIO_B2_BUCKET": str(stack.bucket),
        "RSS_AUDIO_BASE_URL": stack.edge.url, "RSS_AUDIO_API_BASE_URL": stack.edge.url, "RSS_AUDIO_API_TOKEN": "chain",
        "RSS_AUDIO_PUBLIC_AUDIO_BASE_URL": stack.edge.url + "/pub/audio/episodes",
        "RSS_AUDIO_MULLVAD_PROXY": "", "RSS_AUDIO_MAC_SSH": "", "RSS_AUDIO_ENRICHMENT_JOBS": "1",
    }


def _exact_get(stack, url):
    prefix = stack.edge.url + "/pub/"
    assert url.startswith(prefix), url
    return (stack.bucket / url[len(prefix):]).read_bytes()


async def _run(stack, monkeypatch, event, audio):
    from config import load_settings
    from runtime_worker import worker_loop
    from tg_gateway import daemon

    runtime = build_gateway(monkeypatch, gateway_env(
        stack, identity="rss-audio",
        bindings={"rss_chat_id": CHAT_ID, "rss_publish_thread_id": RSS_THREAD, "rss_transcribe_thread_id": 10,
                  "tts_chat_id": CHAT_ID, "tts_thread_id": 11, "x_tts_thread_id": 12},
        extra=_rss_env(stack)), {"audio-1": audio})
    await daemon.handle_message(runtime, event)
    job_id = stack.psql("select job_id from tg_gateway.source_handoffs where state = 'submitted'")
    assert job_id, stack.psql("select state, last_error from tg_gateway.source_handoffs")

    unprojected = lambda: stack.api("GET", "/v1/results?resultOwner=rss-audio&limit=10")
    await run_until(lambda: worker_loop(load_settings()),
                    lambda: stack.job(job_id)["job"]["status"] in {"succeeded", "dead", "failed"} and not unprojected())
    job = stack.job(job_id)["job"]
    assert job["status"] == "succeeded", json.dumps(job, default=str)[:4000]
    await deliver_all(runtime)
    return job


def test_audio_upload_publishes_episode_and_replies(stack, monkeypatch):
    audio = tone_mp3(stack.root / "upload.mp3", seconds=6).read_bytes()
    event = inbound(thread_id=RSS_THREAD, message_id=301, attachment={
        "kind": "audio", "file_id": "audio-1", "file_name": "bottles.mp3", "mime_type": "audio/mpeg",
        "file_size": len(audio), "display_name": "bottles.mp3"})
    job = asyncio.run(_run(stack, monkeypatch, event, audio))
    assert job["inputJson"]["stagedSha256"] == hashlib.sha256(audio).hexdigest()
    assert [r.text for r in FakeTelegram.replies] == ["Received — queued."]

    [episode] = stack.edge.episodes["audio"]
    [reply] = stack.edge.sent("sendMessage")
    payload = reply["payload"]
    assert payload["chat_id"] == CHAT_ID and payload["message_thread_id"] == RSS_THREAD
    assert payload["reply_parameters"] == {"message_id": 301}
    assert episode["mp3_url"] in payload["text"]
    published = _exact_get(stack, episode["mp3_url"])
    assert len(published) == episode["mp3_size"]
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", "-"],
                           input=published, capture_output=True, check=True)
    assert abs(float(json.loads(probe.stdout)["format"]["duration"]) - 6) < 0.5
    notes = _exact_get(stack, episode["md_url"]).decode()
    script = _exact_get(stack, episode["speech_text_url"])
    assert "The finding" in notes and episode["mp3_url"] in notes
    assert b"sixteen million particles per litre" in script
    manifest = json.loads(_exact_get(stack, episode["manifest_url"]))
    assert hashlib.sha256(script).hexdigest() in json.dumps(manifest)
    assert {c["step"] for c in model_calls(stack.root / "models.jsonl")} == {"topics", "mentions"}
