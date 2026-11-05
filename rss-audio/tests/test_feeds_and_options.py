"""Feed table and per-job options (feed, publishedAt, title, delivery); fakes only, no network.

| input                                   | proves                                                                          |
| absent feed / options                   | feed audio: B2 audio/episodes/, API feed audio, ingestion pub_date, channel | title |
| feed surveillance + publishedAt + title | B2 surveillance/episodes/ (mp3 and documents), API body, exact-slug lookup, URLs |
|                                         | and keyed link follow the feed; pub_date and slug date are the given time; the  |
|                                         | title is the whole title                                                         |
| unknown feed / bad publishedAt / delivery | terminal `input` errors (never retried)                                       |
| episode already there                   | returned without fetching or uploading (live window: audio never fetched)       |
"""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest

import pipeline
import runtime_worker
from config import load_settings
from pipeline import PipelineError, apply_options, ingest_live_window, ingest_local_audio, job_options

ENV = {"RSS_AUDIO_BASE_URL": "https://rss-feeds.stations.work", "RSS_AUDIO_B2_BUCKET": "example-audio",
       "HOME": os.environ.get("HOME", "/tmp"), "RSS_AUDIO_B2_REMOTE": "b2"}
ROOT = "https://rss-feeds.stations.work/surveillance-" + "K" * 22
SURVEILLANCE = {"feed": "surveillance", "publishedAt": "2026-11-03T12:00:00Z",
                "title": "Bloomberg Surveillance Radio 11/3/2026"}


@pytest.fixture
def settings():
    with mock.patch.dict(os.environ, ENV, clear=True):
        return load_settings()


@pytest.fixture
def fake(monkeypatch):
    seen = SimpleNamespace(api=[], argv=[], existing=False, enrich=True, row={})

    def api(settings, method, path, payload=None):
        seen.api.append((method, path, payload))
        if method == "GET":
            query = parse_qs(urlsplit(path).query)
            return [{"slug": query["slug"][0], "title": "T", "mp3_url": "https://rss-feeds.stations.work/x.mp3",
                     **seen.row}] if seen.existing else []
        return {}

    def run(args, *, stage, timeout=None, cwd=None):
        seen.argv.append(args)
        if "json" in args:
            return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "audio"}], "format": {"duration": "60"}}))
        return SimpleNamespace(stdout="10799.6\n")

    monkeypatch.setattr(pipeline, "_api_json", api)
    monkeypatch.setattr(pipeline, "_run", run)
    monkeypatch.setattr(pipeline, "_convert_input_to_mp3", lambda s, source, out, stage: out.write_bytes(b"mp3"))
    monkeypatch.setattr(pipeline, "_transcribe", lambda *a: SimpleNamespace(
        text="s", timed_transcript="{}", sha256="a" * 64, stt_engine=None) if seen.enrich else None)
    return seen


def posted(seen):
    (payload,) = [p for method, _, p in seen.api if method == "POST"]
    return payload


def remotes(seen):
    return [args[-1] for args in seen.argv if "copy" in args]


def test_absent_options_are_todays_audio_feed(settings, fake, tmp_path):
    source = tmp_path / "talk.mp3"
    source.write_bytes(b"audio")
    result = ingest_local_audio(settings, source, "Talk", work_dir=tmp_path / "w")
    payload = posted(fake)
    assert payload["feed"] == "audio" and payload["title"] == "telegram | Talk"
    assert payload["pub_date"].endswith("Z") and not payload["pub_date"].startswith("2026-11-03T12")
    assert payload["mp3_url"] == f"https://rss-feeds.stations.work/audio/episodes/{result.slug}.mp3"
    assert remotes(fake) == ["b2:example-audio/audio/episodes/"] + ["b2:example-audio/audio/episodes"] * 2
    assert all(parse_qs(urlsplit(path).query)["feed"] == ["audio"] for _, path, _ in fake.api if "?" in path)


def test_surveillance_feed_follows_into_key_body_lookup_and_links(settings, fake, tmp_path, monkeypatch):
    source = tmp_path / "talk.mp3"
    source.write_bytes(b"audio")
    result = ingest_local_audio(settings, source, "Talk", work_dir=tmp_path / "w", options=job_options(SURVEILLANCE))
    payload = posted(fake)
    assert result.slug.startswith("2026-11-03-bloomberg-surveillance-radio-")
    assert (payload["feed"], payload["title"], payload["pub_date"]) == (
        "surveillance", "Bloomberg Surveillance Radio 11/3/2026", "2026-11-03T12:00:00Z")
    base = "https://rss-feeds.stations.work/surveillance/episodes/" + result.slug
    assert (payload["mp3_url"], payload["speech_text_url"], "md_url" in payload, "manifest_url" in payload) == (
        base + ".mp3", base + ".script.md", False, False)       # notes come from the notes job
    assert result.segments_url == base + ".segments.json" and result.notes["slug"] == result.slug
    assert remotes(fake) == ["b2:example-audio/surveillance/episodes/"] + ["b2:example-audio/surveillance/episodes"] * 2
    lookups = [path for method, path, _ in fake.api if method == "GET"]
    assert lookups and all(parse_qs(urlsplit(path).query)["feed"] == ["surveillance"] for path in lookups)
    monkeypatch.setenv("SURVEILLANCE_PUBLIC_ROOT", ROOT)
    message = pipeline.render_completion_message(result.title, result.public_url, notes_url=result.notes_url,
                                                 script_url=result.script_url, feed="surveillance")
    assert f'href="{ROOT}/episodes/{result.slug}.mp3"' in message and "/surveillance/episodes/" not in message


def test_title_and_date_options_reach_the_episode_record(settings):
    metadata = pipeline.EpisodeMetadata("Original", "Channel", "d", 5, "Unknown", creators=("A",), series="S")
    changed = apply_options(metadata, job_options(SURVEILLANCE))
    assert (changed.title, changed.channel, changed.creators, changed.series) == (
        "Bloomberg Surveillance Radio 11/3/2026", "", (), "")
    assert (changed.source_published_at, changed.date_basis, changed.pub_date) == (
        "2026-11-03T12:00:00Z", "scheduled_source", "2026-11-03T12:00:00Z")
    assert apply_options(metadata, job_options({})) == metadata
    # an offset time is normalised to UTC
    assert job_options({"publishedAt": "2026-11-03T07:00:00-05:00"}).published_at == "2026-11-03T12:00:00Z"


@pytest.mark.parametrize("bad", [{"feed": "nope"}, {"feed": None}, {"publishedAt": "2026-11-03"}, {"publishedAt": "yesterday"},
                                 {"publishedAt": 5}, {"title": "  "}, {"title": 3}, {"delivery": "email"}])
def test_bad_options_are_terminal_input_errors(bad):
    with pytest.raises(PipelineError) as err:
        job_options(bad)
    assert err.value.stage == "input" and runtime_worker._is_retryable_pipeline_error(err.value) is False


def test_unknown_feed_fails_the_job_before_any_work(settings):
    with pytest.raises(PipelineError) as err:
        asyncio.run(runtime_worker.execute_runtime_job(settings, None, {"feed": "nope", "sourceUrl": "https://example.com/a.mp3"}))
    assert err.value.stage == "input" and "nope" in str(err.value)


def test_existing_episode_is_returned_without_fetching_or_uploading(settings, fake, tmp_path):
    fake.existing = True
    fetched = []
    result = ingest_live_window(settings, source_url="https://www.youtube.com/watch?v=f39oHo6vFLg",
                                identity_text="f39oHo6vFLg@2026-11-03T12:00:00Z", fetch_audio=lambda d: fetched.append(d),
                                work_dir=tmp_path / "w", options=job_options(SURVEILLANCE))
    assert fetched == [] and fake.argv == [] and not [1 for method, _, _ in fake.api if method == "POST"]
    assert result.slug.startswith("2026-11-03-bloomberg-surveillance-radio-")


def test_a_read_back_row_with_a_script_and_no_notes_resubmits_its_notes(settings, fake, tmp_path):
    """A lost notes submit: the retry reads the row back and carries the same request, with the engine the
    first attempt checkpointed in this job's work dir (unknown when the work dir is gone)."""
    fake.existing = True
    fake.row = {"speech_text_url": "https://rss-feeds.stations.work/surveillance-x/episodes/s.script.md"}
    run = lambda work: ingest_live_window(
        settings, source_url="https://www.youtube.com/watch?v=f39oHo6vFLg", identity_text="f39oHo6vFLg@2026-11-03T12:00:00Z",
        fetch_audio=lambda d: None, work_dir=work, options=job_options(SURVEILLANCE))
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "stt-engine.txt").write_text("parakeet-ultra@fluidaudio-0.18.0\n")
    notes = run(tmp_path / "w").notes
    assert notes["inputKind"] == "notes" and notes["version"] == 1 and notes["scriptSha256"] is None
    assert notes["sttEngine"] == "parakeet-ultra@fluidaudio-0.18.0"
    assert run(tmp_path / "swept").notes["sttEngine"] is None
    fake.row["md_url"] = "https://rss-feeds.stations.work/surveillance-x/episodes/s.notes.md"
    assert run(tmp_path / "w").notes is None


def test_live_window_identity_is_the_window_and_publishes_with_its_duration(settings, fake, tmp_path):
    def audio(staging):
        path = Path(staging) / "window.m4a"
        path.write_bytes(b"m4a")
        return path

    def once(start):
        fake.api.clear()
        return ingest_live_window(settings, source_url="https://www.youtube.com/watch?v=f39oHo6vFLg",
                                  identity_text=f"f39oHo6vFLg@{start}", fetch_audio=audio, work_dir=tmp_path / start,
                                  options=job_options(SURVEILLANCE))
    first, again, other = once("2026-11-03T12:00:00Z"), once("2026-11-03T12:00:00Z"), once("2026-11-04T12:00:00Z")
    assert first.slug == again.slug != other.slug
    assert posted(fake)["duration"] == "03:00:00" and posted(fake)["feed"] == "surveillance"
