"""surveillance_backfill.py: the fixed X-76 inventory, planned from the shipped sources (fakes only).

| case               | proves                                                                          |
| dry run            | 20 lines, nothing submitted; rebuild versions from each row (none -> v1,        |
|                    | script -> v2); radio carries its window's New York context, TV none             |
| captures           | 10/30 and 11/2 become local-file publishes with the timer's title, publishedAt,  |
|                    | segmenter and context, keyed <item>:local, priority below the scheduled ones     |
| a missing piece    | no row, or no capture: that item says why, the rest still submit, exit 1         |
| apply              | every payload submitted once per run, keys idempotent                            |
| TV days            | 14 shows 10/14-11/2 as the nightly's own items: key surveillance-tv:<video>,     |
|                    | timer title, feed publishedAt, segmenter surveillance-tv, priority 10            |
| --only             | a key, a day, a day range, a source-scoped range; a typo is an error, exit 1     |
| pages              | rows with notes and no page_url get a page job (no model call) at their notes'   |
|                    | version, keyed per episode slug;                                                 |
|                    | version; a row with another job in flight waits; no column yet: one waiting line |
| a second run       | an item whose job succeeded is done, one in flight is running: nothing submitted |
|                    | (not the next version); an item whose job died is planned again                  |
"""
import io
from pathlib import Path

import scheduled_sources as sched
import surveillance_backfill as backfill

SOURCES = sched.load_inventory(Path(__file__).resolve().parents[2] / "config" / "scheduled-sources.json")
BASE = "https://x/surveillance/episodes"


def row(title, slug, script=True, notes=True):
    return {"id": hash(slug) % 1000, "slug": slug, "title": title, "pub_date": "2026-11-03T12:00:00Z",
            "speech_text_url": f"{BASE}/{slug}.script.md" if script else None,
            "md_url": f"{BASE}/{slug}.notes.md" if notes else None, "mp3_url": f"{BASE}/{slug}.mp3"}


EPISODES = [row("Bloomberg Surveillance Radio 11/3/2026", "2026-11-03-radio", script=False, notes=False),
            row("Bloomberg Surveillance TV 11/3/2026", "2026-11-03-tv", script=False, notes=False),
            row("Bloomberg Surveillance Radio 10/28/2026", "2026-10-28-radio"),
            row("Bloomberg Surveillance Radio 10/29/2026", "2026-10-29-radio")]


def captures(tmp_path, days=("2026-10-30", "2026-11-02")):
    for day in days:
        (tmp_path / day).mkdir(parents=True)
        (tmp_path / day / f"radio-{day}.mp3").write_bytes(b"ID3" * 10)
    return tmp_path


def test_the_dry_run_plans_six_items_and_submits_nothing(tmp_path):
    out = io.StringIO()
    assert backfill.run(SOURCES, EPISODES, None, out, captures(tmp_path)) == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 22 and lines[-1] == "dry run: nothing submitted (--apply submits)"
    assert lines[0] == "pages    waiting: the episode API has no page_url column yet"   # these rows have none
    plan = {key: payload for key, _, payload, _ in backfill.plan(SOURCES, EPISODES, captures(tmp_path / "again"))}
    radio = plan["surveillance-radio:2026-11-03:rebuild"]["inputJson"]
    assert radio["version"] == 1 and radio["segmenter"] == "surveillance-radio"
    assert radio["segmentContext"] == {"start": "2026-11-03T12:00:00Z", "timezone": "America/New_York"}
    tv = plan["surveillance-tv:2026-11-03:rebuild"]["inputJson"]
    assert tv["version"] == 1 and tv["segmenter"] == "surveillance-tv" and "segmentContext" not in tv
    assert plan["surveillance-radio:2026-10-28:rebuild"]["inputJson"]["version"] == 2
    assert plan["surveillance-radio:2026-10-29:rebuild"]["producerRef"] == "surveillance-radio:2026-10-29:rebuild"
    assert all(p["priority"] == 10 for p in plan.values() if p is not None)


def test_captures_publish_like_the_timer_would_have(tmp_path):
    plan = {key: payload for key, _, payload, _ in backfill.plan(SOURCES, EPISODES, captures(tmp_path))}
    local = plan["surveillance-radio:2026-10-30:local"]
    job = local["inputJson"]
    assert local["idempotencyKey"] == "surveillance-radio:2026-10-30:local" and local["priority"] == 10
    assert job["title"] == "Bloomberg Surveillance Radio 10/30/2026" and job["publishedAt"] == "2026-10-30T11:00:00Z"
    assert job["segmentContext"] == {"start": "2026-10-30T11:00:00Z", "timezone": "America/New_York"}   # EDT
    assert job["localSourcePath"] == str(tmp_path / "2026-10-30" / "radio-2026-10-30.mp3")
    assert job["inputKind"] == "local_file" and "liveWindow" not in job and job["delivery"] == "none"
    assert plan["surveillance-radio:2026-11-02:local"]["inputJson"]["publishedAt"] == "2026-11-02T12:00:00Z"   # EST


def test_a_missing_row_or_capture_fails_that_item_only(tmp_path):
    submitted, out = [], io.StringIO()
    code = backfill.run(SOURCES, EPISODES[1:], lambda p: submitted.append(p) or {"job": {"jobId": "j"}}, out,
                        captures(tmp_path, days=("2026-10-30",)))
    assert code == 1
    text = out.getvalue()
    assert "cannot plan surveillance-radio:2026-11-03:rebuild: 0 episodes titled" in text
    assert "cannot plan surveillance-radio:2026-11-02:local: no capture at" in text
    assert len(submitted) == 18


def test_apply_submits_each_item_once(tmp_path):
    submitted = []
    assert backfill.run(SOURCES, EPISODES, lambda p: submitted.append(p) or {"job": {"jobId": "j", "status": "pending"}},
                        io.StringIO(), captures(tmp_path)) == 0
    keys = [p["idempotencyKey"] for p in submitted]
    assert len(keys) == len(set(keys)) == 20
    assert backfill.run(SOURCES, EPISODES, submitted.append, io.StringIO(), captures(tmp_path / "x"),
                        only=("surveillance-tv:2026-11-03:rebuild",)) == 0
    assert submitted[-1]["inputJson"]["slug"] == "2026-11-03-tv"


def test_a_second_run_after_a_finished_rebuild_submits_nothing(tmp_path):
    """The 4 Nov defect: after 3 Nov radio's v1 rebuild succeeded, --apply planned it again as v2."""
    key = "surveillance-radio:2026-11-03:rebuild"
    first = []
    backfill.run(SOURCES, EPISODES, first.append, io.StringIO(), captures(tmp_path), only=(key,))
    assert [p["inputJson"]["version"] for p in first] == [1]
    after = [dict(row("Bloomberg Surveillance Radio 11/3/2026", "2026-11-03-radio"))] + EPISODES[1:]   # now v1 + notes
    jobs = [{"jobId": "b-1", "producerRef": key, "status": "succeeded", "updatedAt": "2026-11-04T14:04:07Z"},
            {"jobId": "n-1", "producerRef": key + ":notes", "status": "succeeded"}]
    again, out = [], io.StringIO()
    assert backfill.run(SOURCES, after, again.append, out, captures(tmp_path / "2"), only=(key,), jobs=jobs) == 0
    assert again == [] and out.getvalue().startswith(f"done     {key}: job b-1 succeeded 2026-11-04T14:04, notes succeeded")
    jobs[0]["status"] = "leased"
    assert backfill.run(SOURCES, after, again.append, io.StringIO(), captures(tmp_path / "3"), only=(key,), jobs=jobs) == 0
    assert again == []
    jobs[0]["status"] = "dead"   # a dead rebuild left the row as it was: planned again, visibly
    backfill.run(SOURCES, EPISODES, again.append, io.StringIO(), captures(tmp_path / "4"), only=(key,), jobs=jobs)
    assert [p["idempotencyKey"] for p in again] == ["rss-audio:rebuild:2026-11-03-radio:v1"]


def test_the_tv_days_are_the_nightly_items(tmp_path):
    plan = {key: payload for key, _, payload, _ in backfill.plan(SOURCES, EPISODES, captures(tmp_path))}
    tv = [key for key in plan if key.startswith("surveillance-tv:") and not key.endswith(":rebuild")]
    assert len(tv) == 14 and tv[0] == "surveillance-tv:9bCiqNAyJT8" and tv[-1] == "surveillance-tv:JQZkM3iXIpw"
    job = plan["surveillance-tv:9bCiqNAyJT8"]
    assert job["idempotencyKey"] == job["producerRef"] == "surveillance-tv:9bCiqNAyJT8" and job["priority"] == 10
    assert job["inputJson"] == {
        "kind": "rss", "sourceKey": "surveillance-tv:9bCiqNAyJT8", "inputKind": "url", "feed": "surveillance",
        "title": "Bloomberg Surveillance TV 10/14/2026", "publishedAt": "2026-10-14T16:02:33Z", "delivery": "none",
        "segmenter": "surveillance-tv", "sourceUrl": "https://www.youtube.com/watch?v=9bCiqNAyJT8",
        "preferredRoute": "oci_mullvad"}
    # the nightly's own payload for the same entry: one definition, so the keys can never drift apart
    entry = sched.Entry("9bCiqNAyJT8", "Bloomberg Surveillance 10/14/2026", sched.datetime(2026, 10, 14, 16, 2, 33,
                                                                                           tzinfo=sched.UTC))
    assert sched.job_payload(sched.playlist_item(SOURCES[0], entry))["inputJson"] == job["inputJson"]


def keys_for(tmp_path, *only):
    return [key for key, _, payload, _ in backfill.plan(SOURCES, EPISODES, captures(tmp_path), only=only)]


def test_only_takes_keys_days_and_ranges(tmp_path):
    assert keys_for(tmp_path / "a", "surveillance-tv:2026-10-14..2026-10-16") == [
        "surveillance-tv:9bCiqNAyJT8", "surveillance-tv:kqIZbRqSLuU", "surveillance-tv:j2jSLruhTz8"]
    assert keys_for(tmp_path / "b", "2026-10-28..2026-10-29") == [   # any source in the range
        "surveillance-radio:2026-10-28:rebuild", "surveillance-radio:2026-10-29:rebuild",
        "surveillance-tv:KVyBp7SFrxg", "surveillance-tv:isJd6_0V7Bk"]
    assert keys_for(tmp_path / "c", "surveillance-tv:2026-11-02", "surveillance-radio:2026-11-03:rebuild") == [
        "surveillance-radio:2026-11-03:rebuild", "surveillance-tv:JQZkM3iXIpw"]
    out = io.StringIO()
    assert backfill.run(SOURCES, EPISODES, None, out, captures(tmp_path / "d"), only=("surveillance-tv:10/14",)) == 1
    assert out.getvalue().startswith("cannot plan surveillance-tv:10/14: not an item key")


def test_pages_for_rows_with_notes_and_no_page(tmp_path):
    rows = [dict(r, page_url=None) for r in EPISODES]
    rows[3]["page_url"] = f"{BASE}/2026-10-29-radio.page.html"   # 10/29 has its page
    jobs = [{"jobId": "t-1", "producerRef": "surveillance-tv:2026-11-03:rebuild", "status": "leased",
             "inputJson": {"title": "Bloomberg Surveillance TV 11/3/2026", "inputKind": "rebuild"}}]
    plan = {key: (line, payload) for key, line, payload, _ in backfill.plan(SOURCES, rows, captures(tmp_path), jobs=jobs)}
    pages = {key: value for key, value in plan.items() if key.startswith("surveillance-page:")}
    assert sorted(pages) == ["surveillance-page:2026-10-28-radio"]   # 11/3 rows have no notes; 10/29 has a page
    line, payload = pages["surveillance-page:2026-10-28-radio"]
    assert payload["idempotencyKey"] == "rss-audio:page:2026-10-28-radio:v1:l2" and payload["priority"] == 10
    assert payload["inputJson"]["inputKind"] == "page" and payload["producerRef"] == "surveillance-page:2026-10-28-radio"
    two = rows + [dict(rows[2], id=99, slug="2026-10-28-radio-b")]   # two episodes of one day: two items
    assert len([k for k, *_ in backfill.plan(SOURCES, two, captures(tmp_path / "two"), jobs=jobs)
                if k.startswith("surveillance-page:2026-10-28")]) == 2
    rows[1].update(md_url=f"{BASE}/2026-11-03-tv.notes.md")   # TV 11/3 has notes, but its rebuild is running
    plan = {key: (line, payload) for key, line, payload, _ in backfill.plan(SOURCES, rows, captures(tmp_path / "b"),
                                                                            jobs=jobs, only=("2026-11-03",))}
    assert plan["surveillance-page:2026-11-03-tv"] == (
        "waiting  Bloomberg Surveillance TV 11/3/2026: page after job t-1 (leased)", None)
