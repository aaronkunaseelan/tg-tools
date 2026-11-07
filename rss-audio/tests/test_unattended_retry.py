"""Scheduled (delivery none) jobs retry a sign-in/bot wall; Telegram jobs keep failing at once.

| failure                                  | Telegram job (attended) | scheduled job (unattended) |
| "Sign in to confirm you're not a bot"    | terminal                | retryable                  |
| "Video unavailable" / no format          | terminal                | terminal                   |
| liveWindow stream lookup refused (bot)   | rotates the Mullvad exit and looks again; no rotation for other stages |
"""
import asyncio
from types import SimpleNamespace

import pytest

import runtime_worker
import surveillance
from live_window import LiveWindowError
from pipeline import JobOptions, PipelineError, is_retryable
from source_audio import SourceAudioError

WALL = ("ERROR: [youtube] f39oHo6vFLg: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies "
        "for the authentication.")
WINDOW = {"videoId": "f39oHo6vFLg", "start": "2026-11-03T12:00:00Z", "seconds": 10800, "expectOpening": "Bloomberg Surveillance"}


@pytest.mark.parametrize("text, attended, unattended", [
    (WALL, False, True), ("ERROR: Video unavailable", False, False),
    ("Requested format is not available", False, False), ("HTTP Error 503", True, True)])
def test_classification(text, attended, unattended):
    error = PipelineError("metadata", "yt-dlp failed", text)
    assert is_retryable(error) is attended
    assert is_retryable(error, unattended=True) is unattended


@pytest.mark.parametrize("input_json, outcome", [({"delivery": "none"}, "retryable_error"),
                                                 ({"messageRef": {"chat_id": 1, "message_id": 2}}, "terminal_error")])
def test_attempt_outcome_follows_the_job_kind(monkeypatch, tmp_path, input_json, outcome):
    async def refused(*args, **kwargs):
        raise PipelineError("metadata", "Source requires sign-in or browser cookies", WALL)
    monkeypatch.setattr(runtime_worker, "execute_runtime_job", refused)
    monkeypatch.setattr(runtime_worker, "job_work_dir", lambda root, job_id: tmp_path)
    finalized = []
    client = SimpleNamespace(mark_running=lambda **kw: {}, finalize=lambda payload: finalized.append(payload) or {},
                             append_delivery=lambda payload: {})
    claim = {"job": {"jobId": "j", "inputJson": input_json}, "attempt": {"attemptId": "a", "leaseToken": "t"}}
    asyncio.run(runtime_worker._process_claim_inner(client=client, settings=SimpleNamespace(work_dir=tmp_path), bot=None, claim=claim))
    assert finalized[0]["outcome"] == outcome


class Rotator:
    jitter_sec = (0, 0)

    def __init__(self, allow=True):
        self.allow, self.rotated = allow, []

    def attempts(self): return 3
    def eligible(self, detail): return "not a bot" in detail
    def rotate(self, attempt): return self.rotated.append(attempt) or self.allow


@pytest.fixture
def lookup(monkeypatch, tmp_path):
    settings = SimpleNamespace(ffprobe_bin="ffprobe", enrichment_stt_model="mini", enrichment_jobs=1, mullvad_proxy="http://p:1",
                               mac_ssh_target="", mac_ytdlp_path="", mac_cookies_from_browser="", yt_dlp_bin="yt-dlp",
                               yt_dlp_format="ba", ffmpeg_bin="ffmpeg", mullvad_control_url="", mullvad_control_token="",
                               yt_dlp_remote_components="")
    state = SimpleNamespace(calls=0, rotator=Rotator(), errors=[SourceAudioError("metadata", "Source requires sign-in", WALL)])
    monkeypatch.setattr(surveillance, "check_oci_mullvad_health", lambda tools: SimpleNamespace(ok=True))
    monkeypatch.setattr(surveillance, "exit_rotator", lambda s: state.rotator)
    monkeypatch.setattr(surveillance.time, "sleep", lambda seconds: None)
    audio = tmp_path / "window.m4a"
    audio.write_bytes(b"m4a")

    def fetch(*args, **kwargs):
        state.calls += 1
        if state.errors:
            raise state.errors.pop(0)
        return audio, 10800.0
    monkeypatch.setattr(surveillance.live_window, "fetch_live_window", fetch)
    monkeypatch.setattr(surveillance, "ingest_live_window", lambda settings, **kw: SimpleNamespace(
        slug="s", title="t", public_url="https://h/s.mp3", file_size_bytes=1, script_url=None, notes_url=None,
        manifest_url=kw["fetch_audio"](tmp_path) and None))
    state.run = lambda: surveillance.publish_window(settings, {"liveWindow": WINDOW}, JobOptions(), tmp_path)
    return state


def test_a_bot_wall_on_the_stream_lookup_rotates_the_exit_and_looks_again(lookup):
    assert lookup.run().slug == "s"
    assert lookup.calls == 2 and lookup.rotator.rotated == [0]


def test_other_stages_and_a_refused_rotation_do_not_loop(lookup):
    lookup.errors = [SourceAudioError("download", "ffmpeg joined nothing", WALL)]
    with pytest.raises(PipelineError) as error:
        lookup.run()
    assert error.value.stage == "download" and lookup.calls == 1 and lookup.rotator.rotated == []
    lookup.calls, lookup.errors, lookup.rotator = 0, [SourceAudioError("metadata", "Source requires sign-in", WALL)], Rotator(allow=False)
    with pytest.raises(PipelineError) as error:
        lookup.run()
    assert error.value.stage == "metadata" and lookup.calls == 1 and lookup.rotator.rotated == [0]
    # the stream's own verdicts are never rotated away
    lookup.calls, lookup.errors = 0, [LiveWindowError("window_not_show", "not the show", retryable=False)]
    with pytest.raises(PipelineError) as error:
        lookup.run()
    assert error.value.stage == "window_not_show" and lookup.rotator.rotated == [0]
