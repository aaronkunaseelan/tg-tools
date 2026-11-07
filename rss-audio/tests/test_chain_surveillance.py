"""Native chain for the Surveillance pipeline (surveillance.run), radio leg.

The nightly schedule (scheduled_sources.run on the shipped radio source) -> source receipt, job and link
on the runtime controller -> rss-audio worker loop: the window's stream found by yt-dlp on the Mullvad
route, its first two minutes fetched and heard to open the show, the whole three hours fetched and joined
by ffmpeg, speech-to-text on m3-asr, rclone upload, episode API -> the notes job it submits, cut at the
broadcast's hour seams on New York's clock -> in-loop result projection (no Telegram message) -> the
schedule's line for the item, read again. Exact get: the episode row's links resolve to the uploaded bucket bytes.

Replaced edges only: the Mullvad proxy (the edge answers its CONNECT health check), yt-dlp (a stand-in
that lists the stream), the stream's DASH segments as the proxy relays them (one 4.992 s AAC segment
served for every number in the buffer), m3-asr and the model CLIs (the edge and the harness fakes).

Case table
| case                | input                                    | proves                                               |
| a radio window show | the 3 Nov window, 07:00-10:00 New York,  | queued, then published; segments fetched opening     |
|                     | read by the 4 Nov 01:30 UTC schedule run | first, then exactly the window; the row has the show's |
|                     |                                          | title and air time; notes cut at the 08:00 and 09:00 |
|                     |                                          | opens, labelled on New York's clock; page linked;    |
|                     |                                          | no Telegram message                                  |
"""
import asyncio
import functools
import io
import json
import math
import os
import subprocess
from datetime import datetime

import pytest

from chain_harness import install_fake_models, model_calls, run_until, seal_dotenv, start_stack

pytestmark = pytest.mark.chain
NOW = datetime.fromisoformat("2026-11-04T01:30:00+00:00")   # the timer's run after Tuesday's show
START_MS = int(datetime.fromisoformat("2026-11-03T12:00:00+00:00").timestamp() * 1000)   # 07:00 EST
SEGMENT_MS = 4992   # 39 AAC frames of 1024 samples at 8 kHz
HEAD = 100_000      # the stream head's segment number, ten minutes after the window ends
FIRST = HEAD + math.floor(-(10800 + 600) * 1000 / SEGMENT_MS)
COUNT = round(10800 * 1000 / SEGMENT_MS)
STREAM_URL = "https://rr1.googlevideo.com/videoplayback?id=chain&itag=140"
OPEN = "This is Bloomberg Surveillance with Tom Keene and Paul Sweeney."
HOURS = [
    [OPEN, "Bloomberg Originals, on the radio and on YouTube every weekday morning.",
     "Futures are higher this morning after the jobs report on Friday.", "Treasury yields fell across the curve overnight.",
     "The dollar slipped against the yen in early trading today.", "Oil held near seventy dollars a barrel this week.",
     "Investors are waiting for the central bank decision on Wednesday.", "Volatility stayed low into the open today."],
    [OPEN, "Bloomberg Originals, on the radio and on YouTube every weekday morning.",
     "The Federal Reserve meets this week with markets pricing a cut.", "Two governors spoke about inflation on Monday.",
     "Wages grew faster than expected in the latest report.", "Housing starts slowed for a third straight month.",
     "Mortgage rates eased slightly from their summer highs.", "Bank stocks rose on the steeper curve today."],
    [OPEN, "Bloomberg Originals, on the radio and on YouTube every weekday morning.",
     "Earnings season continues with three large retailers today.", "One chipmaker raised its outlook for the year.",
     "A carmaker cut its forecast on weaker demand in Europe.", "Travel companies reported record bookings again.",
     "Analysts expect margins to narrow next quarter overall.", "That is all for this hour of the program today."],
]


def _words():
    """m3-asr's word timings: each hour's speech starts a second after its wall-clock hour."""
    words = []
    for hour, sentences in enumerate(HOURS):
        at = hour * 3600 + 1.0
        for word in " ".join(sentences).split():
            words.append({"word": word, "start": round(at, 2), "end": round(at + 0.5, 2)})
            at += 0.6
    return words


class Stream:
    """The live stream's buffer as the Mullvad proxy relays it: segment N is the same AAC segment."""

    def __init__(self, segment: bytes):
        self.segment, self.requested = segment, []
        self.headers = {"x-head-seqnum": str(HEAD), "x-head-time-millis": str(HEAD * SEGMENT_MS),
                        "x-walltime-ms": str(START_MS + (10800 + 600) * 1000)}

    def fetcher(self, proxy, url):
        assert url == STREAM_URL, url
        return self.fetch

    def fetch(self, number):
        from live_window import SegmentResponse
        self.requested.append(number)
        if 0 < number < HEAD:
            return SegmentResponse(200, self.headers, self.segment)
        return SegmentResponse(404, self.headers)


def _aac_segment(directory):
    longer, segment = directory / "tone.aac", directory / "segment.aac"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=8",
                    "-ac", "1", "-c:a", "aac", "-b:a", "16k", str(longer)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(longer), "-c", "copy", "-frames:a", "39", "-f", "adts",
                    str(segment)], check=True)
    return segment.read_bytes()


def _yt_dlp(directory, log):
    path = directory / "yt-dlp"
    path.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                    f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    f"print(json.dumps({{'formats': [{{'format_id': '140', 'url': {STREAM_URL!r}}}]}}))\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def stack(tmp_path, monkeypatch):
    import live_window
    from pi_harness import preflight
    from chain_harness import _multipart_fields
    stack = start_stack()
    monkeypatch.setattr(preflight, "CODEX_AUTH_PATH", install_fake_models(tmp_path / "bin", stack.root / "models.jsonl"))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    seal_dotenv(monkeypatch)
    stack.stream = Stream(_aac_segment(tmp_path))
    monkeypatch.setattr(live_window, "fetch_live_window",
                        functools.partial(live_window.fetch_live_window, make_fetcher=stack.stream.fetcher))
    stack.heard = []

    def m3_asr(req):
        name = _multipart_fields(req)["file"]["filename"]
        stack.heard.append(name)
        if name.endswith(".m4a"):   # the opening check's two minutes
            return 200, {"text": OPEN, "model": "parakeet-ultra", "engine": "chain"}
        words = _words()
        return 200, {"text": " ".join(w["word"] for w in words), "model": "parakeet-ultra", "engine": "chain",
                     "words": words}
    stack.edge.route("POST", r"/m3/v1/audio/transcriptions$", m3_asr)
    stack.edge.route("CONNECT", r"", lambda req: (200, b""))   # the Mullvad proxy's health check
    for key, value in {
        **stack.rclone_env(),
        "RUNTIME_CONTROLLER_API_URL": stack.controller_url, "RUNTIME_CONTROLLER_BEARER_TOKEN": "chain-token",
        "RSS_AUDIO_RUNTIME_WORKER_ID": "chain-rss", "RSS_AUDIO_BOT_TOKEN": "123:chain",
        "RSS_AUDIO_WORK_DIR": str(stack.root / "rss-work"), "RSS_AUDIO_FAIL_LOG_DIR": str(stack.root / "rss-fail"),
        "RSS_AUDIO_SCRIBE_DB_PATH": str(stack.root / "scribe.sqlite3"),
        "RSS_AUDIO_B2_REMOTE": "chainfs", "RSS_AUDIO_B2_BUCKET": str(stack.bucket),
        "RSS_AUDIO_BASE_URL": stack.edge.url, "RSS_AUDIO_API_BASE_URL": stack.edge.url, "RSS_AUDIO_API_TOKEN": "chain",
        "RSS_AUDIO_MULLVAD_PROXY": stack.edge.url, "RSS_AUDIO_MAC_SSH": "", "RSS_AUDIO_ENRICHMENT_JOBS": "1",
        "RSS_AUDIO_YTDLP_BIN": str(_yt_dlp(tmp_path / "bin", stack.root / "yt-dlp.jsonl")),
        "TRANSCRIBE_AUDIO_SELF_HOSTED_URL": stack.edge.url + "/m3/v1", "TRANSCRIBE_AUDIO_SELF_HOSTED_TOKEN": "chain",
    }.items():
        monkeypatch.setenv(key, value)
    yield stack
    stack.stop()


def _schedule(now):
    """One run of the nightly timer on the shipped radio source: (its lines, its exit status)."""
    import scheduled_sources as sched
    from config import load_settings
    from runtime_client import RuntimeClient
    radio = [source for source in sched.load_inventory(sched.DEFAULT_INVENTORY) if source["id"] == "surveillance-radio"]
    out = io.StringIO()
    status = sched.run(radio, RuntimeClient(load_settings()), now, out=out)
    return out.getvalue().splitlines(), status


async def _drain(stack):
    """Run the worker until no job is runnable and every result is projected."""
    from config import load_settings
    from runtime_worker import worker_loop
    jobs = lambda: stack.psql("select producer_ref || '=' || status from runtime.jobs order by created_at")
    await run_until(lambda: worker_loop(load_settings()),
                    lambda: not stack.psql("select 1 from runtime.jobs where status in ('pending','leased','retryable')")
                    and not stack.api("GET", "/v1/results?resultOwner=rss-audio&limit=10"), timeout=600, describe=jobs)
    return jobs().splitlines()


def _exact_get(stack, url):
    prefix = stack.edge.url + "/"
    assert url.startswith(prefix), url
    return (stack.bucket / url[len(prefix):]).read_bytes()


def test_a_radio_window_is_published_overnight_with_its_notes_cut_at_the_hour_seams(stack):
    assert _schedule(NOW) == (["surveillance-radio Bloomberg Surveillance Radio 11/3/2026: queued"], 0)
    jobs = asyncio.run(_drain(stack))
    assert jobs == ["surveillance-radio:2026-11-03=succeeded", "surveillance-radio:2026-11-03:notes=succeeded"], \
        stack.psql("select last_error_class || ': ' || last_error_text from runtime.jobs where status = 'dead'")
    assert _schedule(NOW) == (["surveillance-radio Bloomberg Surveillance Radio 11/3/2026: published"], 0)

    # yt-dlp found the stream on the Mullvad route; the opening was heard before the rest was fetched
    [lookup] = [json.loads(line) for line in (stack.root / "yt-dlp.jsonl").read_text().splitlines()]
    assert lookup[lookup.index("--proxy") + 1] == stack.edge.url and lookup[-1].endswith("v=f39oHo6vFLg")
    clip = math.ceil(120 * 1000 / SEGMENT_MS) + 1
    requested = stack.stream.requested
    assert requested[0] == 0 and set(requested[1:clip + 1]) == set(range(FIRST, FIRST + clip))
    assert sorted(set(requested[1:])) == list(range(FIRST, FIRST + COUNT))
    assert [name.rsplit(".", 1)[1] for name in stack.heard] == ["m4a", "mp3"]

    [episode] = stack.edge.episodes["surveillance"]
    assert (episode["title"], episode["pub_date"]) == ("Bloomberg Surveillance Radio 11/3/2026", "2026-11-03T12:00:00Z")
    assert len(_exact_get(stack, episode["mp3_url"])) == episode["mp3_size"]
    notes = _exact_get(stack, episode["md_url"]).decode()
    hours = [line for line in notes.splitlines() if line.startswith("## Hour")]
    assert hours == ["## Hour 1 · 07:00–08:00 EST", "## Hour 2 · 08:00–09:00 EST", "## Hour 3 · 09:00–10:00 EST"], notes
    manifest = json.loads(_exact_get(stack, episode["manifest_url"]))
    assert [segment.get("rule") for segment in manifest["analysis"]["segments"]] == ["start", "seam", "seam"], manifest
    assert {c["step"] for c in model_calls(stack.root / "models.jsonl")} == {"topics", "mentions"}
    assert episode["page_url"].endswith(".page.html") and "Federal Reserve" in _exact_get(stack, episode["page_url"]).decode()
    assert stack.edge.sent() == []
