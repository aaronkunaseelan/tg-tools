"""rss-audio enrichment tier: feed -> pipeline -> tier from the enrichment tier file, once per job, before any work.

| feed         | pipeline     | tier (file: episodes haiku, surveillance sonnet) |
| audio        | episodes     | haiku                                            |
| surveillance | surveillance | sonnet                                           |
Tier file edited between two jobs: the second job uses the new tier (no restart).
Missing file, bad JSON, pipeline not listed, unknown tier, a roles file naming a model for a transcript role or
redefining the chosen tier in "models", a tier on openai-codex (X-60):
terminal `enrichment_config` before any download, conversion, transcription or upload; never "audio published anyway".
Enrichment switched off: no tier file needed.
Publish is speech-to-text first (X-76 step 4): mp3 + script (+ <prefix>.segments.json when there are timings)
go out with speech_text_url set and no notes or manifest; the result carries the notes job's request.
Speech-to-text failing publishes the audio alone, with no notes request; a configuration error fails the job.
"""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest

import episode_enrichment
import pipeline
import runtime_worker
from pi_harness import EnrichmentConfigError
from config import load_settings
from pipeline import ENRICHMENT_PIPELINES, FEEDS, PipelineError

ENV = {"RSS_AUDIO_BASE_URL": "https://rss-feeds.stations.work", "RSS_AUDIO_B2_BUCKET": "example-audio",
       "HOME": os.environ.get("HOME", "/tmp"), "RSS_AUDIO_B2_REMOTE": "b2"}


def make_settings(**extra):
    with mock.patch.dict(os.environ, {**ENV, **extra}, clear=True):
        return load_settings()


@pytest.fixture
def work(monkeypatch, tmp_path):
    seen = SimpleNamespace(enrich=[], converted=[], commands=[], api=[], posts=[], uploaded=[])

    def api(settings, method, path, payload=None):
        seen.api.append((method, path))
        if method == "POST":
            seen.posts.append(payload)
        return [] if method == "GET" else {}

    def run(args, *, stage, timeout=None, cwd=None):
        seen.commands.append(args)
        if "--immutable" in args:   # an enrichment document, staged under its file name
            seen.uploaded.extend(os.listdir(args[-2]))
        if "json" in args:
            return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "audio"}], "format": {"duration": "60"}}))
        return SimpleNamespace(stdout="60\n")

    seen.script = SimpleNamespace(text="s", timed_transcript='{"schema": "tg-tools/timed-transcript/1"}',
                                  sha256="a" * 64, stt_engine="parakeet-ultra@fluidaudio-0.18.0")

    real = pipeline._transcribe

    def transcribe(settings, mp3_path, work_dir, tier):   # the real checks; only the STT call itself is stubbed
        if settings.enrichment_enabled:
            seen.enrich.append({"tier": tier})
        return real(settings, mp3_path, work_dir, tier)
    monkeypatch.setattr(episode_enrichment, "ensure_script", lambda **kw: seen.script)

    monkeypatch.setattr(pipeline, "_api_json", api)
    monkeypatch.setattr(pipeline, "_run", run)
    monkeypatch.setattr(pipeline, "_convert_input_to_mp3",
                        lambda settings, source, out, stage: (seen.converted.append(source), out.write_bytes(b"mp3")))
    monkeypatch.setattr(pipeline, "_transcribe", transcribe)
    seen.audio = tmp_path / "talk.mp3"
    seen.audio.write_bytes(b"audio")
    seen.run = lambda settings, feed, name="job": asyncio.run(runtime_worker.execute_runtime_job(
        settings, None, {"localSourcePath": str(seen.audio), "feed": feed}, tmp_path / name))
    return seen


def test_pipelines_cover_every_feed():
    assert set(ENRICHMENT_PIPELINES) == set(FEEDS)


@pytest.mark.parametrize("feed, tier", [("audio", "haiku"), ("surveillance", "sonnet")])
def test_each_feed_enriches_on_its_pipelines_tier(work, feed, tier):
    work.run(make_settings(), feed)
    assert [call["tier"] for call in work.enrich] == [tier]


def test_a_job_without_a_feed_is_the_episodes_pipeline(work, tmp_path):
    asyncio.run(runtime_worker.execute_runtime_job(make_settings(), None, {"localSourcePath": str(work.audio)}, tmp_path / "j"))
    assert [call["tier"] for call in work.enrich] == ["haiku"]


def test_a_changed_file_applies_to_the_next_job_without_a_restart(work, enrichment_tiers):
    settings = make_settings()
    work.run(settings, "surveillance", "one")
    enrichment_tiers.write_text(json.dumps({"episodes": "haiku", "surveillance": "haiku"}))
    work.run(settings, "surveillance", "two")
    assert [call["tier"] for call in work.enrich] == ["sonnet", "haiku"]


@pytest.mark.parametrize("content", [None, "{", "[]", '{"episodes": "haiku"}', '{"surveillance": "nope"}'])
def test_unusable_tier_file_fails_before_any_work(work, enrichment_tiers, content):
    enrichment_tiers.unlink() if content is None else enrichment_tiers.write_text(content)
    with pytest.raises(PipelineError) as error:
        work.run(make_settings(), "surveillance")
    assert error.value.stage == "enrichment_config" and runtime_worker._is_retryable_pipeline_error(error.value) is False
    assert (work.enrich, work.converted, work.commands, work.api) == ([], [], [], [])


def test_config_error_beats_a_source_url_download(work, enrichment_tiers, monkeypatch):
    enrichment_tiers.write_text("{}")
    for name in ("ingest_direct_audio_url", "_fetch_and_publish_url", "fetch_metadata"):
        monkeypatch.setattr(runtime_worker, name, lambda *a, **k: pytest.fail("downloaded before the configuration check"))
    with pytest.raises(PipelineError) as error:
        asyncio.run(runtime_worker.execute_runtime_job(
            make_settings(), None, {"sourceUrl": "https://example.com/a.mp3", "feed": "surveillance"}))
    assert error.value.stage == "enrichment_config"


@pytest.mark.parametrize("field", ["tier", "provider", "model"])
def test_roles_file_naming_a_model_for_a_transcript_role_fails_the_job(work, tmp_path, monkeypatch, field):
    (tmp_path / "roles.json").write_text(json.dumps({"transcript_topics": {field: "haiku"}}))
    monkeypatch.setenv("TTS_PI_ROLES_FILE", str(tmp_path / "roles.json"))
    with pytest.raises(PipelineError) as error:
        work.run(make_settings(), "audio")
    assert error.value.stage == "enrichment_config" and field in str(error.value) and work.enrich == [] and work.converted == []


def test_roles_file_redefining_the_chosen_tier_fails_the_job(work, tmp_path, monkeypatch):
    # the example tier file says surveillance -> sonnet; a "models" override of sonnet would move it to Codex
    (tmp_path / "roles.json").write_text(json.dumps({"models": {"sonnet": {"provider": "openai-codex", "model": "gpt-6-luna"}}}))
    monkeypatch.setenv("TTS_PI_ROLES_FILE", str(tmp_path / "roles.json"))
    with pytest.raises(PipelineError) as error:
        work.run(make_settings(), "surveillance")
    assert error.value.stage == "enrichment_config" and "sonnet" in str(error.value) and "models" in str(error.value)
    assert work.enrich == [] and work.converted == []
    # a tier the file does not choose may still be redefined (the readers use them)
    (tmp_path / "roles.json").write_text(json.dumps({"models": {"opus": {"model": "claude-opus-5-5"}}}))
    assert work.run(make_settings(), "surveillance", "other").slug


def test_a_tier_on_openai_codex_fails_the_job(work, enrichment_tiers):
    enrichment_tiers.write_text(json.dumps({"episodes": "luna", "surveillance": "sonnet"}))
    with pytest.raises(PipelineError) as error:
        work.run(make_settings(), "audio")
    assert error.value.stage == "enrichment_config" and "openai-codex" in str(error.value) and "X-60" in str(error.value)
    assert work.enrich == [] and work.converted == []


def test_a_configuration_error_in_speech_to_text_is_not_swallowed(monkeypatch, tmp_path):
    def broken(**kwargs):
        raise EnrichmentConfigError("bad tier")
    monkeypatch.setattr(episode_enrichment, "ensure_script", broken)
    with pytest.raises(PipelineError) as error:
        pipeline._transcribe(make_settings(), tmp_path / "a.mp3", tmp_path, "haiku")
    assert error.value.stage == "enrichment_config"
    # any other failure publishes the audio alone, as before
    monkeypatch.setattr(episode_enrichment, "ensure_script", lambda **kw: (_ for _ in ()).throw(RuntimeError("m3 down")))
    assert pipeline._transcribe(make_settings(), tmp_path / "a.mp3", tmp_path, "haiku") is None


def test_speech_to_text_without_a_tier_is_refused(tmp_path):
    with pytest.raises(PipelineError) as error:
        pipeline._transcribe(make_settings(), tmp_path / "a.mp3", tmp_path, None)
    assert error.value.stage == "enrichment_config"


def test_no_tier_file_is_needed_when_enrichment_is_off(work, enrichment_tiers):
    enrichment_tiers.unlink()
    published = work.run(make_settings(RSS_AUDIO_ENRICHMENT_ENABLED="0"), "surveillance")
    assert published.slug and work.enrich == [] and published.script_url is None


@pytest.mark.parametrize("timed, uploaded", [('{"schema": "tg-tools/timed-transcript/1"}', ["script", "segments"]),
                                            (None, ["script"])])
def test_publish_is_speech_to_text_first(work, timed, uploaded):
    work.script.timed_transcript = timed
    result = work.run(make_settings(), "surveillance")
    (payload,) = work.posts
    assert [name.rsplit(".", 2)[-2] for name in work.uploaded] == uploaded
    assert payload["speech_text_url"].endswith(".script.md")
    assert "md_url" not in payload and "manifest_url" not in payload and result.notes_url is None
    assert (result.segments_url is not None) == bool(timed)
    assert {k: result.notes[k] for k in ("inputKind", "feed", "slug", "version", "scriptSha256", "sttEngine")} == {
        "inputKind": "notes", "feed": "surveillance", "slug": result.slug, "version": 1, "scriptSha256": "a" * 64,
        "sttEngine": "parakeet-ultra@fluidaudio-0.18.0"}


def test_audio_alone_when_speech_to_text_fails(work):
    work.script = None
    result = work.run(make_settings(), "audio")
    (payload,) = work.posts
    assert work.uploaded == [] and "speech_text_url" not in payload and result.notes is None
