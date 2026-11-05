"""Scheduled sources: expected items, idempotent submission, per-item report and exit status (fakes only).

| case                                  | proves                                                                      |
| shipped inventory                     | the two Surveillance sources as specified, valid; an unknown segmenter is  |
|                                       | rejected; each item carries its segmenter (radio: its window's clock)      |
| playlist                              | title regex, since, lookback; title M/D/YYYY; publishedAt = Atom published |
| live window                           | weekdays only, 07:00 America/New_York across the 1 Nov DST change, since,   |
|                                       | lookback, due only 15 min after the window ended                           |
| submit                                | receipt (schedule) + job + link; key, delivery none, priority < Telegram,   |
|                                       | foreground, 3 attempts; a second run creates nothing                       |
| report / exit                         | one line per item; nonzero for any failure but window_not_show, for an      |
|                                       | unreadable playlist, or a failed submit                                    |
| already dead                          | a failure only on the first run that sees the item dead (36 h); later runs  |
|                                       | print it and exit 0; no end time counts as new                              |
| skip                                  | the line says what was heard; a skip after a skipped previous scheduled     |
|                                       | day is a failure (a persistent mismatch is not a run of holidays)          |
| second listing (F-150)                | a failed feed (404, bad XML, timeout) lists by yt-dlp on the source's route:|
|                                       | the same items and payloads; a line says so; both failing is one failure    |
|                                       | naming both; a video is read only for a show with no job; the feed path     |
|                                       | calls neither yt-dlp nor the jobs read; the listing command downloads none  |
"""
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import scheduled_sources as sched
from scheduled_sources import InventoryError, expected_items, load_inventory, run

INVENTORY = Path(__file__).resolve().parents[2] / "config" / "scheduled-sources.json"
TV, RADIO = load_inventory(INVENTORY)


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def atom(*entries):
    rows = "".join(f"<entry><yt:videoId>{vid}</yt:videoId><title>{title}</title><published>{published}</published></entry>"
                   for vid, title, published in entries)
    return ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
            f"<title>Full Shows</title>{rows}</feed>").encode()


FEED = atom(("vid1103", "Bloomberg Surveillance 11/3/2026", "2026-11-03T16:45:00+00:00"),
            ("vid1102", "Bloomberg Surveillance 11/2/2026", "2026-11-02T16:05:00+00:00"),
            ("clip", "Bloomberg Surveillance: Fed clip", "2026-11-03T18:00:00+00:00"),
            ("vid1030", "Bloomberg Surveillance 10/30/2026", "2026-10-30T16:00:00+00:00"),
            ("vid1020", "Bloomberg Surveillance 10/20/2026", "2026-10-20T16:00:00+00:00"))


def keys(items):
    return [item.key for item in items]


def test_shipped_inventory_is_the_specified_pair():
    assert (TV["id"], TV["kind"], TV["feed"], TV["since"], TV["playlistId"], TV["preferredRoute"], TV["lookbackDays"]) == (
        "surveillance-tv", "youtube_playlist", "surveillance", "2026-11-03", "PLGaYlBJIOoa-qaI1saEniG3cR9V0Mu-PE", "oci_mullvad", 5)
    assert TV["titleRegex"] == r"^Bloomberg Surveillance \d{1,2}/\d{1,2}/\d{4}$" and TV["title"] == "Bloomberg Surveillance TV {date}"
    assert (RADIO["id"], RADIO["kind"], RADIO["feed"], RADIO["since"], RADIO["videoId"], RADIO["timezone"], RADIO["start"],
            RADIO["seconds"], RADIO["expectOpening"], RADIO["lookbackDays"], RADIO["settleMinutes"]) == (
        "surveillance-radio", "youtube_live_window", "surveillance", "2026-11-03", "f39oHo6vFLg", "America/New_York", "07:00",
        10800, "Bloomberg Surveillance", 4, 15)
    assert RADIO["weekdays"] == [1, 2, 3, 4, 5] and RADIO["title"] == "Bloomberg Surveillance Radio {date}"


@pytest.mark.parametrize("change", [{"kind": "rss"}, {"feed": "nope"}, {"since": "soon"}, {"title": "no date"}, {"id": "Bad Id"},
                                    {"titleRegex": "("}, {"preferredRoute": "mac"}, {"lookbackDays": -1},
                                    {"segmenter": "surveillance-podcast"}])
def test_inventory_validation(tmp_path, change):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"version": 1, "sources": [{**TV, **change}]}))
    with pytest.raises(InventoryError):
        load_inventory(path)


def test_playlist_items_filter_by_regex_since_and_lookback():
    items = expected_items(TV, utc("2026-11-04T01:30:00Z"), lambda playlist: FEED)
    assert keys(items) == ["surveillance-tv:vid1103"]       # 11/2 and 10/30 are before `since`; the clip does not match
    (item,) = items
    assert item.title == "Bloomberg Surveillance TV 11/3/2026"
    assert item.input_json == {
        "kind": "rss", "sourceKey": "surveillance-tv:vid1103", "inputKind": "url", "feed": "surveillance",
        "title": "Bloomberg Surveillance TV 11/3/2026", "publishedAt": "2026-11-03T16:45:00Z", "delivery": "none",
        "sourceUrl": "https://www.youtube.com/watch?v=vid1103", "preferredRoute": "oci_mullvad",
        "segmenter": "surveillance-tv"}
    earlier = {**TV, "since": "2026-10-01"}
    assert keys(expected_items(earlier, utc("2026-11-04T01:30:00Z"), lambda p: FEED)) == [
        "surveillance-tv:vid1030", "surveillance-tv:vid1102", "surveillance-tv:vid1103"]     # 10/20 is 15 days back
    assert keys(expected_items(earlier, utc("2026-11-08T00:00:00Z"), lambda p: FEED)) == ["surveillance-tv:vid1103"]   # 5 days back


@pytest.mark.parametrize("body", [b"not xml", atom(), atom(("", "Bloomberg Surveillance 11/3/2026", "2026-11-03T16:45:00+00:00"))])
def test_unreadable_playlist_is_an_error(body):
    with pytest.raises(InventoryError):
        expected_items(TV, utc("2026-11-04T01:30:00Z"), lambda playlist: body)


def test_radio_windows_follow_new_york_across_the_dst_change():
    wide = {**RADIO, "since": "2026-10-26"}
    items = expected_items(wide, utc("2026-11-04T01:30:00Z"), None)
    assert [(i.key, i.input_json["liveWindow"]["start"]) for i in items] == [
        ("surveillance-radio:2026-10-30", "2026-10-30T11:00:00Z"),     # Fri, EDT: 07:00 = 11:00Z
        ("surveillance-radio:2026-11-02", "2026-11-02T12:00:00Z"),     # Mon, EST: 07:00 = 12:00Z
        ("surveillance-radio:2026-11-03", "2026-11-03T12:00:00Z")]     # Sat 10/31 and Sun 11/1 are skipped
    item = items[-1]
    assert item.title == "Bloomberg Surveillance Radio 11/3/2026"
    assert item.input_json["liveWindow"] == {"videoId": "f39oHo6vFLg", "start": "2026-11-03T12:00:00Z", "seconds": 10800,
                                             "expectOpening": "Bloomberg Surveillance"}
    assert item.input_json["publishedAt"] == "2026-11-03T12:00:00Z" and item.input_json["delivery"] == "none"
    assert item.input_json["segmenter"] == "surveillance-radio"   # hour labels on the window's New York clock
    assert item.input_json["segmentContext"] == {"start": "2026-11-03T12:00:00Z", "timezone": "America/New_York"}
    assert "preferredRoute" not in item.input_json and "sourceUrl" not in item.input_json


def test_radio_since_lookback_and_settle_time():
    assert keys(expected_items(RADIO, utc("2026-11-04T01:30:00Z"), None)) == ["surveillance-radio:2026-11-03"]
    # 4 days back from Fri 11/6 ET is Mon 11/2
    wide = {**RADIO, "since": "2026-10-26"}
    assert keys(expected_items(wide, utc("2026-11-06T20:00:00Z"), None))[0] == "surveillance-radio:2026-11-02"
    assert keys(expected_items(wide, utc("2026-11-07T20:00:00Z"), None))[0] == "surveillance-radio:2026-11-03"
    # due 15 minutes after the 15:00Z end
    assert expected_items(RADIO, utc("2026-11-03T15:14:59Z"), None) == []
    assert keys(expected_items(RADIO, utc("2026-11-03T15:15:00Z"), None)) == ["surveillance-radio:2026-11-03"]
    # a weekend and a date before `since` give nothing
    assert expected_items({**RADIO, "since": "2026-10-26"}, utc("2026-11-01T20:00:00Z"), None)[-1].key == "surveillance-radio:2026-10-30"
    assert expected_items({**RADIO, "since": "2026-11-04"}, utc("2026-11-04T10:00:00Z"), None) == []


class Controller:
    """The runtime controller's dedupe on (sourceSystem, sourceKey) and (producerSystem, idempotencyKey)."""

    def __init__(self, status=None, fail_submit=()):
        self.receipts, self.jobs, self.links, self.status, self.fail_submit = {}, {}, {}, status or {}, set(fail_submit)
        self.calls = []

    def admit_source_receipt(self, payload):
        self.calls.append("receipt")
        row = self.receipts.setdefault((payload["sourceSystem"], payload["sourceKey"]), {**payload, "receiptId": f"r{len(self.receipts)}"})
        return {"receipt": row}

    def submit_job(self, payload):
        self.calls.append("job")
        if payload["idempotencyKey"] in self.fail_submit:
            raise RuntimeError("controller refused")
        key = (payload["producerSystem"], payload["idempotencyKey"])
        job = self.jobs.setdefault(key, {**payload, "jobId": f"j{len(self.jobs)}", "status": "pending"})
        return {"job": job}

    def submit_source_receipt(self, *, receipt_id, job_id):
        self.calls.append("link")
        assert self.links.setdefault(receipt_id, job_id) == job_id

    def get_job(self, *, job_id):
        (job,) = [j for j in self.jobs.values() if j["jobId"] == job_id]
        change = self.status.get(job["idempotencyKey"], {})
        return {"job": {**job, **change}}


NOW = utc("2026-11-04T01:30:00Z")


def report(controller, *, feed=lambda p: FEED, now=NOW):
    out = io.StringIO()
    return run([TV, RADIO], controller, now, fetch_feed=feed, out=out), out.getvalue().splitlines()


def test_submission_follows_the_gateway_and_is_idempotent():
    controller = Controller()
    assert report(controller)[0] == 0 and controller.calls == ["receipt", "job", "link"] * 2
    again = report(controller)
    assert again[0] == 0 and len(controller.receipts) == len(controller.jobs) == len(controller.links) == 2
    tv, radio = (controller.jobs[("rss-audio", key)] for key in ("surveillance-tv:vid1103", "surveillance-radio:2026-11-03"))
    for job in (tv, radio):
        assert (job["queueClass"], job["workloadType"], job["maxAttempts"], job["executionProfile"], job["resultOwner"]) == (
            "foreground", "rss_publish", 3, "cpu_media", "rss-audio")
        assert job["priority"] == 20 < 60 and job["inputJson"]["delivery"] == "none"
        assert job["producerRef"] == job["idempotencyKey"] == job["inputJson"]["sourceKey"]
    receipt = controller.receipts[("schedule", "surveillance-radio:2026-11-03")]
    assert (receipt["routeKind"], receipt["producerSystem"], receipt["workloadType"]) == ("scheduled_source", "rss-audio", "rss_publish")
    assert "liveWindow" in radio["inputJson"] and tv["inputJson"]["preferredRoute"] == "oci_mullvad"


@pytest.mark.parametrize("change, line", [
    ({"status": "succeeded"}, "published"), ({"status": "pending"}, "queued"), ({"status": "retryable"}, "queued"),
    ({"status": "leased"}, "running"),
    ({"status": "dead", "lastErrorClass": "window_not_show"}, "skipped (window_not_show)")])
def test_states_that_exit_zero(change, line):
    code, lines = report(Controller({"surveillance-radio:2026-11-03": change, "surveillance-tv:vid1103": change}))
    assert code == 0
    assert lines == [f"surveillance-tv Bloomberg Surveillance TV 11/3/2026: {line}",
                     f"surveillance-radio Bloomberg Surveillance Radio 11/3/2026: {line}"]


def test_a_skip_line_says_what_was_heard():
    heard = "window opening does not say 'Bloomberg Surveillance': 'this is abc news'"
    code, lines = report(Controller({"surveillance-radio:2026-11-03": {
        "status": "dead", "lastErrorClass": "window_not_show", "lastErrorText": heard + "\n" + "x" * 400}}))
    assert code == 0 and lines[1].endswith(f": skipped (window_not_show: {heard} {'x' * (240 - len(heard) - 1)})")


WIDE_RADIO = {**RADIO, "since": "2026-10-26"}     # at NOW: Fri 10/30, Mon 11/2, Tue 11/3
SKIP = {"status": "dead", "lastErrorClass": "window_not_show", "lastErrorText": "heard abc"}


def radio_report(status):
    out = io.StringIO()
    return run([WIDE_RADIO], Controller(status), NOW, out=out), out.getvalue().splitlines()


@pytest.mark.parametrize("status", [
    {"surveillance-radio:2026-11-03": SKIP},                                          # a lone skip
    {"surveillance-radio:2026-11-02": SKIP, "surveillance-radio:2026-11-03": SKIP},   # skips in a row: telemetry shows them (X-75)
    {"surveillance-radio:2026-11-02": SKIP, "surveillance-radio:2026-11-03": {"status": "succeeded"}}])
def test_skips_do_not_fail_the_scheduler(status):
    assert radio_report(status)[0] == 0


@pytest.mark.parametrize("change, line", [
    ({"status": "dead", "lastErrorClass": "window_outside_buffer"}, "failed window_outside_buffer"),
    ({"status": "dead", "lastErrorClass": "download"}, "failed download"),
    ({"status": "dead"}, "failed unknown"), ({"status": "cancelled"}, "failed cancelled")])
def test_any_other_failure_exits_nonzero(change, line):
    code, lines = report(Controller({"surveillance-radio:2026-11-03": change}))
    assert code == 1 and lines[1].endswith(f": {line}") and lines[0].endswith(": queued")


@pytest.mark.parametrize("ended, code, suffix", [
    ("2026-11-03T20:00:00Z", 1, "failed download"),                    # ended 5.5 h ago: this is the first run to see it
    ("2026-11-03T13:30:00Z", 1, "failed download"),                    # 36 h boundary is inclusive...
    ("2026-11-03T13:29:59Z", 0, "failed download (reported by an earlier run)"),   # ...a second older is not
    ("2026-11-01T06:00:00Z", 0, "failed download (reported by an earlier run)"),
    ("not a time", 1, "failed download")])
def test_a_dead_item_fails_only_the_first_run_that_sees_it(ended, code, suffix):
    now = utc("2026-11-04T01:30:00Z") + timedelta(hours=24)
    dead = {"status": "dead", "lastErrorClass": "download", "updatedAt": ended}
    out = io.StringIO()
    assert run([RADIO], Controller({"surveillance-radio:2026-11-03": dead}), now, out=out) == code
    (line,) = [l for l in out.getvalue().splitlines() if "11/3/2026" in l]
    assert line.endswith(f": {suffix}")


def test_the_same_dead_item_alerts_once_across_nightly_runs():
    controller = Controller({"surveillance-radio:2026-11-03": {
        "status": "dead", "lastErrorClass": "download", "updatedAt": "2026-11-04T04:00:00Z"}})
    codes = [run([RADIO], controller, utc("2026-11-04T01:30:00Z") + timedelta(days=n), out=io.StringIO()) for n in range(4)]
    assert codes == [1, 1, 0, 0]       # n=0 sees the stamp in its own future (unknown is loud); n=1 is the first run after it died


def test_a_second_skip_in_a_row_alerts_once_too():
    old = {**SKIP, "updatedAt": "2026-10-31T06:00:00Z"}
    code, lines = radio_report({"surveillance-radio:2026-11-02": SKIP, "surveillance-radio:2026-11-03": old})
    assert code == 0 and lines[2].endswith(": skipped (window_not_show: heard abc)")


def test_unreadable_playlist_exits_nonzero_but_the_radio_is_still_submitted():
    def broken(playlist):
        raise OSError("HTTP 500")
    controller = Controller()
    code, lines = report(controller, feed=broken)
    assert code == 1 and lines[0] == "surveillance-tv: cannot list items: HTTP 500"
    assert lines[1].endswith(": queued") and len(controller.jobs) == 1


def test_a_refused_submit_is_a_failed_line_and_exit_one():
    code, lines = report(Controller(fail_submit={"surveillance-tv:vid1103"}))
    assert code == 1 and lines[0].endswith(": failed submit: controller refused") and lines[1].endswith(": queued")


def test_nothing_due_is_quiet_and_zero():
    code, lines = report(Controller(), now=utc("2026-11-03T14:00:00Z"), feed=lambda p: atom(
        ("old", "Bloomberg Surveillance 11/2/2026", "2026-11-02T16:00:00+00:00")))
    assert code == 0 and lines == ["surveillance-tv: nothing expected", "surveillance-radio: nothing expected"]


def test_list_mode_submits_nothing(capsys, monkeypatch):
    monkeypatch.setattr(sched, "fetch_playlist_feed", lambda playlist: FEED)
    assert sched.main(["--list", "--inventory", str(INVENTORY), "--now", "2026-11-04T01:30:00Z"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "surveillance-tv Bloomberg Surveillance TV 11/3/2026: expected (surveillance-tv:vid1103, published 2026-11-03T16:45:00Z)",
        "surveillance-radio Bloomberg Surveillance Radio 11/3/2026: expected (surveillance-radio:2026-11-03, "
        "published 2026-11-03T12:00:00Z)"]


# -- the second listing (F-150): yt-dlp when the Atom feed fails ---------------------------------

def http_404(playlist):
    import urllib.error
    raise urllib.error.HTTPError(f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist}", 404,
                                 "Not Found", {}, None)


class YtDlp:
    """yt-dlp's JSON for the FEED's videos, as ``fetch_source_json_for_route`` returns it: a flat listing (newest
    first, no times) or one video's metadata (``timestamp``, epoch seconds). Records every call."""

    VIDEOS = [("vid1103", "Bloomberg Surveillance 11/3/2026", "2026-11-03T16:45:00Z"),
              ("vid1102", "Bloomberg Surveillance 11/2/2026", "2026-11-02T16:05:00Z"),
              ("clip", "Bloomberg Surveillance: Fed clip", "2026-11-03T18:00:00Z"),
              ("vid1030", "Bloomberg Surveillance 10/30/2026", "2026-10-30T16:00:00Z"),
              ("vid1020", "Bloomberg Surveillance 10/20/2026", "2026-10-20T16:00:00Z")]

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, settings, url, route, playlist_end=None):
        self.calls.append((url.rsplit("=", 1)[-1], str(route), playlist_end))
        if self.fail:
            raise self.fail
        if playlist_end is not None:
            return {"_type": "playlist", "entries": [{"id": vid, "title": title, "timestamp": None, "upload_date": None}
                                                     for vid, title, _ in self.VIDEOS][:playlist_end]}
        (published,) = [p for vid, _, p in self.VIDEOS if vid == url.rsplit("=", 1)[-1]]
        return {"id": url.rsplit("=", 1)[-1], "timestamp": int(utc(published).timestamp())}


def second(fake):
    return sched.YtDlpPlaylist(lambda: "tool settings", fetch_json=fake)


def test_a_feed_404_falls_back_to_yt_dlp_with_the_same_items():
    for since, now in (("2026-11-03", NOW), ("2026-10-01", NOW), ("2026-10-01", utc("2026-11-08T00:00:00Z"))):
        source, fake = {**TV, "since": since}, YtDlp()
        notes = []
        by_feed = expected_items(source, now, lambda p: FEED)
        by_ytdlp = expected_items(source, now, http_404, second=second(fake), notes=notes)
        assert by_ytdlp == by_feed and by_feed
        assert notes == ["surveillance-tv: listed by yt-dlp (feed: HTTP Error 404: Not Found)"]
        # one flat listing by the source's route, then one video read per show inside since and lookback
        assert fake.calls[0] == ("PLGaYlBJIOoa-qaI1saEniG3cR9V0Mu-PE", "oci_mullvad", sched.LISTING_DEPTH)
        assert {vid for vid, _, end in fake.calls[1:]} >= {item.key.split(":")[1] for item in by_feed}
        assert all(end is None for _, _, end in fake.calls[1:]) and "vid1020" not in str(fake.calls) and "clip" not in str(fake.calls)


@pytest.mark.parametrize("feed_error", [http_404, lambda p: b"not xml", lambda p: (_ for _ in ()).throw(TimeoutError("timed out"))])
def test_any_unreadable_feed_takes_the_second_path(feed_error):
    assert keys(expected_items(TV, NOW, feed_error, second=second(YtDlp()))) == ["surveillance-tv:vid1103"]


def test_the_run_says_yt_dlp_listed_and_submits_the_same_item():
    by_feed, by_ytdlp = Controller(), JobsController()
    out = io.StringIO()
    code = run([TV, RADIO], by_ytdlp, NOW, fetch_feed=http_404, second=second(YtDlp()), out=out)
    lines = out.getvalue().splitlines()
    assert code == 0 and lines[0] == "surveillance-tv: listed by yt-dlp (feed: HTTP Error 404: Not Found)"
    assert lines[1] == "surveillance-tv Bloomberg Surveillance TV 11/3/2026: queued"
    report(by_feed)
    assert by_ytdlp.jobs == by_feed.jobs and by_ytdlp.receipts == by_feed.receipts   # same payloads, byte for byte


def test_both_paths_failing_is_one_failure_naming_both():
    out = io.StringIO()
    controller = Controller()
    code = run([TV, RADIO], controller, NOW, fetch_feed=http_404, out=out,
               second=second(YtDlp(fail=RuntimeError("Sign in to confirm you're not a bot"))))
    lines = out.getvalue().splitlines()
    assert code == 1 and lines[0] == ("surveillance-tv: cannot list items: feed: HTTP Error 404: Not Found; "
                                      "yt-dlp: Sign in to confirm you're not a bot")
    assert len([line for line in lines if line.startswith("surveillance-tv")]) == 1
    assert lines[1].endswith(": queued") and len(controller.jobs) == 1   # the radio still goes


def test_an_empty_yt_dlp_listing_is_a_failure_too():
    class Empty(YtDlp):
        def __call__(self, settings, url, route, playlist_end=None):
            return {"_type": "playlist", "entries": []}
    with pytest.raises(InventoryError, match="feed: HTTP Error 404.*yt-dlp: yt-dlp listing has no usable entries"):
        expected_items(TV, NOW, http_404, second=second(Empty()))


def test_a_video_without_a_time_fails_the_listing():
    class NoTime(YtDlp):
        def __call__(self, settings, url, route, playlist_end=None):
            data = super().__call__(settings, url, route, playlist_end)
            return data if playlist_end else {"id": data["id"]}
    with pytest.raises(InventoryError, match="no published time for vid1103"):
        expected_items(TV, NOW, http_404, second=second(NoTime()))


class JobsController(Controller):
    """The controller's read of jobs by producer ref prefix (newest first), counting the reads."""

    def __init__(self, existing=()):
        super().__init__()
        self.existing, self.lists = list(existing), 0

    def list_jobs(self, *, producer_ref_prefix, limit, **_):
        self.lists += 1
        return {"items": [job for job in self.existing if job["producerRef"].startswith(producer_ref_prefix)]}


def test_no_video_read_for_an_item_that_has_a_job():
    feed_day = utc("2026-11-04T20:00:00Z")
    feed = YtDlp()
    feed.VIDEOS = [("vid1104", "Bloomberg Surveillance 11/4/2026", "2026-11-04T15:30:00Z"), *YtDlp.VIDEOS]
    controller = JobsController([
        {"producerRef": "surveillance-tv:vid1103", "status": "succeeded", "inputJson": {"publishedAt": "2026-11-03T16:45:00Z"}},
        {"producerRef": "surveillance-tv:vid1103:notes", "status": "succeeded", "inputJson": {}},
        {"producerRef": "surveillance-radio:2026-11-03", "status": "succeeded", "inputJson": {"publishedAt": "x"}}])
    out = io.StringIO()
    code = run([TV], controller, feed_day, fetch_feed=http_404, second=second(feed), out=out)
    assert code == 0 and controller.lists == 1
    assert [vid for vid, _, end in feed.calls if end is None] == ["vid1104"]   # 11/3 has a job: its time is the job's
    assert [job["idempotencyKey"] for job in controller.jobs.values()] == ["surveillance-tv:vid1103", "surveillance-tv:vid1104"]
    assert {job["inputJson"]["publishedAt"] for job in controller.jobs.values()} == {"2026-11-03T16:45:00Z", "2026-11-04T15:30:00Z"}


def test_the_feed_path_reads_neither_yt_dlp_nor_the_jobs():
    fake, controller = YtDlp(), JobsController()
    out = io.StringIO()
    assert run([TV], controller, NOW, fetch_feed=lambda p: FEED, second=second(fake), out=out) == 0
    assert fake.calls == [] and controller.lists == 0 and "yt-dlp" not in out.getvalue()


def test_flat_listing_commands_download_nothing():
    import source_audio as sa
    settings = sa.SourceToolSettings(mac_ssh_target="mac", mac_ytdlp_path="/opt/yt-dlp", yt_dlp_bin="yt-dlp",
                                     ffmpeg_bin="ffmpeg", mullvad_proxy="http://127.0.0.1:8888",
                                     yt_dlp_remote_components="ejs:github")
    url = "https://www.youtube.com/playlist?list=PL1"
    local = sa._flat_listing_command_for_route(settings, url, sa.SourceRoute.OCI_MULLVAD, 30)
    assert local == ["yt-dlp", "--proxy", "http://127.0.0.1:8888", "--flat-playlist", "--dump-single-json",
                     "--playlist-end", "30", "--remote-components", "ejs:github", url]
    mac = sa._flat_listing_command_for_route(settings, url, sa.SourceRoute.MAC_SSH, 30)
    assert mac[:2] == ["ssh", "mac"] and "--flat-playlist --dump-single-json --playlist-end 30" in mac[2]
    assert "--cookies-from-browser safari" in mac[2] and mac[2].endswith(f"'{url}'")
