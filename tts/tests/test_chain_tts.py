"""Native chains for tts.synthesize.telegram.v1 and x-tts.explain.telegram.v1.

Telegram message -> rss-audio gateway admission + source handoff -> runtime
controller (receipt, job) -> tts-worker loop (claim, acquire, materialise via
the installed QMD release, reader v4 for x-tts, enrichment, Azure speech,
rclone upload, episode API) -> tts result projector -> gateway delivery ->
Bot API. Then exact get: every object the episode row and the Telegram reply
point at is read back from the bucket by key.

Case table
| case                  | input                         | proves                                              |
| tts text message      | plain text in the tts topic   | faithful read published; reply links the exact mp3  |
| x-tts document upload | arXiv 1006.3868 PDF upload    | staged upload; write/review/revise on one claude     |
|                       |                               | session (--resume); notes + script published         |
"""
import asyncio
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from chain_harness import (CHAT_ID, FakeTelegram, build_gateway, deliver_all, gateway_env, inbound, install_fake_models,
                           model_calls, run_until, start_stack, tone_pcm)

pytestmark = pytest.mark.chain
TTS_THREAD, XTTS_THREAD = 11, 12
SOURCE = """# Plastic bottles and particles

Researchers prepared infant formula in polypropylene feeding bottles and counted the particles that the bottles released into the liquid.

Heating the water to seventy degrees released up to sixteen million particles per litre, and sterilising the bottles beforehand raised the count further.

The authors caution that exposure estimates depend on assumptions about feeding practice, so the health relevance remains uncertain.
"""


@pytest.fixture
def stack(tmp_path, monkeypatch):
    stack = start_stack()
    from pi_harness import preflight
    monkeypatch.setattr(preflight, "CODEX_AUTH_PATH", install_fake_models(tmp_path / "bin", stack.root / "models.jsonl"))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    for key, value in stack.rclone_env().items():
        monkeypatch.setenv(key, value)
    # Speech sized to the text at a natural pace; the worker rejects implausible ratios.
    stack.edge.route("POST", r"/openai/v1/audio/speech$",
                     lambda req: (200, tone_pcm(len(req.json()["input"].split()) / 2.5)))
    yield stack
    stack.stop()


def _tts_settings(stack, monkeypatch):
    from tts_worker.config import load_settings
    env = {
        "TG_TOOLS_PROGRESS_ENABLED": "1",
        "TTS_WORKER_ID": "chain-tts", "RUNTIME_CONTROLLER_API_URL": stack.controller_url,
        "RUNTIME_CONTROLLER_BEARER_TOKEN": "chain-token", "TTS_DATABASE_URL": stack.dsn(),
        "TTS_WORK_DIR": str(stack.root / "tts-work"), "TTS_SPEECH_PROVIDER": "azure",
        "TTS_AZURE_TTS_BASE_URL": stack.edge.url, "TTS_AZURE_TTS_API_KEY": "chain",
        "TTS_RCLONE_REMOTE": "chainfs", "TTS_B2_BUCKET": str(stack.bucket),
        "TTS_PUBLIC_BASE_URL": stack.edge.url + "/pub", "TTS_API_BASE_URL": stack.edge.url, "TTS_API_TOKEN": "chain",
        "TTS_FEED_URL": stack.edge.url + "/tts/podcast.xml", "TTS_FEED_TITLE": "Example TTS",
        "TTS_AZURE_TTS_KEY_CMD": "printf chain",
        "TTS_AZURE_CHAT_BASE_URL": stack.edge.url + "/openai/v1", "TTS_AZURE_CHAT_KEY_CMD": "printf chain",
        "TTS_TITLE_MODEL": "",
        "TTS_PI_ROLES_FILE": "",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TTS_PI_ROLES_FILE")
    return load_settings()


def _exact_get(stack, url: str) -> bytes:
    prefix = stack.edge.url + "/pub/"
    assert url.startswith(prefix), url
    return (stack.bucket / url[len(prefix):]).read_bytes()


async def _run_lane(stack, monkeypatch, event, files=None):
    from tg_gateway import daemon
    from tts_worker.result_projector import ResultProjector
    from tts_worker.runtime_client import RuntimeClient
    from tts_worker.worker import worker_loop

    runtime = build_gateway(monkeypatch, gateway_env(
        stack, identity="rss-audio",
        bindings={"rss_chat_id": CHAT_ID, "rss_publish_thread_id": 9, "rss_transcribe_thread_id": 10,
                  "tts_chat_id": CHAT_ID, "tts_thread_id": TTS_THREAD, "x_tts_thread_id": XTTS_THREAD},
        extra={"RSS_AUDIO_SCRIBE_DB_PATH": str(stack.root / "scribe.sqlite3")}), files)
    await daemon.handle_message(runtime, event)
    job_id = stack.psql("select job_id from tg_gateway.source_handoffs where state = 'submitted'")
    assert job_id, stack.psql("select state, last_error from tg_gateway.source_handoffs")

    settings = _tts_settings(stack, monkeypatch)
    await run_until(lambda: worker_loop(settings),
                    lambda: stack.job(job_id)["job"]["status"] in {"succeeded", "dead", "failed"})
    job = stack.job(job_id)["job"]
    assert job["status"] == "succeeded", json.dumps(stack.job(job_id), default=str)[:4000]
    stages = json.loads(stack.psql(f"select stages_json from runtime.progress_snapshots where job_id='{job_id}'"))
    names = {stage["stage"] for stage in stages}
    assert {"acquire_source", "extract_document", "topic_analysis", "reference_extraction",
            "synthesize_speech", "publish_episode"} <= names
    assert all(stage["kind"] in {"completed", "reused"} for stage in stages)
    if job["workloadType"] == "x_tts_explain":
        assert {"write_explanation", "review_explanation", "revise_explanation"} <= names
    else:
        assert "prepare_reading" in names and "write_explanation" not in names
    projected = await asyncio.to_thread(ResultProjector(RuntimeClient(stack.controller_url, "chain-token")).project_once)
    assert projected == 1
    await deliver_all(runtime)


def _episode_checks(stack, event):
    """Exact get: the reply's links, the episode row and the manifest all name the same bucket bytes."""
    [episode] = stack.edge.episodes["tts"]
    [reply] = stack.edge.sent("sendMessage")
    payload = reply["payload"]
    assert payload["chat_id"] == CHAT_ID and payload["message_thread_id"] == event.message.thread_id
    assert payload["reply_parameters"] == {"message_id": event.message.message_id}
    links = re.findall(r"http\S+/pub/\S+", payload["text"])
    assert links == [episode["mp3_url"], episode["speech_text_url"], episode["md_url"]]
    audio, script, notes = (_exact_get(stack, url) for url in links)
    manifest = json.loads(_exact_get(stack, episode["manifest_url"]))
    hashes = {entry["sha256"] for entry in manifest["artifacts"].values()}
    assert {hashlib.sha256(blob).hexdigest() for blob in (audio, script, notes)} <= hashes
    assert len(audio) == episode["mp3_size"]
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", "-"],
                           input=audio, capture_output=True, check=True)
    hours, minutes, seconds = map(int, episode["duration"].split(":"))
    assert abs(float(json.loads(probe.stdout)["format"]["duration"]) - (hours * 3600 + minutes * 60 + seconds)) <= 1
    return episode, notes.decode(), script.decode()


def test_tts_text_message_publishes_and_replies(stack, monkeypatch):
    event = inbound(thread_id=TTS_THREAD, message_id=101, text=SOURCE)
    asyncio.run(_run_lane(stack, monkeypatch, event))
    assert [r.text for r in FakeTelegram.replies] == ["Received — queued."]
    episode, notes, script = _episode_checks(stack, event)
    assert episode["route"] == "tts" and episode["episode_kind"] == "faithful_read"
    assert "sixteen million particles" in script and episode["mp3_url"] in notes
    assert {c["step"] for c in model_calls(stack.root / "models.jsonl")} == {"topics", "mentions"}


@pytest.mark.skipif(not (Path(__file__).parent / "fixtures" / "arxiv-1006.3868.pdf").exists(),
                    reason="Non-CC arXiv fixture not distributed")
def test_xtts_document_upload_runs_reader_and_publishes(stack, monkeypatch):
    pdf = (Path(__file__).parent / "fixtures" / "arxiv-1006.3868.pdf").read_bytes()
    event = inbound(thread_id=XTTS_THREAD, message_id=201, attachment={
        "kind": "document", "file_id": "doc-1", "file_name": "1006.3868.pdf", "mime_type": "application/pdf",
        "file_size": len(pdf), "display_name": "1006.3868.pdf"})
    asyncio.run(_run_lane(stack, monkeypatch, event, files={"doc-1": pdf}))
    episode, notes, script = _episode_checks(stack, event)
    assert episode["route"] == "x-tts" and episode["episode_kind"] == "explained_read"
    assert "Philosophy and the practice of Bayesian statistics" in _exact_get(stack, episode["source_md_url"]).decode()
    assert "Heating water in a polypropylene bottle" in script and "The finding" in notes
    calls = model_calls(stack.root / "models.jsonl")
    reader = [c for c in calls if c["cli"] == "claude"]
    assert [c["step"] for c in reader] == ["write", "review", "revise"]
    session = reader[0]["argv"][reader[0]["argv"].index("--session-id") + 1]
    assert reader[2]["argv"][reader[2]["argv"].index("--resume") + 1] == session
