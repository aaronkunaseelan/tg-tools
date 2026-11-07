"""Rebuild by slug (rss-audio/rebuild.py): speech-to-text from the stored mp3 into a new version.

| episode now               | rebuild            | outcome                                                          |
| no script                 | v1                 | unversioned script + segments uploaded; no PUT (the notes job's) |
| script v1 (+ notes)       | v2                 | .v2. script + segments; the row untouched; notes request v2      |
| a retry                   | v2                 | the same version again (the row is still on v1)                  |
| at v3                     | v2                 | terminal: superseded                                             |
| speech-to-text fails      |                    | retryable stt error, nothing uploaded or PUT                     |
| unknown segmenter         |                    | the worker's input error before any read                         |
| no stored mp3             |                    | terminal; a failed read of it is retryable                       |
plan(): reads only; the version is fixed at submit; the key is (slug, version); dry run submits nothing;
--apply prints the controller's action and the job's status (an existing dead job: the key is spent).
"""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import notes_job
import pipeline
import rebuild
from config import load_settings

SLUG = "2026-11-03-bloomberg-surveillance-radio-27d0976312c4"
BASE = "https://rss-feeds.stations.work/surveillance/episodes"
ENV = {"RSS_AUDIO_BASE_URL": "https://rss-feeds.stations.work", "RSS_AUDIO_B2_BUCKET": "bucket",
       "HOME": os.environ.get("HOME", "/tmp"), "RSS_AUDIO_B2_REMOTE": "b2", "RSS_AUDIO_ENRICHMENT_ENABLED": "1"}


def settings(**extra):
    with mock.patch.dict(os.environ, {**ENV, **extra}, clear=True):
        return load_settings()


def episode(script=None, md=None):
    return {"id": 7, "slug": SLUG, "title": "Bloomberg Surveillance Radio 11/3/2026", "pub_date": "2026-11-03T12:00:00Z",
            "mp3_url": f"{BASE}/{SLUG}.mp3", "mp3_size": 1000,
            "speech_text_url": f"{BASE}/{script}" if script else None, "md_url": f"{BASE}/{md}" if md else None}


@pytest.fixture
def store(monkeypatch):
    seen = SimpleNamespace(episode=episode(), uploaded=[], api=[], downloads=[], stt=True,
                           script=SimpleNamespace(text="Hello.\n\nWorld.", timed_transcript='{"t": 1}',
                                                  sha256="b" * 64, stt_engine="parakeet-ultra@fluidaudio-0.18.0"))

    def download(s, feed, name, dest):
        seen.downloads.append(name)
        if name != f"{SLUG}.mp3":
            return False
        Path(dest).write_bytes(b"ID3")
        return True

    def transcribe(s, mp3, work, tier):
        assert mp3.parent.name == "stored" and tier == "sonnet"
        return seen.script if seen.stt else None

    monkeypatch.setattr(notes_job, "_download", download)
    monkeypatch.setattr(rebuild, "_transcribe", transcribe)
    monkeypatch.setattr(rebuild, "_probe_duration_seconds", lambda s, path: 10800)
    monkeypatch.setattr(rebuild, "_get_episode_by_slug", lambda s, slug, feed: seen.episode)
    monkeypatch.setattr(rebuild, "_upload_artifact", lambda s, path, key: seen.uploaded.append(key))
    seen.run = lambda version, tmp, **extra: rebuild.run_rebuild(
        settings(), {"feed": "surveillance", "slug": SLUG, "version": version, **extra}, tmp, "sonnet")
    return seen


def test_next_version():
    assert rebuild.next_version(episode()) == 1
    assert rebuild.next_version(episode(f"{SLUG}.script.md")) == 2
    assert rebuild.next_version(episode(f"{SLUG}.v2.script.md")) == 3


def test_v1_when_the_episode_has_no_script(store, tmp_path):
    result = store.run(1, tmp_path)
    assert store.uploaded == [f"surveillance/episodes/{SLUG}.script.md", f"surveillance/episodes/{SLUG}.segments.json"]
    assert result.notes["version"] == 1 and result.notes["durationSeconds"] == 10800
    assert result.segments_url == f"{BASE}/{SLUG}.segments.json" and result.notes_url is None


def test_v2_beside_v1_and_the_notes_request_carries_it(store, tmp_path):
    store.episode = episode(f"{SLUG}.script.md", f"{SLUG}.notes.md")
    result = store.run(2, tmp_path, segmenter=None, publishedAt="2026-11-03T12:00:00Z", dateBasis="scheduled_window")
    assert store.uploaded == [f"surveillance/episodes/{SLUG}.v2.script.md",
                              f"surveillance/episodes/{SLUG}.v2.segments.json"]
    assert {k: result.notes[k] for k in ("inputKind", "version", "scriptSha256", "dateBasis", "slug")} == {
        "inputKind": "notes", "version": 2, "scriptSha256": "b" * 64, "dateBasis": "scheduled_window", "slug": SLUG}
    assert (tmp_path / f"{SLUG}.v2.script.md").read_text() == "Hello.\n\nWorld."
    assert not hasattr(rebuild, "_api_json")   # the row changes only in the notes job's one PUT
    assert not (tmp_path / f"{SLUG}.mp3").exists()   # the stored mp3 is not this job's artifact


def test_a_retry_rebuilds_the_same_version(store, tmp_path):
    store.episode = episode(f"{SLUG}.script.md", f"{SLUG}.notes.md")
    store.stt = False
    with pytest.raises(pipeline.PipelineError):
        store.run(2, tmp_path)
    store.stt = True
    assert store.run(2, tmp_path).notes["version"] == 2
    store.episode = episode(f"{SLUG}.v2.script.md", f"{SLUG}.v2.notes.md")   # its notes job already switched it
    assert store.run(2, tmp_path).notes["version"] == 2


def test_a_newer_version_supersedes(store, tmp_path):
    store.episode = episode(f"{SLUG}.v3.script.md")
    with pytest.raises(pipeline.PipelineError) as error:
        store.run(2, tmp_path)
    assert error.value.retryable is False and "v3" in str(error.value)
    assert store.downloads == [] and store.uploaded == []


def test_speech_to_text_failing_is_retryable_and_changes_nothing(store, tmp_path):
    store.stt = False
    with pytest.raises(pipeline.PipelineError) as error:
        store.run(1, tmp_path)
    assert error.value.stage == "stt" and error.value.retryable is True
    assert store.uploaded == [] and store.api == []


def test_an_unknown_segmenter_is_refused_before_any_read(store, tmp_path):
    import asyncio
    import runtime_worker
    job = rebuild.rebuild_input(store.episode, "surveillance", version=1, segmenter="no-such")
    with pytest.raises(pipeline.PipelineError) as error:
        asyncio.run(runtime_worker.execute_runtime_job(settings(), None, job, tmp_path))
    assert error.value.stage == "input" and store.downloads == []


def test_a_failed_read_of_the_mp3_is_retryable(store, tmp_path, monkeypatch):
    def blip(*a):
        raise pipeline.PipelineError("notes", "could not read the mp3 from the store", retryable=True)
    monkeypatch.setattr(notes_job, "_download", blip)
    with pytest.raises(pipeline.PipelineError) as error:
        store.run(1, tmp_path)
    assert error.value.retryable is True


def test_no_stored_mp3_is_terminal(store, tmp_path, monkeypatch):
    monkeypatch.setattr(notes_job, "_download", lambda *a: False)
    with pytest.raises(pipeline.PipelineError) as error:
        store.run(1, tmp_path)
    assert error.value.retryable is False and store.uploaded == []


def test_the_plan_reads_only_and_fixes_the_version(store):
    store.episode = episode(f"{SLUG}.script.md", f"{SLUG}.notes.md")
    payload, lines = rebuild.plan(settings(), SLUG, "surveillance", producer_ref="surveillance-radio:2026-11-03:rebuild")
    assert payload["idempotencyKey"] == f"rss-audio:rebuild:{SLUG}:v2"
    assert payload["producerRef"] == "surveillance-radio:2026-11-03:rebuild" and payload["priority"] == 10
    assert payload["inputJson"]["version"] == 2 and payload["inputJson"]["inputKind"] == "rebuild"
    assert lines[0] == f"rebuild {SLUG} in surveillance: v2" and "older versions" in lines[-1]
    assert store.api == [] and store.uploaded == [] and store.downloads == []


def test_the_cli_is_a_dry_run_by_default(store, monkeypatch, capsys):
    import config
    import runtime_client
    submitted = []
    monkeypatch.setattr(config, "worker_settings", lambda: (settings(), {}))
    monkeypatch.setattr(runtime_client.RuntimeClient, "submit_job", lambda self, payload: submitted.append(payload) or {
        "action": "existing", "job": {"jobId": "b-1", "status": "dead"}})
    assert rebuild.main([SLUG, "--feed", "surveillance"]) == 0
    assert submitted == [] and "dry run: nothing submitted" in capsys.readouterr().out
    assert rebuild.main([SLUG, "--feed", "surveillance", "--apply"]) == 0 and len(submitted) == 1
    assert capsys.readouterr().out.splitlines()[-1] == f"existing rss-audio:rebuild:{SLUG}:v1: job b-1 dead"
