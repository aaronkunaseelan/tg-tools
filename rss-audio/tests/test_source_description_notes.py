"""The publisher's own description travels from the publish job to the notes prompt (A-17 round 2).

| notes_input(..., source_description=...) | the job's input                                |
| "x"                                      | carries sourceDescription: "x"                  |
| "" or None                               | no key at all, as before                        |
| the notes job, sections path             | enrich_script(source_description=<the input's>) |
"""
import json
import os
from types import SimpleNamespace
from unittest import mock

import notes_job
import pipeline
from config import load_settings

SLUG = "2026-11-03-bloomberg-surveillance-radio-27d0976312c4"
SCRIPT = "\n\n".join(f"Paragraph {n} of the show." for n in range(1, 5))
ENV = {"RSS_AUDIO_BASE_URL": "https://rss-feeds.stations.work", "RSS_AUDIO_B2_BUCKET": "bucket",
       "HOME": os.environ.get("HOME", "/tmp"), "RSS_AUDIO_B2_REMOTE": "b2"}


def settings():
    with mock.patch.dict(os.environ, ENV, clear=True):
        return load_settings()


def notes(**extra):
    return pipeline.notes_input(feed="surveillance", slug=SLUG, version=1, script_sha256="a" * 64,
                                title="Radio", published_at=None, date_basis="unknown", duration_seconds=None,
                                source_url=None, stt_engine="parakeet-ultra", **extra)


def test_notes_input_carries_source_description():
    assert notes(source_description="x")["sourceDescription"] == "x"
    assert "sourceDescription" not in notes()
    assert "sourceDescription" not in notes(source_description="")
    assert "sourceDescription" not in notes(source_description=None)


def test_notes_job_passes_source_description(tmp_path, monkeypatch):
    """The sections path hands the input's description to enrich_script, which cleans it for the prompts."""
    description = "Hosted by Jonathan Ferro.\nhttps://x.y/z\n07:41 Julian Emanuel, Evercore ISI"
    seen = {}

    def fake_enrich_script(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(notes_markdown="# notes", manifest=json.dumps({"analysis": {"segments": []}}),
                               description="d", topic_count=1, mentions={"topics": []})

    monkeypatch.setattr(notes_job, "enrich_script", fake_enrich_script)
    monkeypatch.setattr(notes_job, "_get_episode_by_slug",
                        lambda s, slug, feed: {"id": 9, "slug": slug, "title": "Radio", "mp3_url": "https://x/a.mp3"})
    monkeypatch.setattr(notes_job, "_finish_earlier", lambda *a, **k: None)
    monkeypatch.setattr(notes_job, "_stored_script", lambda *a, **k: SimpleNamespace(text=SCRIPT))
    monkeypatch.setattr(notes_job, "_upload_documents", lambda *a, **k: None)
    monkeypatch.setattr(notes_job, "_switch", lambda *a, **k: "switched")

    entry = {**notes(source_description=description)}
    assert notes_job.run_notes(settings(), entry, tmp_path / "w", "sonnet") == "switched"
    assert seen["source_description"] == entry["sourceDescription"] == description
    # an episode without one passes nothing, so its prompts keep the bytes they always had
    seen.clear()
    notes_job.run_notes(settings(), notes(), tmp_path / "v", "sonnet")
    assert seen["source_description"] is None
