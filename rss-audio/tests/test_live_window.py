"""Live-window source: segment math from the measured stream head, outcomes, and the fetch itself (fakes only).

Measured 2026-11-03 21:54 UTC: head seq 1677546, head time 8387729278 ms, walltime 1793742841578 ms.
| case                                   | outcome                                                              |
| 07:00 ET (12:00Z) window, 3 h         | first segment 1670417, 2160 segments, 5.000 s each                  |
| window ends after the stream head      | retryable `window_pending`                                          |
| segment length outside 4.9-5.1 s       | terminal `window_segment_duration`                                  |
| no head headers                        | retryable `window_head_unknown`                                     |
| first segment 404 (older than buffer)  | terminal `window_outside_buffer`, nothing else fetched              |
| a segment 503 once / always            | retried / retryable `window_fetch`                                  |
| joined audio not within 30 s of window | retryable `window_duration` (segments dropped)                      |
| opening lacks expectOpening            | terminal `window_not_show`; only the opening clip was fetched       |
| all good                               | 2160 segments, at most 8 at once, retried segment fetched again     |
"""
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import live_window
import runtime_worker
from live_window import (LiveWindow, LiveWindowError, SegmentResponse, fetch_live_window, head_from_headers,
                         parse_window, plan_window)
from pipeline import PipelineError
from source_audio import SourceToolSettings

HEADERS = {"X-Head-Seqnum": "1677546", "X-Head-Time-Millis": "8387729278", "X-Walltime-Ms": "1793742841578"}
WINDOW = {"videoId": "f39oHo6vFLg", "start": "2026-11-03T12:00:00Z", "seconds": 10800,
          "expectOpening": "Bloomberg Surveillance"}
FIRST, COUNT = 1670417, 2160
TOOLS = SourceToolSettings(mac_ssh_target="mac", mac_ytdlp_path="/y", yt_dlp_bin="yt-dlp", ffmpeg_bin="ffmpeg",
                           mullvad_proxy="http://proxy:8888")


def at(headers=HEADERS, **change):
    return head_from_headers({**headers, **change})


def test_measured_head_gives_the_segment_and_count():
    head = at()
    assert head.segment_seconds == pytest.approx(5.0, abs=1e-3)
    plan = plan_window(parse_window(WINDOW), head)
    assert (plan.first, plan.count) == (FIRST, COUNT)


def test_window_start_is_offset_by_the_wall_clock_not_the_segment_grid():
    # the same head read 5 s later in wall time puts the window start one segment further back
    later = plan_window(parse_window(WINDOW), at(**{"X-Walltime-Ms": "1793742846578"}))
    assert later.first == FIRST - 1


def test_window_not_over_at_the_head_is_retryable():
    with pytest.raises(LiveWindowError) as err:
        plan_window(parse_window({**WINDOW, "start": "2026-11-04T12:00:00Z"}), at())
    assert (err.value.error_class, err.value.retryable) == ("window_pending", True)
    # a window one segment longer than the buffered stream is still pending
    with pytest.raises(LiveWindowError) as err:
        plan_window(parse_window({**WINDOW, "start": "2026-11-03T21:50:00Z", "seconds": 600}), at())
    assert err.value.error_class == "window_pending"


@pytest.mark.parametrize("millis", ["5500000000", "4000000000"])
def test_implausible_segment_length_is_terminal(millis):
    with pytest.raises(LiveWindowError) as err:
        plan_window(parse_window(WINDOW), at(**{"X-Head-Time-Millis": millis, "X-Head-Seqnum": "1000000"}))
    assert (err.value.error_class, err.value.retryable) == ("window_segment_duration", False)


@pytest.mark.parametrize("headers", [{}, {"X-Head-Seqnum": "1"}, {**HEADERS, "X-Head-Seqnum": "x"}, {**HEADERS, "X-Head-Seqnum": "0"}])
def test_missing_head_is_retryable(headers):
    with pytest.raises(LiveWindowError) as err:
        head_from_headers(headers)
    assert (err.value.error_class, err.value.retryable) == ("window_head_unknown", True)


@pytest.mark.parametrize("change", [{"videoId": "short"}, {"seconds": 5}, {"seconds": True}, {"start": "2026-11-03T12:00:00"},
                                    {"start": "soon"}, {"expectOpening": " "}])
def test_bad_window_input_is_terminal(change):
    with pytest.raises(LiveWindowError) as err:
        parse_window({**WINDOW, **change})
    assert (err.value.error_class, err.value.retryable) == ("input", False)


def test_window_identity_and_url():
    window = parse_window({**WINDOW, "start": "2026-11-03T07:00:00-05:00"})
    assert window.start == datetime(2026, 11, 3, 12, tzinfo=UTC)
    assert (window.identity, window.source_url) == ("f39oHo6vFLg@2026-11-03T12:00:00Z", "https://www.youtube.com/watch?v=f39oHo6vFLg")


def test_stream_url_must_be_googlevideo_https():
    for url in ("http://rr1---sn.googlevideo.com/videoplayback?x=1", "https://evil.example.com/videoplayback?x=1"):
        with pytest.raises(LiveWindowError) as err:
            live_window.make_proxy_fetcher("http://proxy:8888", url)
        assert err.value.error_class == "window_stream_url"


def test_resolve_reads_format_140_url_and_passes_the_route_options():
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return SimpleNamespace(stdout='{"formats": [{"format_id": "139", "url": "u139"}, {"format_id": "140", "url": "https://rr1.googlevideo.com/v?a=1"}]}')
    assert live_window.resolve_stream_url(TOOLS, "f39oHo6vFLg", run=run) == "https://rr1.googlevideo.com/v?a=1"
    command = seen[0]
    assert command[command.index("--proxy") + 1] == "http://proxy:8888"
    assert {"--live-from-start", "-j", "--skip-download", "--no-remote-components"} <= set(command)
    with pytest.raises(LiveWindowError) as err:
        live_window.resolve_stream_url(TOOLS, "f39oHo6vFLg", run=lambda *a, **k: SimpleNamespace(stdout='{"formats": []}'))
    assert (err.value.error_class, err.value.retryable) == ("window_format_unavailable", True)


class Stream:
    """A fake DASH buffer: segment responses (statuses) and the ffmpeg/ffprobe the join runs."""

    def __init__(self, *, missing=(), flaky=(), broken=(), duration=10799.6, heard="This is Bloomberg Surveillance with Tom Keene"):
        self.missing, self.flaky, self.broken, self.duration, self.heard = set(missing), set(flaky), set(broken), duration, heard
        self.calls, self.joined, self.active, self.max_active, self.lock = [], [], 0, 0, threading.Lock()

    def fetch(self, number):
        with self.lock:
            self.calls.append(number)
            attempts = self.calls.count(number)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.0005)
        try:
            if number == 0 or number in self.missing:
                return SegmentResponse(404, HEADERS)
            if number in self.broken or (number in self.flaky and attempts == 1):
                return SegmentResponse(503, HEADERS)
            return SegmentResponse(200, HEADERS, b"seg")
        finally:
            with self.lock:
                self.active -= 1

    def run(self, args, *, stage, timeout=None, env=None):
        if args[0] == "ffmpeg":
            self.joined.append(Path(args[args.index("-i") + 1]).read_text().count("file '"))
            Path(args[-1]).write_bytes(b"m4a")
            return SimpleNamespace(stdout="")
        return SimpleNamespace(stdout=f"{self.duration}\n")

    def fetch_window(self, tmp_path, window=WINDOW):
        return fetch_live_window(
            TOOLS, parse_window(window), tmp_path, ffprobe_bin="ffprobe", transcribe_clip=lambda clip: self.heard,
            resolve=lambda settings, video: "https://rr1.googlevideo.com/v?a=1", make_fetcher=lambda proxy, url: self.fetch,
            run=self.run, sleep=lambda seconds: None)


def test_whole_window_is_fetched_joined_and_checked(tmp_path):
    stream = Stream(flaky={FIRST + 700})
    audio, duration = stream.fetch_window(tmp_path)
    assert audio.name == "window.m4a" and duration == pytest.approx(10799.6)
    assert sorted(set(stream.calls) - {0}) == list(range(FIRST, FIRST + COUNT))
    assert stream.calls.count(FIRST + 700) == 2 and stream.max_active <= live_window.FETCH_WORKERS
    assert stream.joined == [26, COUNT]      # the opening clip first, then the whole window
    # a retry reuses segments already on disk
    again = Stream()
    again.fetch_window(tmp_path)
    assert again.calls == [0]


def test_first_segment_gone_is_terminal_and_nothing_else_is_fetched(tmp_path):
    stream = Stream(missing={FIRST})
    with pytest.raises(LiveWindowError) as err:
        stream.fetch_window(tmp_path)
    assert (err.value.error_class, err.value.retryable) == ("window_outside_buffer", False)
    assert len(stream.calls) <= 1 + 26


def test_a_segment_that_keeps_failing_is_retryable(tmp_path):
    stream = Stream(broken={FIRST + 1000})
    with pytest.raises(LiveWindowError) as err:
        stream.fetch_window(tmp_path)
    assert (err.value.error_class, err.value.retryable) == ("window_fetch", True)
    assert stream.calls.count(FIRST + 1000) == live_window.SEGMENT_TRIES and f"{FIRST + 1000}:503" in str(err.value)


def test_window_not_over_is_retryable_before_any_segment(tmp_path):
    stream = Stream()
    with pytest.raises(LiveWindowError) as err:
        stream.fetch_window(tmp_path, {**WINDOW, "start": "2026-11-04T12:00:00Z"})
    assert (err.value.error_class, err.value.retryable) == ("window_pending", True) and stream.calls == [0]


@pytest.mark.parametrize("duration", [10830.5, 10769.5, 5400])
def test_joined_duration_must_match_the_window(tmp_path, duration):
    stream = Stream(duration=duration)
    with pytest.raises(LiveWindowError) as err:
        stream.fetch_window(tmp_path)
    assert (err.value.error_class, err.value.retryable) == ("window_duration", True)
    assert not list((tmp_path / "live").glob("seg-*.bin"))


@pytest.mark.parametrize("duration", [10770.5, 10829.5])
def test_duration_within_thirty_seconds_passes(tmp_path, duration):
    assert Stream(duration=duration).fetch_window(tmp_path)[1] == duration


def test_opening_that_is_not_the_show_publishes_nothing(tmp_path):
    stream = Stream(heard="This is ABC News. Top of the hour")
    with pytest.raises(LiveWindowError) as err:
        stream.fetch_window(tmp_path)
    assert (err.value.error_class, err.value.retryable) == ("window_not_show", False)
    assert len(stream.calls) <= 1 + 26 and not (tmp_path / "live" / "window.m4a").exists()


@pytest.mark.parametrize("heard", ["Bloomberg, Surveillance.", "Bloomberg-Surveillance", "...BLOOMBERG   (Surveillance)!",
                                   "This is Bloomberg: Surveillance, with Tom Keene"])
def test_opening_match_ignores_punctuation(tmp_path, heard):
    Stream(heard=heard).fetch_window(tmp_path)


def test_punctuation_does_not_make_another_show_match(tmp_path):
    with pytest.raises(LiveWindowError) as err:
        Stream(heard="Bloomberg, Markets. Surveil lance").fetch_window(tmp_path)
    assert err.value.error_class == "window_not_show"


def test_opening_match_ignores_case_and_spacing(tmp_path):
    Stream(heard="this   is BLOOMBERG\nsurveillance with tom").fetch_window(tmp_path)


def test_worker_turns_window_errors_into_the_jobs_error_class(tmp_path, monkeypatch):
    """errorClass of the finalized attempt is the PipelineError stage; retryable is explicit."""
    settings = SimpleNamespace(ffprobe_bin="ffprobe", enrichment_stt_model="mini", enrichment_jobs=1, mullvad_proxy="http://p:1",
                               mac_ssh_target="", mac_ytdlp_path="", mac_cookies_from_browser="", yt_dlp_bin="yt-dlp",
                               yt_dlp_format="ba", ffmpeg_bin="ffmpeg", mullvad_control_url="", mullvad_control_token="",
                               yt_dlp_remote_components="")
    monkeypatch.setattr(runtime_worker, "check_oci_mullvad_health", lambda tools: SimpleNamespace(ok=True))
    for error, retryable in ((LiveWindowError("window_not_show", "not the show", retryable=False), False),
                             (LiveWindowError("window_pending", "not over", retryable=True), True)):
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(runtime_worker.live_window, "fetch_live_window", fail)
        published = []
        monkeypatch.setattr(runtime_worker, "ingest_live_window",
                            lambda settings, **kw: kw["fetch_audio"](tmp_path) and published.append(1))
        with pytest.raises(PipelineError) as raised:
            runtime_worker._publish_live_window(settings, {"liveWindow": WINDOW}, runtime_worker.JobOptions(), tmp_path)
        assert raised.value.stage == error.error_class and runtime_worker._is_retryable_pipeline_error(raised.value) is retryable
        assert published == []


def test_live_window_is_oci_mullvad_only_and_exclusive(tmp_path, monkeypatch):
    import os
    from unittest import mock
    from config import load_settings
    with mock.patch.dict(os.environ, {"RSS_AUDIO_BASE_URL": "https://rss.example.test", "RSS_AUDIO_B2_BUCKET": "b",
                                      "HOME": os.environ.get("HOME", "/tmp")}, clear=True):
        settings = load_settings()
    with pytest.raises(PipelineError) as err:
        runtime_worker._publish_live_window(settings, {"liveWindow": WINDOW, "preferredRoute": "mac_ssh"}, runtime_worker.JobOptions(), tmp_path)
    assert err.value.stage == "input"
    import asyncio
    with pytest.raises(PipelineError) as err:
        asyncio.run(runtime_worker.execute_runtime_job(settings, None, {"liveWindow": WINDOW, "sourceUrl": "https://example.com/a"}))
    assert err.value.stage == "input"
