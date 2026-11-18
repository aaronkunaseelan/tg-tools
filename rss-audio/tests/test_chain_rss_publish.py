"""Native chain for rss.publish.telegram.v1.

Audio upload in the publish topic -> rss-audio gateway admission + staged
download -> runtime controller -> rss-audio worker loop (ffmpeg/ffprobe,
Azure STT, topic/mention enrichment, rclone upload, episode API) -> in-loop
result projector -> gateway delivery -> Bot API. Exact get: the reply's link
and the episode row resolve to the uploaded bucket bytes.

Case table
| case             | input                      | proves                                                    |
| audio upload     | 6 s mp3 in the rss topic   | staged sha, STT transcript as script, manifest names the  |
|                  |                            | published script sha, reply links the mp3, the notes are  |
|                  |                            | sections with clocks and gates, exactly two model calls    |
| a speaker named  | the same, titled for a     | the speaker the metadata names reaches the prompt and the  |
|                  | person ("Jane Doe")        | renderer writes the full name once, the surname after      |
| rebuild to v2    | the same, then a rebuild   | STT from the stored mp3; .v2. documents live; v1 documents |
|                  | job by slug                | deleted, mp3 kept; still one Telegram message              |
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
DOCUMENTS = ("script.md", "segments.json", "notes.md", "manifest.json", "page.html")   # Azure chunk timings give segments
TRANSCRIPT = ("Jane Doe's lab heated water in polypropylene bottles and counted the particles released.\n\n"
              "The count reached sixteen million particles per litre at seventy degrees, Doe said.")


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


async def _drain(stack):
    """Run the worker until no job is runnable and every result is projected."""
    from config import load_settings
    from runtime_worker import worker_loop
    jobs = lambda: stack.psql("select producer_ref || '=' || status from runtime.jobs order by created_at")
    await run_until(lambda: worker_loop(load_settings()),
                    lambda: not stack.psql("select 1 from runtime.jobs where status in ('pending','leased','retryable')")
                    and not stack.api("GET", "/v1/results?resultOwner=rss-audio&limit=10"), describe=jobs)
    return jobs().splitlines()


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

    # The publish job, then the notes job it submits (two-job path); both projected.
    unprojected = lambda: stack.api("GET", "/v1/results?resultOwner=rss-audio&limit=10")
    jobs = lambda: stack.psql("select producer_ref || '=' || status from runtime.jobs order by created_at")
    await run_until(lambda: worker_loop(load_settings()),
                    lambda: not stack.psql("select 1 from runtime.jobs where status in ('pending','leased','retryable')")
                    and not unprojected(), describe=jobs)
    job = stack.job(job_id)["job"]
    assert job["status"] == "succeeded", json.dumps(job, default=str)[:4000]
    assert jobs().splitlines() == [f"{job['producerRef']}=succeeded", f"{job['producerRef']}:notes=succeeded"], \
        stack.psql("select last_error_class || ': ' || last_error_text from runtime.jobs where status = 'dead'")
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
    # One message, at publish, with the transcript link; the notes job sends nothing.
    assert episode["speech_text_url"].rsplit("/", 1)[1] in payload["text"] and "Notes</a>" not in payload["text"]
    published = _exact_get(stack, episode["mp3_url"])
    assert len(published) == episode["mp3_size"]
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", "-"],
                           input=published, capture_output=True, check=True)
    assert abs(float(json.loads(probe.stdout)["format"]["duration"]) - 6) < 0.5
    notes = _exact_get(stack, episode["md_url"]).decode()
    script = _exact_get(stack, episode["speech_text_url"])
    assert "The finding" in notes and episode["mp3_url"] in notes
    # an episode's notes are the section shape: an opening lede per section under its clock, and the
    # episode's ranked points above them (X-104 upstream), all inside the same two model calls
    assert "## Main points" in notes and "### 0:00 · The finding" in notes
    assert "Doe says the study measured what heating the bottle released" in notes
    assert b"sixteen million particles per litre" in script
    manifest = json.loads(_exact_get(stack, episode["manifest_url"]))
    assert hashlib.sha256(script).hexdigest() in json.dumps(manifest)
    gates = manifest["gates"]["notes"]
    assert gates["ok"] and gates["stats"]["sections"] >= 1 and "quotesOutOfRange" in gates["stats"]
    assert [c["step"] for c in model_calls(stack.root / "models.jsonl")] == ["topics", "mentions"]
    assert len(model_calls(stack.root / "models.jsonl")) == 2   # the budget: two calls, no repair
    page = _exact_get(stack, episode["page_url"]).decode()   # the notes job's page, linked from the row
    assert episode["page_url"].endswith(".page.html") and "sixteen million particles per litre" in page
    assert page.index('<h2 id="script">') > page.index("<h1>")


def test_an_episode_named_for_its_speaker_writes_the_full_name_once(stack, monkeypatch):
    """The speaker an episode's metadata names: one sentence to the prompt, the full name in code once."""
    audio = tone_mp3(stack.root / "upload.mp3", seconds=6).read_bytes()
    event = inbound(thread_id=RSS_THREAD, message_id=303, attachment={
        "kind": "audio", "file_id": "audio-1", "file_name": "Jane Doe", "mime_type": "audio/mpeg",
        "file_size": len(audio), "display_name": "Jane Doe"})
    asyncio.run(_run(stack, monkeypatch, event, audio))
    [episode] = stack.edge.episodes["audio"]
    notes = _exact_get(stack, episode["md_url"]).decode()
    # the title is exactly a person's name, so the notes job's input carries that speaker, and the
    # renderer writes the full name at its first appearance — the main point above the sections
    assert notes.startswith("# Jane Doe")            # the row carries the channel prefix; the notes do not
    body = notes.split("## Main points", 1)[1]
    assert body.count("Jane Doe") == 1              # once, where the surname first appears
    assert body.count("Doe says") >= 2             # the ledes keep the surname
    assert "- 0:00 · Jane Doe says heating the bottle released millions of particles." in notes
    assert "### 0:00 · The finding\n\nDoe says the study measured" in notes
    gates = json.loads(_exact_get(stack, episode["manifest_url"]))["gates"]["notes"]
    assert gates["stats"]["topicsWithoutName"] == 0
    assert [c["step"] for c in model_calls(stack.root / "models.jsonl")] == ["topics", "mentions"]


def test_a_rebuild_replaces_the_documents_with_v2(stack, monkeypatch):
    import rebuild
    audio = tone_mp3(stack.root / "upload.mp3", seconds=6).read_bytes()
    event = inbound(thread_id=RSS_THREAD, message_id=302, attachment={
        "kind": "audio", "file_id": "audio-1", "file_name": "bottles.mp3", "mime_type": "audio/mpeg",
        "file_size": len(audio), "display_name": "bottles.mp3"})
    asyncio.run(_run(stack, monkeypatch, event, audio))
    [before] = stack.edge.episodes["audio"]
    slug = before["slug"]
    payload = rebuild.rebuild_job_payload(rebuild.rebuild_input(before, "audio", version=rebuild.next_version(before)))
    stack.api("POST", "/v1/submit", payload)
    jobs = asyncio.run(_drain(stack))
    assert jobs[-2:] == [f"rebuild:{slug}=succeeded", f"rebuild:{slug}:notes=succeeded"], jobs

    [after] = stack.edge.episodes["audio"]
    for key, kind in (("speech_text_url", "script.md"), ("md_url", "notes.md"), ("manifest_url", "manifest.json"),
                      ("page_url", "page.html")):
        assert after[key].endswith(f"/{slug}.v2.{kind}"), (key, after[key])
    assert after["mp3_url"] == before["mp3_url"]
    stored = sorted(p.name for p in stack.bucket.rglob(f"{slug}.*"))
    assert stored == sorted([f"{slug}.mp3"] + [f"{slug}.v2.{kind}" for kind in DOCUMENTS]), stored
    assert b"sixteen million particles per litre" in _exact_get(stack, after["speech_text_url"])
    manifest = json.loads(_exact_get(stack, after["manifest_url"]))
    assert hashlib.sha256(_exact_get(stack, after["speech_text_url"])).hexdigest() in json.dumps(manifest)
    assert len(stack.edge.sent("sendMessage")) == 1   # the publish's; rebuild and notes send nothing
