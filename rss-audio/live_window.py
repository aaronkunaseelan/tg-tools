"""A time window cut from a YouTube live stream's DASH buffer, through the OCI Mullvad route.

The buffer of a permanent live stream reaches back days. yt-dlp (``--live-from-start``) lists the
audio format (itag 140) with a URL; segment ``N`` is ``<url>&sq=N``. Every segment response, a 404
included, carries the stream head in ``X-Head-Seqnum``, ``X-Head-Time-Millis`` and ``X-Walltime-Ms``.
That fixes the segment length (head time / head sequence, 5.0 s) and maps a wall-clock start to a
segment number. Nothing is published unless the window's first two minutes say it is the expected
show (``expectOpening``): a holiday or special can carry other programming.

Outcomes: window not over at the head -> retryable; first segment gone from the buffer or the
opening is not the show -> terminal; segment or transport failures -> retryable. The caller turns
``LiveWindowError`` into the job's ``PipelineError`` (``error_class`` becomes the job's errorClass).
"""
from __future__ import annotations

import http.client
import json
import logging
import math
import random
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from tg_tools_common.runtime_contract import SKIPPED_ERROR_CLASS

from source_audio import (SourceToolSettings, _remote_component_argv, run_command,
                          sanitized_source_subprocess_env)

logger = logging.getLogger(__name__)

SEGMENT_SECONDS_RANGE = (4.9, 5.1)
FETCH_WORKERS = 8
SEGMENT_TRIES = 4
DURATION_TOLERANCE_SECONDS = 30
OPENING_CLIP_SECONDS = 120
MAX_WINDOW_SECONDS = 6 * 3600
STREAM_FORMAT_ID = "140"
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class LiveWindowError(RuntimeError):
    def __init__(self, error_class: str, message: str, *, retryable: bool, stderr: str = "") -> None:
        super().__init__(message)
        self.error_class = error_class
        self.retryable = retryable
        self.stderr = stderr


@dataclass(frozen=True)
class LiveWindow:
    video_id: str
    start: datetime          # UTC
    seconds: int
    expect_opening: str

    @property
    def source_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def identity(self) -> str:
        """What the episode is: this stream at this start, never the bytes."""
        return f"{self.video_id}@{self.start.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def parse_window(raw) -> LiveWindow:
    if not isinstance(raw, Mapping):
        raise LiveWindowError("input", "liveWindow must be an object", retryable=False)
    video_id, seconds, opening = raw.get("videoId"), raw.get("seconds"), raw.get("expectOpening")
    if not isinstance(video_id, str) or not _VIDEO_ID.match(video_id):
        raise LiveWindowError("input", "liveWindow.videoId must be a YouTube video id", retryable=False)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 60 <= seconds <= MAX_WINDOW_SECONDS:
        raise LiveWindowError("input", f"liveWindow.seconds must be an integer in 60..{MAX_WINDOW_SECONDS}",
                              retryable=False)
    if not isinstance(opening, str) or not _squash(opening):
        raise LiveWindowError("input", "liveWindow.expectOpening must be a non-empty string", retryable=False)
    try:
        start = datetime.fromisoformat(str(raw.get("start")).replace("Z", "+00:00"))
    except ValueError:
        raise LiveWindowError("input", "liveWindow.start must be an ISO 8601 UTC time", retryable=False) from None
    if start.tzinfo is None or start.utcoffset() is None:
        raise LiveWindowError("input", "liveWindow.start must carry a UTC offset", retryable=False)
    return LiveWindow(video_id, start.astimezone(UTC), seconds, opening.strip())


@dataclass(frozen=True)
class Head:
    seq: int
    time_ms: int
    wall_ms: int

    @property
    def segment_seconds(self) -> float:
        return self.time_ms / self.seq / 1000


@dataclass(frozen=True)
class Plan:
    first: int
    count: int
    segment_seconds: float


def head_from_headers(headers: Mapping[str, str]) -> Head:
    lowered = {str(key).lower(): value for key, value in headers.items()}
    try:
        head = Head(seq=int(lowered["x-head-seqnum"]), time_ms=int(lowered["x-head-time-millis"]),
                    wall_ms=int(lowered["x-walltime-ms"]))
    except (KeyError, TypeError, ValueError):
        raise LiveWindowError("window_head_unknown", "stream head headers missing from the segment response",
                              retryable=True) from None
    if head.seq <= 0 or head.time_ms <= 0:
        raise LiveWindowError("window_head_unknown", f"implausible stream head {head}", retryable=True)
    return head


def plan_window(window: LiveWindow, head: Head) -> Plan:
    seconds = head.segment_seconds
    low, high = SEGMENT_SECONDS_RANGE
    if not low <= seconds <= high:
        raise LiveWindowError("window_segment_duration",
                              f"segment duration {seconds:.3f}s is outside {low}-{high}s", retryable=False)
    # The head segment was at stream time `time_ms` when the wall clock read `wall_ms`.
    first = math.floor((window.start.timestamp() * 1000 - head.wall_ms + head.time_ms) / (seconds * 1000))
    count = round(window.seconds / seconds)
    if first + count > head.seq:
        raise LiveWindowError("window_pending",
                              f"window ends after the stream head (needs segment {first + count - 1}, "
                              f"head is {head.seq})", retryable=True)
    return Plan(first=first, count=count, segment_seconds=seconds)


@dataclass(frozen=True)
class SegmentResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = b""


Fetcher = Callable[[int], SegmentResponse]


def make_proxy_fetcher(proxy: str, base_url: str, *, timeout: float = 30) -> Fetcher:
    """Segment fetcher through the Mullvad HTTP proxy; non-200 statuses are returned, not raised."""
    split = urlsplit(base_url)
    if split.scheme != "https" or not (split.hostname or "").endswith(".googlevideo.com"):
        raise LiveWindowError("window_stream_url", "stream URL is not a googlevideo https URL", retryable=False)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    joiner = "&" if split.query else "?"

    def fetch(number: int) -> SegmentResponse:
        request = urllib.request.Request(f"{base_url}{joiner}sq={number}", headers={"User-Agent": "Mozilla/5.0"})
        try:
            with opener.open(request, timeout=timeout) as response:
                return SegmentResponse(response.status, {k.lower(): v for k, v in response.headers.items()},
                                       response.read())
        except urllib.error.HTTPError as exc:
            return SegmentResponse(exc.code, {k.lower(): v for k, v in exc.headers.items()})

    return fetch


def resolve_stream_url(settings: SourceToolSettings, video_id: str, *, run=run_command) -> str:
    """The itag-140 URL yt-dlp finds for the stream. SourceAudioError propagates (classified by the caller)."""
    command = [settings.yt_dlp_bin, "--proxy", settings.mullvad_proxy, *_remote_component_argv(settings),
               "--extractor-args", "youtube:player_client=default,-web,-web_safari,-android_sdkless",
               "--live-from-start", "-j", "--skip-download", "--no-playlist",
               f"https://www.youtube.com/watch?v={video_id}"]
    result = run(command, stage="metadata", timeout=180, env=sanitized_source_subprocess_env())
    try:
        formats = json.loads(result.stdout).get("formats") or []
    except (json.JSONDecodeError, AttributeError):
        raise LiveWindowError("window_format_unavailable", "yt-dlp returned invalid JSON", retryable=True,
                              stderr=result.stdout[:500]) from None
    for item in formats:
        if isinstance(item, dict) and str(item.get("format_id")) == STREAM_FORMAT_ID:
            url = item.get("fragment_base_url") or item.get("url")
            if isinstance(url, str) and url:
                return url
    raise LiveWindowError("window_format_unavailable", f"stream lists no format {STREAM_FORMAT_ID}", retryable=True)


def _squash(text: str) -> str:
    """Letters and digits only, lower case, single spaces: ``Bloomberg, Surveillance.`` is ``bloomberg surveillance``."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _fetch_one(fetch: Fetcher, number: int, path: Path, sleep, tries: int) -> int | None:
    """None when the segment is on disk, else the last HTTP status (-1 transport failure, -2 empty body)."""
    if path.exists() and path.stat().st_size > 0:
        return None
    last = -1
    for attempt in range(tries):
        try:
            response = fetch(number)
        except (OSError, http.client.HTTPException):
            last = -1
        else:
            if response.status == 200 and response.body:
                partial = path.with_suffix(".part")
                partial.write_bytes(response.body)
                partial.replace(path)
                return None
            last = response.status if response.status != 200 else -2
            if response.status == 404:
                return 404
        if attempt + 1 < tries:
            sleep(min(8.0, 0.5 * 2 ** attempt) * (0.5 + random.random() / 2))
    return last


def _fetch_range(fetch: Fetcher, numbers: range, directory: Path, *, first: int, check, sleep,
                 workers: int = FETCH_WORKERS, tries: int = SEGMENT_TRIES) -> None:
    failures: dict[int, int] = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(_fetch_one, fetch, n, directory / f"seg-{n}.bin", sleep, tries): n for n in numbers}
        for future in as_completed(futures):
            check()
            status = future.result()
            if status is not None:
                failures[futures[future]] = status
                if status == 404 and futures[future] == first:
                    break
                if len(failures) >= 5:
                    break
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if failures.get(first) == 404:
        raise LiveWindowError("window_outside_buffer",
                              f"segment {first} is older than the stream's buffer", retryable=False)
    if failures:
        shown = ", ".join(f"{n}:{status}" for n, status in sorted(failures.items())[:5])
        raise LiveWindowError("window_fetch", f"segments failed (segment:status) {shown}", retryable=True)


def _concat(run, ffmpeg_bin: str, directory: Path, numbers: range, output: Path) -> Path:
    listing = directory / f"concat-{output.stem}.txt"
    listing.write_text("".join("file '{}'\n".format(str(directory / f"seg-{n}.bin").replace("'", "'\\''"))
                               for n in numbers), encoding="utf-8")
    run([ffmpeg_bin, "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
         str(output)], stage="download", timeout=900, env=sanitized_source_subprocess_env())
    if not output.exists() or output.stat().st_size == 0:
        raise LiveWindowError("window_fetch", "ffmpeg joined no audio", retryable=True)
    return output


def _duration(run, ffprobe_bin: str, path: Path) -> float:
    result = run([ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of",
                  "default=noprint_wrappers=1:nokey=1", str(path)], stage="validate", timeout=60,
                 env=sanitized_source_subprocess_env())
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise LiveWindowError("window_duration", "ffprobe returned no duration", retryable=True) from None


def fetch_live_window(
    settings: SourceToolSettings,
    window: LiveWindow,
    work_dir: Path,
    *,
    ffprobe_bin: str,
    transcribe_clip: Callable[[Path], str],
    check: Callable[[], None] = lambda: None,
    resolve=resolve_stream_url,
    make_fetcher=make_proxy_fetcher,
    run=run_command,
    sleep=time.sleep,
) -> tuple[Path, float]:
    """Download the window and return (joined m4a, its duration in seconds).

    Completed segments stay in ``work_dir/live`` so a retry fetches only what is missing. The first
    two minutes are fetched and transcribed before the rest, so a window that is not the show costs
    one clip, not three hours.
    """
    directory = Path(work_dir) / "live"
    directory.mkdir(parents=True, exist_ok=True)
    fetch = make_fetcher(settings.mullvad_proxy, resolve(settings, window.video_id))
    check()
    try:
        head = head_from_headers(fetch(0).headers)
    except (OSError, http.client.HTTPException) as exc:
        raise LiveWindowError("window_fetch", f"stream head probe failed: {exc}", retryable=True) from exc
    plan = plan_window(window, head)
    logger.info("live window %s: segments %d..%d (%.3fs each, head %d)", window.identity, plan.first,
                plan.first + plan.count - 1, plan.segment_seconds, head.seq)

    numbers = range(plan.first, plan.first + plan.count)
    clip_numbers = numbers[:min(plan.count, math.ceil(OPENING_CLIP_SECONDS / plan.segment_seconds) + 1)]
    _fetch_range(fetch, clip_numbers, directory, first=plan.first, check=check, sleep=sleep)
    heard = transcribe_clip(_concat(run, settings.ffmpeg_bin, directory, clip_numbers, directory / "opening.m4a"))
    if _squash(window.expect_opening) not in _squash(heard):
        raise LiveWindowError(SKIPPED_ERROR_CLASS,
                              f"window opening does not say {window.expect_opening!r}: {_squash(heard)[:160]!r}",
                              retryable=False)

    _fetch_range(fetch, numbers[len(clip_numbers):], directory, first=plan.first, check=check, sleep=sleep)
    output = _concat(run, settings.ffmpeg_bin, directory, numbers, directory / "window.m4a")
    duration = _duration(run, ffprobe_bin, output)
    if abs(duration - window.seconds) > DURATION_TOLERANCE_SECONDS:
        for segment in directory.glob("seg-*.bin"):
            segment.unlink()
        raise LiveWindowError("window_duration", f"joined audio is {duration:.0f}s, expected {window.seconds}s "
                              f"(±{DURATION_TOLERANCE_SECONDS}s)", retryable=True)
    return output, duration
