"""The notes job (rss-audio/notes_job.py): notes written after the episode is live.

| stored                                  | job                 | outcome                                                   |
| script + its segments.json, v1          | a notes job         | notes + manifest uploaded once; PUT md_url, manifest_url, description |
| the same, v2                            |                     | PUT also moves speech_text_url; versions below 2 deleted after the PUT |
| segments.json of another script         |                     | notes still written; manifest timing says why it was not used |
| a script that is not the submitted one  |                     | terminal error, nothing uploaded or PUT                     |
| -                                       | notesKind sections  | one call per structure block, sections rendered, checks in the job |
| no episode row                          |                     | terminal error                                              |
notes_job_payload: idempotent on (slug, version), producer ref extends the parent's, delivery none.
runtime_worker._submit_notes: a failed submit retries the parent.
page: the notes job uploads <prefix>[.v<n>].page.html, overwritable, and PUTs page_url (an API that drops it is
a warning); a page that fails is left out of the PUT, never blocking the notes (a lost lease is not swallowed);
run_page builds it from stored notes + script with no model call, and fails if the API does not store it.
retries: after the uploads, a retry PUTs the stored notes and manifest (no model call, even after a restart
with a model that would answer differently); notes without their manifest are terminal (rebuild); a failed
read of the script or segments is retryable, never "missing"; a missing script is terminal.
"""
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import notes_job
import pipeline
from config import load_settings
from episode_enrichment import timing

SLUG = "2026-11-03-bloomberg-surveillance-radio-27d0976312c4"
SCRIPT = "\n\n".join(f"Paragraph {n} of the show." for n in range(1, 5))
SHA = hashlib.sha256(SCRIPT.encode()).hexdigest()
ENV = {"RSS_AUDIO_BASE_URL": "https://rss-feeds.stations.work", "RSS_AUDIO_B2_BUCKET": "bucket",
       "HOME": os.environ.get("HOME", "/tmp"), "RSS_AUDIO_B2_REMOTE": "b2"}


def settings():
    with mock.patch.dict(os.environ, ENV, clear=True):
        return load_settings()


class Runner:
    def __init__(self, calls, variant=""):
        self.calls, self.variant = calls, variant   # a variant answers differently, as a re-run model does

    def run(self, prompt, allow_tools=False, timeout_s=None, session_name=""):
        ids = re.findall(r"^\[(p\d{5})\]", prompt, re.M)
        self.calls.append((session_name, ids[0], ids[-1]))
        heading = f"{self.variant}Topic {ids[0]}"
        body = ({"summary": f"About {ids[0]}.",
                 "mainPoints": [{"text": f"Doe says {ids[0]} was measured.", "paragraphId": ids[0]},
                                {"text": f"Doe says {ids[-1]} stays uncertain.", "paragraphId": ids[-1]}],
                 "topics": [{"heading": heading,
                             "lede": "Doe says the show measured the finding and the number is larger than "
                                     "the earlier one.",
                             "startParagraphId": ids[0], "endParagraphId": ids[-1]}]}
                if "topics" in session_name else {"topics": [{"heading": heading, "mentions": []}]})
        return SimpleNamespace(ok=True, text=json.dumps(body), error=None)


@pytest.fixture
def store(monkeypatch, tmp_path):
    words = [{"word": w, "start": float(i), "end": i + 0.5} for i, w in enumerate(SCRIPT.split())]
    timed, _ = timing.build(SCRIPT, timing.from_self_hosted({"words": words, "segments": None}))
    seen = SimpleNamespace(objects={f"{SLUG}.script.md": SCRIPT, f"{SLUG}.segments.json": timed,
                                    f"{SLUG}.v2.script.md": SCRIPT, f"{SLUG}.v2.segments.json": timed},
                           uploaded={}, api=[], deleted=[], calls=[], unreadable=set(), put_fails=0, page_fails=False,
                           episode={"id": 9, "slug": SLUG, "title": "Radio", "mp3_url": "https://x/a.mp3",
                                    "speech_text_url": f"https://rss-feeds.stations.work/surveillance/episodes/{SLUG}.script.md"})

    def run(args, *, stage, timeout=None, cwd=None):   # the store, as rclone sees it
        name = args[4] if args[1] == "lsf" else args[2].rsplit("/", 1)[-1]
        if name in seen.unreadable:
            raise pipeline.PipelineError(stage, "rclone: connection reset")
        if args[1] == "lsf":
            return SimpleNamespace(stdout=name + "\n" if name in seen.objects else "")
        if name not in seen.objects:
            raise pipeline.PipelineError(stage, "not found")
        Path(args[3]).write_text(seen.objects[name], encoding="utf-8")
        return SimpleNamespace(stdout="")

    def upload(s, path, key):   # immutable, like the real helper: changed bytes are refused
        name, text = key.rsplit("/", 1)[-1], path.read_text()
        if name in seen.objects and seen.objects[name] != text:
            raise pipeline.PipelineError("upload", f"{name} differs: immutable file modified")
        seen.objects[name] = seen.uploaded[name] = text

    def api(s, method, path, payload=None):
        if seen.put_fails:
            seen.put_fails -= 1
            raise pipeline.PipelineError("api", "edge 502", retryable=True)
        seen.api.append((method, path, payload))
    monkeypatch.setattr(notes_job, "_run", run)
    monkeypatch.setattr(notes_job, "_upload_artifact", upload)

    def overwrite(s, path, key):   # the page: derived, replaced freely
        if seen.page_fails:
            raise pipeline.PipelineError("upload", "rclone: connection reset")
        name = key.rsplit("/", 1)[-1]
        seen.objects[name] = seen.uploaded[name] = path.read_text()
    monkeypatch.setattr(notes_job, "_overwrite", overwrite)
    monkeypatch.setattr(notes_job, "_get_episode_by_slug", lambda s, slug, feed: seen.episode)
    monkeypatch.setattr(notes_job, "_api_json", api)
    monkeypatch.setattr(notes_job, "delete_documents",
                        lambda s, slug, feed, below_version: seen.deleted.append((slug, feed, below_version)))
    seen.variant = ""
    seen.run = lambda version=1, sha=SHA, work="w", **extra: notes_job.run_notes(
        settings(), notes_job.notes_input(feed="surveillance", slug=SLUG, version=version, script_sha256=sha,
                                          title="Radio", published_at="2026-11-03T12:00:00Z", date_basis="scheduled_source",
                                          duration_seconds=10800, source_url=None, stt_engine="parakeet-ultra", **extra),
        tmp_path / f"{work}{version}", "sonnet", runner_factory=lambda role: Runner(seen.calls, seen.variant))
    return seen


def test_notes_are_written_once_and_the_episode_switched(store):
    result = store.run()
    assert sorted(store.uploaded) == [f"{SLUG}.manifest.json", f"{SLUG}.notes.md", f"{SLUG}.page.html"]
    ((method, path, change),) = store.api
    assert (method, path, sorted(change)) == ("PUT", "/api/episodes/9", ["description", "manifest_url", "md_url",
                                                                          "page_url"])
    assert change["page_url"].endswith(f"/surveillance/episodes/{SLUG}.page.html") and result.page_url == change["page_url"]
    assert change["md_url"].endswith(f"/surveillance/episodes/{SLUG}.notes.md") and store.deleted == []
    manifest = json.loads(store.uploaded[f"{SLUG}.manifest.json"])
    assert manifest["script"]["timing"]["basis"] == "words" and manifest["artifacts"]["segmentsUrl"].endswith(".segments.json")
    assert manifest["script"]["sttModel"] == "parakeet-ultra" and result.notes_url == change["md_url"]
    # an episode's notes are the section shape, and their form gates travel in the manifest (X-104)
    notes = store.uploaded[f"{SLUG}.notes.md"]
    assert "## Main points" in notes and "### 0:00 · Topic p00001" in notes
    assert "Doe says the show measured the finding" in notes
    assert set(manifest["gates"]["notes"]["stats"]) >= {"sections", "ledesMissing", "quotesOutOfRange"}


def test_a_rebuilt_version_moves_the_script_and_drops_older_documents(store):
    store.run(version=2)
    ((_, _, change),) = store.api
    assert change["speech_text_url"].endswith(f"{SLUG}.v2.script.md") and change["md_url"].endswith(f"{SLUG}.v2.notes.md")
    assert sorted(store.uploaded) == [f"{SLUG}.v2.manifest.json", f"{SLUG}.v2.notes.md", f"{SLUG}.v2.page.html"]
    assert change["page_url"].endswith(f"{SLUG}.v2.page.html")
    assert store.deleted == [(SLUG, "surveillance", 2)]


def test_a_rebuilt_v1_switches_its_script_with_its_notes(store):   # the row had no script yet (3 Nov)
    store.episode = {k: v for k, v in store.episode.items() if k != "speech_text_url"}
    store.run()
    ((_, _, change),) = store.api
    assert sorted(change) == ["description", "manifest_url", "md_url", "page_url", "speech_text_url"]
    assert store.deleted == []


def test_segments_of_another_script_are_not_used(store):
    other = json.loads(store.objects[f"{SLUG}.segments.json"])
    other["scriptSha256"] = "0" * 64
    store.objects[f"{SLUG}.segments.json"] = json.dumps(other)
    store.run()
    manifest = json.loads(store.uploaded[f"{SLUG}.manifest.json"])
    assert manifest["script"]["timing"] == {"basis": None, "reason": "the stored segments.json belongs to another script"}
    assert "segmentsUrl" not in manifest["artifacts"]


def test_a_script_that_is_not_the_submitted_one_is_refused(store):
    with pytest.raises(pipeline.PipelineError) as error:
        store.run(sha="f" * 64)
    assert error.value.retryable is False and store.uploaded == {} and store.api == []


def test_no_episode_is_terminal(store):
    store.episode = None
    with pytest.raises(pipeline.PipelineError) as error:
        store.run()
    assert error.value.retryable is False


def test_the_submit_payload():
    notes = notes_job.notes_input(feed="audio", slug=SLUG, version=1, script_sha256=SHA, title="T", published_at=None,
                                  date_basis="unknown", duration_seconds=None, source_url=None, stt_engine=None)
    payload = notes_job.notes_job_payload({"producerRef": "surveillance-radio:2026-11-03"}, notes)
    flat = json.dumps(payload)
    assert f"rss-audio:notes:{SLUG}:v1" in flat and "surveillance-radio:2026-11-03:notes" in flat
    assert notes["delivery"] == "none" and notes["inputKind"] == "notes"
    # notes run just below their parent: Telegram 60 -> 55, scheduled 20 -> 15, never below 0
    assert [notes_job.notes_job_payload({"producerRef": "p", "priority": n}, notes)["priority"]
            for n in (60, 20, 3)] == [55, 15, 0]


def test_the_worker_runs_a_notes_input_as_a_notes_job(monkeypatch, tmp_path):
    import asyncio
    import runtime_worker
    seen = []

    def fake(s, notes, work_dir, tier):
        seen.append((notes["slug"], tier))
        return pipeline.PublishResult(slug=SLUG, title="Radio", public_url="https://x/a.mp3", file_size_bytes=1,
                                      notes_url="https://x/n.md")
    monkeypatch.setattr(notes_job, "run_notes", fake)
    notes = notes_job.notes_input(feed="surveillance", slug=SLUG, version=1, script_sha256=SHA, title="Radio",
                                  published_at=None, date_basis="unknown", duration_seconds=None, source_url=None,
                                  stt_engine=None)
    result = asyncio.run(runtime_worker.execute_runtime_job(settings(), None, notes, tmp_path))
    assert seen == [(SLUG, "sonnet")] and result.notes_url == "https://x/n.md"


def test_a_notes_job_with_a_bare_extractor_date_runs(monkeypatch, tmp_path):
    # notes 21cb2f8e (21 Nov) died 'input: publishedAt must carry a UTC offset, got '2026-11-19'': a URL
    # publish passes yt-dlp's upload_date, a bare date, as provenance; it is not a publish option.
    import asyncio
    import runtime_worker
    seen = []
    monkeypatch.setattr(notes_job, "run_notes", lambda s, notes, work_dir, tier: seen.append(notes["publishedAt"])
                        or pipeline.PublishResult(slug=SLUG, title="Call", public_url="https://x/a.mp3",
                                                  file_size_bytes=1, notes_url="https://x/n.md"))
    notes = notes_job.notes_input(feed="audio", slug=SLUG, version=1, script_sha256=SHA, title="Call",
                                  published_at="2026-11-19", date_basis="extractor_upload_date",
                                  duration_seconds=3715, source_url=None, stt_engine=None)
    asyncio.run(runtime_worker.execute_runtime_job(settings(), None, notes, tmp_path))
    assert seen == ["2026-11-19"]
    with pytest.raises(pipeline.PipelineError, match="UTC offset"):   # a publish job still needs one
        pipeline.job_options({"publishedAt": "2026-11-19"})

def test_the_publish_job_submits_its_notes_job():
    import runtime_worker
    notes = notes_job.notes_input(feed="surveillance", slug=SLUG, version=1, script_sha256=SHA, title="T",
                                  published_at=None, date_basis="unknown", duration_seconds=None,
                                  source_url=None, stt_engine=None)
    parent = {"producerRef": "surveillance-radio:2026-11-03"}
    submitted = []
    client = SimpleNamespace(submit_job=lambda payload: submitted.append(payload) or {"job": {"jobId": "n-1"}})
    job_id = runtime_worker._submit_notes(client, parent, notes)
    assert job_id == "n-1" and "segmenter" not in json.dumps(submitted)

    def refused(payload):
        raise RuntimeError("controller down")
    with pytest.raises(pipeline.PipelineError) as error:
        runtime_worker._submit_notes(SimpleNamespace(submit_job=refused), parent, notes)
    assert error.value.stage == "notes_submit" and error.value.retryable is True


@pytest.mark.parametrize("version", [1, 2])
def test_a_retry_after_the_uploads_finishes_the_earlier_attempt(store, version):
    """The PUT fails once; a restart swept the work dir; the re-run model would answer differently.
    The retry PUTs the first attempt's stored pair and its description, with no model call."""
    store.put_fails = 1
    with pytest.raises(pipeline.PipelineError) as error:
        store.run(version=version)
    assert error.value.retryable is True and store.api == []
    first = {name: text for name, text in store.objects.items() if name.endswith((".notes.md", ".manifest.json",
                                                                                 ".page.html"))}
    assert any(name.endswith(".page.html") for name in first)   # the page went up before the failed PUT
    calls, store.variant = len(store.calls), "Different "
    result = store.run(version=version, work="swept")
    assert len(store.calls) == calls   # zero new calls
    ((method, _, change),) = store.api
    assert method == "PUT" and change["description"].startswith("About p00001.")
    assert change["md_url"] == result.notes_url and change["manifest_url"] == result.manifest_url
    assert {name: text for name, text in store.objects.items() if name.endswith((".notes.md", ".manifest.json",
                                                                               ".page.html"))} == first
    assert change["page_url"].endswith(".page.html")
    assert store.deleted == ([(SLUG, "surveillance", 2)] if version == 2 else [])


def test_notes_stored_without_their_manifest_are_terminal(store):
    store.objects[f"{SLUG}.notes.md"] = "notes of an attempt that stopped between its uploads"
    with pytest.raises(pipeline.PipelineError) as error:
        store.run()
    assert error.value.retryable is False and "rebuild the episode" in str(error.value)
    assert store.api == [] and store.calls == []


def test_a_failed_read_is_retryable_and_never_passes_for_missing(store):
    store.unreadable.add(f"{SLUG}.segments.json")
    with pytest.raises(pipeline.PipelineError) as error:
        store.run()
    assert error.value.retryable is True and store.uploaded == {} and store.api == []
    store.unreadable = {f"{SLUG}.script.md"}
    with pytest.raises(pipeline.PipelineError) as error:
        store.run()
    assert error.value.retryable is True


def test_a_missing_script_is_terminal_and_missing_segments_run_untimed(store):
    del store.objects[f"{SLUG}.segments.json"]
    store.run()
    manifest = json.loads(store.uploaded[f"{SLUG}.manifest.json"])
    assert manifest["script"]["timing"] == {"basis": None, "reason": "no segments.json was stored"}
    del store.objects[f"{SLUG}.v2.script.md"]
    with pytest.raises(pipeline.PipelineError) as error:
        store.run(version=2)
    assert error.value.retryable is False


def test_the_page_is_the_notes_then_the_script(store):
    store.run()
    page = store.uploaded[f"{SLUG}.page.html"]
    assert page.startswith("<!doctype html>") and "<h1>Radio</h1>" in page and '<h2 id="script">Script</h2>' in page
    assert page.index("<h2>Summary</h2>") < page.index('id="p00001"')   # notes first
    assert '<p id="p00004"><a class="t" href="#p00004">0:00:' in page   # timed: the paragraph's start
    assert '<a href="#p00001">p00001</a>' in page


def test_the_page_alone_from_stored_notes_with_no_model_call(store, tmp_path, monkeypatch):
    store.run()
    notes_page = store.uploaded.pop(f"{SLUG}.page.html")
    del store.objects[f"{SLUG}.page.html"]
    calls, store.api = len(store.calls), []
    store.episode.update(md_url=f"https://x/surveillance/episodes/{SLUG}.notes.md")
    def api(s, method, path, payload=None):
        store.api.append((method, path, payload))
        return {**store.episode, **payload}   # an API with the page_url column
    monkeypatch.setattr(notes_job, "_api_json", api)
    result = notes_job.run_page(settings(), notes_job.page_input(store.episode, "surveillance"), tmp_path / "p")
    assert len(store.calls) == calls and store.uploaded[f"{SLUG}.page.html"] == notes_page   # same bytes
    assert store.api == [("PUT", "/api/episodes/9", {"page_url": result.page_url})]


def test_the_page_job_refuses_what_it_cannot_link(store, tmp_path):
    store.run()
    store.episode.update(md_url=f"https://x/surveillance/episodes/{SLUG}.notes.md")
    with pytest.raises(pipeline.PipelineError) as error:   # the stub API returns no row: page_url not stored
        notes_job.run_page(settings(), notes_job.page_input(store.episode, "surveillance"), tmp_path / "p")
    assert error.value.retryable is False and "did not store page_url" in str(error.value)
    store.episode.update(speech_text_url=f"https://x/surveillance/episodes/{SLUG}.v2.script.md")
    with pytest.raises(pipeline.PipelineError) as error:   # notes v1, script v2
        notes_job.run_page(settings(), notes_job.page_input(store.episode, "surveillance"), tmp_path / "q")
    assert error.value.retryable is False and "notes v1 and script v2" in str(error.value)


def test_the_page_job_payload():
    payload = notes_job.page_job_payload({"slug": SLUG, "version": 2, "feed": "surveillance", "inputKind": "page"},
                                         producer_ref="surveillance-radio:2026-11-03:page")
    assert payload["idempotencyKey"] == f"rss-audio:page:{SLUG}:v2:l2" and payload["priority"] == 10


def test_a_failed_page_never_blocks_the_notes_switch(store):
    store.page_fails = True
    result = store.run()
    ((_, _, change),) = store.api
    assert "page_url" not in change and change["md_url"].endswith(".notes.md") and result.page_url is None
    assert f"{SLUG}.page.html" not in store.uploaded


def test_the_page_is_replaced_not_refused(store, tmp_path, monkeypatch):
    store.objects[f"{SLUG}.page.html"] = "<p>an older renderer's page</p>"
    store.run()
    assert store.objects[f"{SLUG}.page.html"].startswith("<!doctype html>")
    ((_, _, change),) = store.api
    assert change["page_url"].endswith(f"{SLUG}.page.html")


def test_lease_loss_is_not_swallowed_as_a_page_failure(store, monkeypatch):
    from tg_tools_common.lease import LeaseLost

    def lost(*a, **k):
        raise LeaseLost("lease lost")
    monkeypatch.setattr(notes_job, "_overwrite", lost)
    with pytest.raises(LeaseLost):
        store.run()
    assert store.api == []


def test_the_notes_input_carries_the_speaker_the_metadata_names():
    """One speaker, when the metadata names a person: the title if it is exactly a name, else a creator."""
    named = pipeline.EpisodeMetadata("Jane Doe", "", "", 0, "Unknown")
    assert pipeline.episode_speaker(named) == {"name": "Jane Doe"}
    assert pipeline.notes_input(feed="audio", slug=SLUG, version=1, script_sha256=SHA, title="Jane Doe",
                                published_at=None, date_basis="unknown", duration_seconds=None, source_url=None,
                                stt_engine=None, speaker={"name": "Jane Doe"})["speaker"] == {"name": "Jane Doe"}
    # a show's own name is not a speaker, and a creator is only one when it is not the channel or the series
    assert pipeline.episode_speaker(pipeline.EpisodeMetadata("Interviews from the field", "", "", 0, "Unknown")) is None
    assert pipeline.episode_speaker(pipeline.EpisodeMetadata(
        "On shipping a company", "TBPN", "", 0, "Unknown", creators=("Travis K",))) == {"name": "Travis K"}
    assert pipeline.episode_speaker(pipeline.EpisodeMetadata(
        "On shipping a company", "TBPN", "", 0, "Unknown", creators=("TBPN",))) is None


def test_a_title_that_is_really_a_show_names_nobody():
    """Two capitalised words can be a person or a show: the transcript decides, and silence is safe."""
    assert notes_job._named_in({"name": "Jane Doe"}, "Jane Doe says the bottle shed particles.") == {"name": "Jane Doe"}
    assert notes_job._named_in({"name": "Hard Fork"}, "The show begins with a question about chips.") is None
    assert notes_job._named_in(None, "anything") is None
