"""surveillance-tui: stage derivation from fixtures, unknown sources, and --once output (fakes only).

| case                 | proves                                                                          |
| published with notes | every stage done; upload names mp3+script+notes+manifest; feed row id           |
| script only          | notes failed "script only" (topics interrupted, speech_text_url, no md_url)      |
| running mid-STT      | opening split from STT; STT running with chunks n/m; later stages not reached    |
| dead skip            | opening skipped with the heard text; nothing after it is green                  |
| unknown sources      | a failed progress/B2/API read is unknown with its reason, never done or empty   |
| controller down      | --once prints unknown and exits 1                                              |
| --once               | list rows and one detail; no URL host, no bucket, paths only                    |
| naming               | expected_prefix is the pipeline's slug for live TV and radio items              |
| two-job path         | the newest notes job is the notes stage (over a row's old notes too): waiting,  |
|                      | running, retrying, failed, done; the                                            |
|                      | publish job stays the pipe; upload names segments and versions (.v<n>.)        |
| cancelled notes job  | a newer cancelled job is not the notes stage (the row's v1 notes stay done);    |
|                      | a newer pending/leased job is waiting/running; the detail still lists it        |
| edge cache           | 6 refreshes in 60 s read the episode API and B2 once; `r` (force) and 60 s read  |
|                      | again; jobs and progress are read every refresh; the age is reported            |
"""
import io
from datetime import UTC, datetime

import surveillance_tui as tui
from surveillance_tui import Item, Unknown, collect, feed_age, run_once, stages

NOW = datetime(2026, 11, 4, 11, 0, tzinfo=UTC)
RADIO_IN = {"feed": "surveillance", "kind": "rss", "title": "Bloomberg Surveillance Radio 11/3/2026",
            "delivery": "none", "inputKind": "live_window", "sourceKey": "surveillance-radio:2026-11-03",
            "publishedAt": "2026-11-03T12:00:00Z",
            "liveWindow": {"start": "2026-11-03T12:00:00Z", "seconds": 10800, "videoId": "f39oHo6vFLg",
                           "expectOpening": "Bloomberg Surveillance"}}
TV_IN = {"feed": "surveillance", "kind": "rss", "title": "Bloomberg Surveillance TV 11/3/2026", "delivery": "none",
         "inputKind": "url", "sourceKey": "surveillance-tv:KhnxubbrOrk", "publishedAt": "2026-11-03T16:45:18Z",
         "sourceUrl": "https://www.youtube.com/watch?v=KhnxubbrOrk", "preferredRoute": "oci_mullvad"}
RADIO_SLUG = "2026-11-03-bloomberg-surveillance-radio-27d0976312c4"
TV_SLUG = "2026-11-03-bloomberg-surveillance-tv-37d48d123b73"


def job(input_json, status="succeeded", error=None, text=None, job_id="j-1", attempts=1):
    return {"jobId": job_id, "producerRef": input_json["sourceKey"], "status": status, "attemptCount": attempts,
            "maxAttempts": 3, "lastErrorClass": error, "lastErrorText": text, "inputJson": input_json,
            "createdAt": "2026-11-04T08:00:00Z", "updatedAt": "2026-11-04T09:00:00Z",
            "leaseOwner": "rss-audio-runtime-worker" if status == "leased" else None,
            "leaseExpiresAt": "2026-11-04T12:00:00Z" if status == "leased" else None, "leaseToken": "SECRET-LEASE"}


def ev(stage, start, end, kind="completed", **facts):
    return {"stage": stage, "kind": kind, "startedAt": f"2026-11-04T{start}Z", "updatedAt": f"2026-11-04T{end}Z",
            "facts": facts}


WINDOW = [ev("check_publication", "08:01:00", "08:01:00"), ev("transcribe_chunks", "08:01:10", "08:01:15", total=1),
          ev("download_audio", "08:01:00", "08:03:00"), ev("convert_audio", "08:03:00", "08:05:00")]
STT = [ev("transcribe_chunks", "08:05:00", "08:05:01", "interrupted"), ev("split_audio", "08:05:01", "08:09:00"),
       ev("transcribe_chunks", "08:09:00", "08:10:00", total=18, completed=18),
       ev("transcript_ready", "08:05:00", "08:10:00")]
NOTES = [ev("prepare_paragraphs", "08:10:00", "08:10:00"), ev("topic_analysis", "08:10:00", "08:20:00"),
         ev("reference_extraction", "08:20:00", "08:25:00")]
PUBLISH = [ev("upload_audio", "08:25:00", "08:25:05"), ev("upload_document", "08:25:05", "08:25:06"),
           ev("publish_episode", "08:25:06", "08:25:07")]
ALL = ["mp3", "script.md", "notes.md", "manifest.json"]


def objects(slug, *exts):
    return [(f"{slug}.{ext}", 1000, "2026-11-04 08:25:05") for ext in exts]


def row(slug, title, md=True, script=True, row_id=257):
    base = f"https://edge.example/surveillance/episodes/{slug}"
    return {"id": row_id, "slug": slug, "title": title, "pub_date": "2026-11-03T12:00:00Z", "duration": "03:00:00",
            "mp3_size": 1000, "mp3_url": f"{base}.mp3", "md_url": f"{base}.notes.md" if md else None,
            "speech_text_url": f"{base}.script.md" if script else None,
            "manifest_url": f"{base}.manifest.json" if md else None}


def item(input_json, progress, episode=None, objs=(), **job_kw):
    return Item(input_json["title"], [job(input_json, **job_kw)], {"stages": progress}, episode, list(objs),
                RADIO_SLUG if "liveWindow" in input_json else TV_SLUG)


def states(it):
    return {s.name: (s.state, s.text) for s in stages(it, NOW)}


def test_published_with_notes_is_done_end_to_end():
    got = states(item(RADIO_IN, WINDOW + STT + NOTES + PUBLISH, row(RADIO_SLUG, RADIO_IN["title"]),
                      objects(RADIO_SLUG, *ALL)))
    assert {name: state for name, (state, _) in got.items()} == dict.fromkeys(tui.STAGES, "done")
    assert got["upload"][1] == "mp3+script+notes+manifest" and got["feed"][1] == "#257"
    assert got["opening"][1] == "5s" and got["stt"][1] == "5m00s azure" and got["queued"][1] == "1m00s"


def test_script_only_item_shows_failed_notes_and_a_published_script():
    notes = [ev("prepare_paragraphs", "08:10:00", "08:10:00"), ev("topic_analysis", "08:10:00", "08:40:00", "interrupted")]
    got = states(item(TV_IN, [ev("download_audio", "08:01:00", "08:03:00"), *STT[2:], *notes, *PUBLISH[:2]],
                      row(TV_SLUG, TV_IN["title"], md=False), objects(TV_SLUG, "mp3", "script.md")))
    assert got["notes"] == ("failed", "script only")
    assert got["opening"] == ("skipped", "n/a") and got["stt"] == ("done", "5m00s m3")
    assert got["upload"] == ("done", "mp3+script") and got["feed"][0] == "done"


def test_running_job_mid_stt():
    stt = [ev("split_audio", "08:05:00", "08:09:00"),
           ev("transcribe_chunks", "08:09:00", "08:10:00", "advanced", total=18, completed=3)]
    got = states(item(RADIO_IN, WINDOW + stt, None, (), status="leased"))
    assert got["opening"][0] == "done" and got["download"][0] == "done"
    assert got["stt"] == ("running", "2h55m 3/18")
    assert [got[n][0] for n in ("notes", "upload", "feed")] == ["pending"] * 3


def test_dead_skip_carries_what_was_heard_and_nothing_after_is_green():
    heard = "window opening does not say 'Bloomberg Surveillance': 'veterans day special coverage'"
    progress = [ev("transcribe_chunks", "08:01:10", "08:01:15", total=1),
                ev("download_audio", "08:01:00", "08:01:20", "interrupted")]
    it = item(RADIO_IN, progress, None, (), status="dead", error="window_not_show", text=heard)
    got = states(it)
    assert got["opening"] == ("skipped", "veterans day special coverage")
    assert {got[n][0] for n in ("download", "stt", "notes", "upload", "feed")} == {"skipped"}
    assert tui.summary(it.job) == "skipped"


def test_a_failed_source_is_unknown_with_its_reason_never_done():
    it = item(RADIO_IN, [], Unknown("episode API: GET failed"), ())
    it.progress, it.objects = Unknown("progress: timeout"), Unknown("B2: rclone lsf exit 1")
    got = states(it)
    for name in ("download", "opening", "stt", "notes"):
        assert got[name] == ("unknown", "progress: timeout")
    assert got["upload"] == ("unknown", "B2: rclone lsf exit 1") and got["feed"][0] == "unknown"
    assert "?window" in "".join(text for text, _ in tui.strip(it, now=NOW))


class Fake:
    def __init__(self, tmp_path, jobs, progress, episodes, objs, fail=()):
        self.tmp, self._jobs, self._progress, self._episodes, self._objs, self.fail = (
            tmp_path, jobs, progress, episodes, objs, fail)

    def _maybe(self, name, value):
        if name in self.fail:
            raise RuntimeError(f"{name} failed for bucket-xyz at https://host.example/k/SECRET")
        return value

    def jobs(self):
        return self._maybe("jobs", self._jobs)

    def progress(self, job_id):
        return self._maybe("progress", self._progress[job_id])

    def episodes(self):
        return self._maybe("episodes", self._episodes)

    def objects(self):
        return self._maybe("objects", self._objs)

    def workdir(self, job_id):
        return self.tmp / job_id

    def journal(self, since, until):
        return ["2026-11-04 08:01:10,000 transcribe-audio INFO Transcribing opening.m4a (2.0 MB) with self-hosted "
                "parakeet-ultra at http://10.0.0.1:8178/v1...",
                "2026-11-04 08:05:01,000 transcribe-audio INFO transcribing x.mp3 with Azure deployment gpt-4o-mini",
                "2026-11-04 08:05:02,000 noise INFO unrelated line"]

    def hide(self):
        return ("bucket-xyz",)


def test_once_prints_rows_and_a_detail_without_hosts_or_secrets(tmp_path):
    radio, tv = job(RADIO_IN, job_id="r-1"), job(TV_IN, job_id="t-1", status="leased")
    (tmp_path / "t-1" / "live").mkdir(parents=True)
    (tmp_path / "t-1" / "live" / "seg-1.bin").write_bytes(b"x" * 10)
    (tmp_path / "t-1" / "script.md").write_text("words")
    attempt = {"startedAt": "2026-11-04T08:01:00Z", "completedAt": None}
    sources = Fake(tmp_path, [tv, radio],
                   {"r-1": {"stages": WINDOW + STT + NOTES + PUBLISH, "attempt": attempt},
                    "t-1": {"stages": WINDOW[2:], "attempt": attempt}},
                   [row(RADIO_SLUG, RADIO_IN["title"])], objects(RADIO_SLUG, *ALL) + [("other.mp3", 1, "")],
                   fail=("objects",))
    out = io.StringIO()
    assert run_once(sources, out, key="radio", utf=False, now=NOW) == 0
    text = out.getvalue()
    lines = text.splitlines()
    assert lines[0] == "Surveillance, 2 items (times UTC)"
    assert lines[1].startswith("2026-11-03  tv     running") and lines[2].startswith("2026-11-03  radio  published")
    assert "+queued 1m00s > +window 4m00s > +opening 5s > +STT 5m00s azure > ?upload > +feed #257 > +notes" in lines[2]
    assert "B2: objects failed for <hidden> at <url>" in text
    assert f"md_url          /surveillance/episodes/{RADIO_SLUG}.notes.md" in text
    assert "STT engine  azure gpt-4o-mini (Azure)" in text and "opening heard  matched" in text
    assert "with self-hosted parakeet-ultra at <url>" in text and "unrelated" not in text
    for secret in ("edge.example", "bucket-xyz", "SECRET", "10.0.0.1"):
        assert secret not in text

    out = io.StringIO()
    run_once(sources, out, key="tv", utf=True, now=NOW)
    assert "live/  1 files  10 B" in out.getvalue() and "STT             script.md  5 B" in out.getvalue()


def test_once_with_the_controller_down_is_unknown_and_fails(tmp_path):
    out = io.StringIO()
    assert run_once(Fake(tmp_path, [], {}, [], [], fail=("jobs",)), out) == 1
    assert out.getvalue().startswith("Surveillance: unknown: controller: jobs failed for <hidden> at <url>")


def test_a_redo_is_the_same_item_and_shows_its_newest_job(tmp_path):
    redo = dict(job(RADIO_IN, job_id="r-2", status="pending", attempts=0), producerRef="x:redo",
                createdAt="2026-11-04T10:00:00Z")
    items = collect(Fake(tmp_path, [redo, job(RADIO_IN, job_id="r-1")], {"r-1": {}, "r-2": {}}, [], []))
    assert len(items) == 1 and items[0].job["jobId"] == "r-2" and len(items[0].jobs) == 2


def test_expected_prefix_is_the_pipeline_slug():
    assert tui.expected_prefix(RADIO_IN) == RADIO_SLUG
    assert tui.expected_prefix(TV_IN) == TV_SLUG


def notes_job(status="pending", attempts=0, error=None, job_id="n-1"):
    notes_in = {"kind": "rss", "inputKind": "notes", "feed": "surveillance", "slug": RADIO_SLUG, "version": 1,
                "title": RADIO_IN["title"], "delivery": "none"}
    return dict(job(dict(notes_in, sourceKey=RADIO_IN["sourceKey"] + ":notes"), status=status, error=error,
                    job_id=job_id, attempts=attempts), createdAt="2026-11-04T10:30:00Z")


def test_the_notes_job_is_the_notes_stage(tmp_path):
    published = row(RADIO_SLUG, RADIO_IN["title"], md=False)
    pipe = WINDOW + STT + PUBLISH
    docs = objects(RADIO_SLUG, "mp3", "script.md", "segments.json")
    for nj, progress, expected in [
            (notes_job(), {}, ("running", "waiting 30m00s")),
            (notes_job("leased", 1), {"stages": [ev("topic_analysis", "10:40:00", "10:50:00", "advanced")]},
             ("running", "20m00s")),
            (notes_job("retryable", 1, "notes"), {}, ("running", "retry 1/3 notes")),
            (notes_job("dead", 3, "notes"), {}, ("failed", "notes")),
            (notes_job("succeeded", 1), {"stages": NOTES}, ("done", "15m00s"))]:
        items = collect(Fake(tmp_path, [job(RADIO_IN, job_id="r-1"), nj], {"r-1": {"stages": pipe}, "n-1": progress},
                             [published], docs))
        assert len(items) == 1 and items[0].job["jobId"] == "r-1" and items[0].notes_job["jobId"] == "n-1"
        got = states(items[0])
        assert got["notes"] == expected, nj["status"]
        assert got["upload"] == ("done", "mp3+script+segments") and got["feed"] == ("done", "#257")
    # a rebuild's notes job is followed even while the row still has the old notes
    items = collect(Fake(tmp_path, [job(RADIO_IN, job_id="r-1"), notes_job("dead", 3, "notes")],
                         {"r-1": {"stages": pipe}, "n-1": {}}, [row(RADIO_SLUG, RADIO_IN["title"])], docs))
    assert states(items[0])["notes"] == ("failed", "notes")
    out = io.StringIO()
    run_once(Fake(tmp_path, [job(RADIO_IN, job_id="r-1"), notes_job("dead", 3, "notes")],
                  {"r-1": {"stages": pipe}, "n-1": {}}, [published], docs), out, key="radio", utf=False, now=NOW)
    assert "job n-1  surveillance-radio:2026-11-03:notes  dead" in out.getvalue()
    assert "last error (notes) notes:" in out.getvalue() and "  publish job progress" in out.getvalue()


def test_a_rebuilt_version_shows_in_upload():
    got = states(item(RADIO_IN, WINDOW + STT + PUBLISH, row(RADIO_SLUG, RADIO_IN["title"]),
                      objects(RADIO_SLUG, "mp3", "v2.script.md", "v2.segments.json", "v2.notes.md", "v2.manifest.json")))
    assert got["upload"] == ("done", "mp3+script+segments+notes+manifest v2")


def test_a_rebuild_is_the_item_newest_pipe_job_and_keeps_its_source(tmp_path):
    rebuild_in = {"kind": "rss", "inputKind": "rebuild", "feed": "surveillance", "slug": RADIO_SLUG, "version": 2,
                  "title": RADIO_IN["title"], "delivery": "none", "sourceKey": "surveillance-radio:2026-11-03:rebuild"}
    redo = dict(job(rebuild_in, job_id="b-1", status="leased"), createdAt="2026-11-04T10:00:00Z")
    items = collect(Fake(tmp_path, [job(RADIO_IN, job_id="r-1"), redo], {"r-1": {}, "b-1": {}}, [], []))
    assert len(items) == 1 and items[0].job["jobId"] == "b-1"
    assert items[0].prefix == RADIO_SLUG and items[0].source == "radio"
    items[0].progress = {"stages": STT[2:]}
    got = states(items[0])
    assert got["download"] == ("done", "stored mp3") and got["opening"] == ("skipped", "n/a")


def test_a_page_job_is_never_the_pipe(tmp_path):
    page_in = {"kind": "rss", "inputKind": "page", "feed": "surveillance", "slug": RADIO_SLUG, "version": 1,
               "title": RADIO_IN["title"], "delivery": "none", "sourceKey": f"surveillance-page:{RADIO_SLUG}"}
    page = dict(job(page_in, job_id="g-1", status="dead", error="page"), createdAt="2026-11-04T10:00:00Z")
    items = collect(Fake(tmp_path, [job(RADIO_IN, job_id="r-1"), page], {"r-1": {}, "g-1": {}}, [], []))
    assert items[0].job["jobId"] == "r-1" and [j["jobId"] for j in items[0].page_jobs] == ["g-1"]
    out = io.StringIO()
    run_once(Fake(tmp_path, [job(RADIO_IN, job_id="r-1"), page], {"r-1": {}, "g-1": {}}, [], []), out, key="radio",
             utf=False, now=NOW)
    assert f"job g-1  surveillance-page:{RADIO_SLUG}  dead" in out.getvalue()


def test_a_cancelled_newer_notes_job_is_not_the_notes_stage(tmp_path):
    v1 = row(RADIO_SLUG, RADIO_IN["title"])            # v1 notes are live on the row
    pipe = {"stages": WINDOW + STT + PUBLISH}
    cancelled = dict(notes_job("cancelled", 0, job_id="n-2"), createdAt="2026-11-04T10:45:00Z")
    sources = Fake(tmp_path, [job(RADIO_IN, job_id="r-1"), notes_job("succeeded", 1), cancelled],
                   {"r-1": pipe, "n-1": {"stages": NOTES}, "n-2": {}}, [v1], [])
    item_ = collect(sources)[0]
    assert item_.notes_job["jobId"] == "n-1" and [j["jobId"] for j in item_.notes_jobs] == ["n-1", "n-2"]
    assert states(item_)["notes"] == ("done", "15m00s")
    # the only notes job is cancelled: the row's notes decide
    only = collect(Fake(tmp_path, [job(RADIO_IN, job_id="r-1"), cancelled], {"r-1": pipe, "n-2": {}}, [v1], []))[0]
    assert only.notes_job is None and states(only)["notes"] == ("done", "")
    # a newer live job wins over the row's notes
    for status, attempts, expected in (("pending", 0, ("running", "waiting 15m00s")), ("leased", 1, ("running", ""))):
        live = dict(notes_job(status, attempts, job_id="n-3"), createdAt="2026-11-04T10:45:00Z")
        got = collect(Fake(tmp_path, [job(RADIO_IN, job_id="r-1"), notes_job("succeeded", 1), cancelled, live],
                           {"r-1": pipe, "n-1": {"stages": NOTES}, "n-2": {}, "n-3": {}}, [v1], []))[0]
        assert got.notes_job["jobId"] == "n-3" and states(got)["notes"] == expected, status
    out = io.StringIO()
    run_once(sources, out, key="radio", utf=False, now=NOW)
    assert "job n-2  surveillance-radio:2026-11-03:notes  cancelled" in out.getvalue()


class Counting(Fake):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.reads = {"jobs": 0, "progress": 0, "episodes": 0, "objects": 0}

    def jobs(self):
        self.reads["jobs"] += 1
        return super().jobs()

    def progress(self, job_id):
        self.reads["progress"] += 1
        return super().progress(job_id)

    def episodes(self):
        self.reads["episodes"] += 1
        return super().episodes()

    def objects(self):
        self.reads["objects"] += 1
        return super().objects()


def test_the_edge_is_read_once_a_minute_and_r_forces_it(tmp_path):
    sources = Counting(tmp_path, [job(RADIO_IN, status="leased", job_id="r-1")], {"r-1": {"stages": WINDOW}},
                       [row(RADIO_SLUG, RADIO_IN["title"])], [])
    clock, cache = [1000.0], {}
    now = lambda: clock[0]
    assert feed_age(cache, now) is None
    for n in range(6):                        # 6 refreshes, 10 s apart: t = 0 .. 50
        clock[0] = 1000.0 + 10 * n
        assert len(collect(sources, cache, clock=now)) == 1
    assert sources.reads == {"jobs": 6, "progress": 6, "episodes": 1, "objects": 1}   # a leased job's progress is live
    assert feed_age(cache, now) == 50
    clock[0] = 1055.0
    collect(sources, cache, clock=now)
    assert sources.reads["episodes"] == 1 and feed_age(cache, now) == 55
    collect(sources, cache, force=True, clock=now)          # r
    assert sources.reads["episodes"] == 2 and sources.reads["objects"] == 2 and feed_age(cache, now) == 0
    clock[0] = 1115.0                                       # 60 s after the forced read
    collect(sources, cache, clock=now)
    assert sources.reads["episodes"] == 3
    # a failed read is cached too, and shown as unknown with its reason
    failing = Counting(tmp_path, [job(RADIO_IN, job_id="r-1")], {"r-1": {}}, [], [], fail=("episodes",))
    cache = {}
    for n in range(3):
        clock[0] = 2000.0 + 10 * n
        got = collect(failing, cache, clock=now)[0]
    assert failing.reads["episodes"] == 1 and isinstance(got.episode, Unknown)
