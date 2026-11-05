"""Versioned documents (X-76 D1) and the feed-aware delete.

names_from_prefix(p, n): n >= 2 -> p.v<n>.script.md / .segments.json / .notes.md / .manifest.json; the mp3 never.
document_version: p.script.md -> 1, p.v3.notes.md -> 3, another prefix -> None.
delete_episode: finds the slug in any feed, deletes its row, then every <slug>.* object in that feed.
delete_documents(below_version=n): only documents of versions < n; the mp3 and version n stay.
"""
import os
from types import SimpleNamespace
from unittest import mock

import pytest

import pipeline
from config import load_settings
from episode_enrichment.naming import document_version, names_from_prefix

SLUG = "2026-11-03-bloomberg-surveillance-radio-27d0976312c4"
ENV = {"RSS_AUDIO_BASE_URL": "https://rss-feeds.stations.work", "RSS_AUDIO_B2_BUCKET": "bucket",
       "HOME": os.environ.get("HOME", "/tmp"), "RSS_AUDIO_B2_REMOTE": "b2"}


def settings():
    with mock.patch.dict(os.environ, ENV, clear=True):
        return load_settings()


def test_versioned_names():
    v1, v2 = names_from_prefix(SLUG), names_from_prefix(SLUG, 2)
    assert (v1.script_name, v2.script_name, v2.segments_name, v2.notes_name, v2.manifest_name, v2.audio_name) == (
        f"{SLUG}.script.md", f"{SLUG}.v2.script.md", f"{SLUG}.v2.segments.json", f"{SLUG}.v2.notes.md",
        f"{SLUG}.v2.manifest.json", f"{SLUG}.mp3")
    assert names_from_prefix(SLUG, 1) == v1
    assert [document_version(SLUG, n) for n in (f"https://x/surveillance/episodes/{SLUG}.script.md",
                                                f"{SLUG}.v3.notes.md", "other.v2.notes.md", None)] == [1, 3, None, None]


@pytest.fixture
def calls(monkeypatch):
    seen = SimpleNamespace(api=[], run=[], listed=[])
    monkeypatch.setattr(pipeline, "_get_episode_by_slug",
                        lambda s, slug, feed="audio": {"id": 7, "slug": slug} if feed == "surveillance" else None)
    monkeypatch.setattr(pipeline, "_api_json", lambda s, method, path, payload=None: seen.api.append((method, path)))

    def run(args, *, stage, timeout=None, cwd=None):
        seen.run.append(args)
        return SimpleNamespace(stdout="\n".join(seen.listed))
    monkeypatch.setattr(pipeline, "_run", run)
    return seen


def test_delete_finds_the_feed_and_removes_every_object(calls):
    pipeline.delete_episode(settings(), SLUG)
    assert calls.api == [("DELETE", "/api/episodes/7")]
    assert calls.run == [["rclone", "delete", "--include", f"{SLUG}.*", "b2:bucket/surveillance/episodes/"]]
    with pytest.raises(pipeline.PipelineError):
        pipeline.delete_episode(settings(), SLUG, feed="audio")


def test_old_versions_go_and_the_mp3_and_current_stay(calls):
    calls.listed = [f"{SLUG}.mp3", f"{SLUG}.script.md", f"{SLUG}.notes.md", f"{SLUG}.manifest.json",
                    f"{SLUG}.v2.script.md", f"{SLUG}.v2.segments.json", f"{SLUG}.v2.notes.md"]
    pipeline.delete_documents(settings(), SLUG, "surveillance", below_version=2)
    deleted = [args[3] for args in calls.run if args[1] == "delete"]
    assert deleted == [f"{SLUG}.script.md", f"{SLUG}.notes.md", f"{SLUG}.manifest.json"]
