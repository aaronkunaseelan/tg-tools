"""Scheduled sources: publish expected recurring items (a YouTube playlist, a live-stream window)
as rss-audio jobs, with no Telegram message.

Run by ``rss-audio-schedule.timer`` (``python scheduled_sources.py``). Each run lists what each
source in ``config/scheduled-sources.json`` should have by now, submits it exactly as the gateway
submits a Telegram message (source receipt -> job -> link), then reads every item's job and prints
one line per item. A resubmit is a no-op: the idempotency key (`<source>:<video id | air date>`)
names the item, so each run only reports state for items already submitted. Exit status is
nonzero when a source cannot be listed or any item's job failed (an item that ended more than 36 h ago
was reported by an earlier run and is only printed), except ``window_not_show`` (the
stream carried something else that day; recorded as skipped, with what was heard). The skipped job is
dead in the controller, so tg-tools telemetry shows it and the supervisor checks whether the show
really did not air (X-75).

A playlist is listed from its Atom feed. When the feed fails (it has answered 404 for days at a time, F-150),
the same playlist is listed with yt-dlp by the source's download route: one flat listing, plus one video
metadata read per new show for its published time (an item with a job keeps its job's). Either path gives the
same items, keyed on the video; the run prints a line when yt-dlp listed them, and names both errors if both fail.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from tg_tools_common.intent_contract import RuntimeJobIntent
from tg_tools_common.runtime_contract import SKIPPED_ERROR_CLASS, source_receipt_payload

from config import load_settings
from pipeline import FEEDS
from runtime_client import RuntimeClient
from source_audio import SourceRoute

logger = logging.getLogger(__name__)

DEFAULT_INVENTORY = Path(__file__).resolve().parents[1] / "config" / "scheduled-sources.json"
SOURCE_SYSTEM = "schedule"
RESULT_ROUTE = "rss-audio.scheduled.publish.v1"
# The controller claims higher numbers first; Telegram rss jobs are 60.
PRIORITY = 20
MAX_ATTEMPTS = 3
# A dead item is a failure of the run that first sees it dead, so one lost item raises one alert, not one
# per night while it stays inside the lookback. Runs are 24 h apart (10 min of jitter), so a job that
# ended within this long ago was not seen dead by the previous run. The job's updatedAt is its end time.
FRESH_FAILURE = timedelta(hours=36)
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
_DATE_TEXT = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
KINDS = ("youtube_playlist", "youtube_live_window")
# Newest playlist entries the yt-dlp listing reads (the Atom feed carries 15; the lookback needs about 5).
LISTING_DEPTH = 30


class InventoryError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    source_id: str
    key: str                 # idempotency key and source key
    title: str
    input_json: dict[str, Any]


@dataclass(frozen=True)
class Entry:
    video_id: str
    title: str
    published: datetime | None   # None only from the yt-dlp listing, until the item's time is known


# -- inventory ---------------------------------------------------------------------------------

def load_inventory(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError(f"cannot read {path}: {exc}") from exc
    sources = data.get("sources") if isinstance(data, dict) and data.get("version") == 1 else None
    if not isinstance(sources, list) or not sources:
        raise InventoryError(f"{path}: expected {{'version': 1, 'sources': [...]}} with at least one source")
    seen: set[str] = set()
    for source in sources:
        _validate_source(source)
        if source["id"] in seen:
            raise InventoryError(f"duplicate source id {source['id']!r}")
        seen.add(source["id"])
    return sources


def _validate_source(source: Any) -> None:
    if not isinstance(source, dict):
        raise InventoryError("every source must be an object")
    name = source.get("id")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise InventoryError(f"source id must be lowercase letters, digits and '-': {name!r}")
    kind = source.get("kind")
    if kind not in KINDS:
        raise InventoryError(f"{name}: kind must be one of {', '.join(KINDS)}")
    needed = {"feed", "since", "title", "lookbackDays"} | (
        {"playlistId", "titleRegex", "preferredRoute"} if kind == "youtube_playlist" else
        {"videoId", "timezone", "weekdays", "start", "seconds", "expectOpening", "settleMinutes"})
    missing = sorted(needed - set(source))
    if missing:
        raise InventoryError(f"{name}: missing {', '.join(missing)}")
    if source["feed"] not in FEEDS:
        raise InventoryError(f"{name}: unknown feed {source['feed']!r}")
    try:
        date.fromisoformat(source["since"])
        if kind == "youtube_playlist":
            re.compile(source["titleRegex"])
            SourceRoute(source["preferredRoute"])
        else:
            ZoneInfo(source["timezone"])
            time.fromisoformat(source["start"])
    except (ValueError, KeyError, OSError, re.error) as exc:
        raise InventoryError(f"{name}: {exc}") from exc
    if "{date}" not in source["title"]:
        raise InventoryError(f"{name}: title must contain {{date}}")
    if "segmenter" in source:   # the notes job splits the episode with it (notes_job.SEGMENTERS)
        from notes_job import SEGMENTERS
        if source["segmenter"] not in SEGMENTERS:
            raise InventoryError(f"{name}: unknown segmenter {source['segmenter']!r} "
                                 f"(known: {', '.join(sorted(SEGMENTERS))})")
    for field in ("lookbackDays",) + (("seconds", "settleMinutes") if kind == "youtube_live_window" else ()):
        if isinstance(source[field], bool) or not isinstance(source[field], int) or source[field] < 0:
            raise InventoryError(f"{name}: {field} must be a non-negative integer")
    if kind == "youtube_live_window" and not (
            isinstance(source["weekdays"], list) and source["weekdays"]
            and all(isinstance(day, int) and 1 <= day <= 7 for day in source["weekdays"])):
        raise InventoryError(f"{name}: weekdays must be ISO weekday numbers 1 (Mon) .. 7 (Sun)")


# -- expected items ----------------------------------------------------------------------------

def parse_playlist_feed(body: bytes) -> list[Entry]:
    try:
        root = ET.fromstring(body)
        entries = [Entry(video_id=entry.findtext(f"{YT}videoId") or "", title=(entry.findtext(f"{ATOM}title") or ""),
                         published=datetime.fromisoformat((entry.findtext(f"{ATOM}published") or "").replace("Z", "+00:00"))
                         .astimezone(UTC))
                   for entry in root.iter(f"{ATOM}entry")]
    except (ET.ParseError, ValueError) as exc:
        raise InventoryError(f"playlist feed is not a readable Atom feed: {exc}") from exc
    if not entries or any(not entry.video_id for entry in entries):
        raise InventoryError("playlist feed has no usable entries")
    return entries


def fetch_playlist_feed(playlist_id: str) -> bytes:
    request = urllib.request.Request(
        f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}", headers={"User-Agent": "rss-audio-schedule/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _short_date(day: date) -> str:
    return f"{day.month}/{day.day}/{day.year}"


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _base_input(source: Mapping[str, Any], key: str, input_kind: str, title: str, published_at: datetime) -> dict[str, Any]:
    job = {"kind": "rss", "sourceKey": key, "inputKind": input_kind, "feed": source["feed"], "title": title,
           "publishedAt": _iso_z(published_at), "delivery": "none"}
    if source.get("segmenter"):
        job["segmenter"] = source["segmenter"]
    return job


class YtDlpPlaylist:
    """The second way to list a playlist (F-150), used only when its Atom feed fails: yt-dlp by the route the
    source's downloads take, downloading nothing. ``entries`` is one flat listing call. Its entries carry no
    exact time (a flat entry's ``timestamp``, when yt-dlp gives one, is parsed from "2 days ago"), so
    ``published`` reads one video's metadata; ``_listed_entries`` asks it only for a new show."""

    def __init__(self, tool_settings: Callable[[], Any], fetch_json: Callable[..., dict] | None = None):
        self._tool_settings, self._settings = tool_settings, None
        self._fetch_json = fetch_json

    def _json(self, source: Mapping[str, Any], url: str, **kwargs) -> dict:
        if self._fetch_json is None:
            from source_audio import fetch_source_json_for_route
            self._fetch_json = fetch_source_json_for_route
        if self._settings is None:
            self._settings = self._tool_settings()
        return self._fetch_json(self._settings, url, SourceRoute(source["preferredRoute"]), **kwargs)

    def entries(self, source: Mapping[str, Any]) -> list[Entry]:
        data = self._json(source, f"https://www.youtube.com/playlist?list={source['playlistId']}",
                          playlist_end=LISTING_DEPTH)
        rows = data.get("entries")
        entries = [Entry(video_id=str(row.get("id") or ""), title=str(row.get("title") or ""), published=None)
                   for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        if not entries or any(not entry.video_id for entry in entries):
            raise InventoryError("yt-dlp listing has no usable entries")
        return entries

    def published(self, source: Mapping[str, Any], video_id: str) -> datetime:
        stamp = self._json(source, f"https://www.youtube.com/watch?v={video_id}").get("timestamp")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            raise InventoryError(f"yt-dlp gives no published time for {video_id}")
        return datetime.fromtimestamp(stamp, UTC)


def expected_items(source: Mapping[str, Any], now: datetime,
                   fetch_feed: Callable[[str], bytes] | None = None, *, second: YtDlpPlaylist | None = None,
                   job_published: Callable[[str], datetime | None] | None = None,
                   notes: list[str] | None = None) -> list[Item]:
    """The source's items due by ``now``. A playlist is listed from its Atom feed, or when that fails and
    ``second`` is given, by ``second`` (and a line saying so goes to ``notes``). ``job_published(key)`` is the
    published time of the item's existing job, if any; the yt-dlp path takes it instead of reading the video."""
    now = now.astimezone(UTC)
    fetch_feed = fetch_feed or fetch_playlist_feed
    since = date.fromisoformat(source["since"])
    if source["kind"] == "youtube_playlist":
        return _playlist_items(source, now, since, fetch_feed, second, job_published, notes)
    return _window_items(source, now, since)


def _playlist_items(source, now: datetime, since: date, fetch_feed, second=None, job_published=None,
                    notes=None) -> list[Item]:
    pattern, cutoff = re.compile(source["titleRegex"]), now - timedelta(days=source["lookbackDays"])
    try:
        entries = parse_playlist_feed(fetch_feed(source["playlistId"]))
    except (OSError, InventoryError) as feed_error:   # OSError: urllib's HTTPError and URLError, timeouts
        if second is None:
            raise
        try:
            entries = _listed_entries(source, second, pattern, cutoff, since, job_published or (lambda key: None))
        except Exception as exc:
            raise InventoryError(f"feed: {feed_error}; yt-dlp: {exc}") from exc
        if notes is not None:
            notes.append(f"{source['id']}: listed by yt-dlp (feed: {feed_error})")
    items = []
    for entry in entries:
        if not pattern.fullmatch(entry.title) or entry.published < cutoff or entry.published.date() < since:
            continue
        items.append(playlist_item(source, entry))
    return sorted(items, key=lambda item: item.input_json["publishedAt"])


def _listed_entries(source, second: YtDlpPlaylist, pattern, cutoff: datetime, since: date,
                    job_published: Callable[[str], datetime | None]) -> list[Entry]:
    """The yt-dlp listing's entries that could be due, each with its published time: its job's when it has one,
    else the video's own. The date in the title (the air day) keeps old shows from costing a video read; a day
    of slack covers a show published the day after it aired."""
    entries = []
    for entry in second.entries(source):
        if not pattern.fullmatch(entry.title):
            continue
        day = _title_day(entry.title)
        if day is not None and (day < since or day < cutoff.date() - timedelta(days=1)):
            continue
        published = job_published(f"{source['id']}:{entry.video_id}") or second.published(source, entry.video_id)
        entries.append(replace(entry, published=published.astimezone(UTC)))
    return entries


def _title_day(title: str) -> date | None:
    shown = _DATE_TEXT.search(title)
    if shown is None:
        return None
    month, day, year = (int(part) for part in shown.group(0).split("/"))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def jobs_published(client, source_id: str) -> Callable[[str], datetime | None]:
    """``key -> publishedAt`` of the item's existing publish job, read from the controller once, on first use."""
    known: dict[str, datetime] | None = None

    def lookup(key: str) -> datetime | None:
        nonlocal known
        if known is None:
            known = {}
            for job in client.list_jobs(producer_ref_prefix=f"{source_id}:", limit=200)["items"]:
                stamp = (job.get("inputJson") or {}).get("publishedAt")
                if job.get("producerRef") and stamp:
                    try:
                        known.setdefault(job["producerRef"], datetime.fromisoformat(str(stamp).replace("Z", "+00:00")))
                    except ValueError:
                        continue
        return known.get(key)
    return lookup


def playlist_item(source: Mapping[str, Any], entry: Entry) -> Item:
    """The item of one ``youtube_playlist`` entry: keyed on its video, titled by the date in its title."""
    shown = _DATE_TEXT.search(entry.title)
    if shown is None:
        raise InventoryError(f"{source['id']}: no date in matching title {entry.title!r}")
    key, title = f"{source['id']}:{entry.video_id}", source["title"].replace("{date}", shown.group(0))
    job = _base_input(source, key, "url", title, entry.published)
    job.update(sourceUrl=f"https://www.youtube.com/watch?v={entry.video_id}", preferredRoute=source["preferredRoute"])
    return Item(source["id"], key, title, job)


def _window_items(source, now: datetime, since: date) -> list[Item]:
    today = now.astimezone(ZoneInfo(source["timezone"])).date()
    items = []
    for back in range(source["lookbackDays"], -1, -1):
        day = today - timedelta(days=back)
        if day < since or day.isoweekday() not in source["weekdays"]:
            continue
        item = window_item(source, day)
        if now < datetime.fromisoformat(item.input_json["liveWindow"]["start"].replace("Z", "+00:00")) + timedelta(
                seconds=source["seconds"], minutes=source["settleMinutes"]):
            continue
        items.append(item)
    return items


def window_item(source: Mapping[str, Any], day: date) -> Item:
    """The item of one air day of a ``youtube_live_window`` source, due or not."""
    start = datetime.combine(day, time.fromisoformat(source["start"]), tzinfo=ZoneInfo(source["timezone"])).astimezone(UTC)
    key, title = f"{source['id']}:{day.isoformat()}", source["title"].replace("{date}", _short_date(day))
    job = _base_input(source, key, "live_window", title, start)
    job["liveWindow"] = {"videoId": source["videoId"], "start": _iso_z(start), "seconds": source["seconds"],
                         "expectOpening": source["expectOpening"]}
    if source.get("segmenter"):   # hour labels on the window's wall clock
        job["segmentContext"] = {"start": _iso_z(start), "timezone": source["timezone"]}
    return Item(source["id"], key, title, job)


# -- submit and report -------------------------------------------------------------------------

def job_payload(item: Item, priority: int = PRIORITY) -> dict[str, Any]:
    """The item's publish job, idempotent on its key."""
    return RuntimeJobIntent(
        producer_system="rss-audio", producer_ref=item.key, result_owner="rss-audio", result_route=RESULT_ROUTE,
        workload_type="rss_publish", queue_class="foreground", execution_profile="cpu_media",
        execution_shape="local_bounded", priority=priority, input_json=item.input_json,
        output_contract_json={"version": 1, "result": "rss episode publication, no delivery"},
        idempotency_key=item.key, effect_key=f"rss-audio:feed-publish:{item.key}",
        max_attempts=MAX_ATTEMPTS).to_submit_payload()


def submit_item(client, item: Item) -> dict[str, Any]:
    """Receipt, job and link, in the gateway's order. Every step is idempotent on the item's key."""
    receipt = client.admit_source_receipt(source_receipt_payload(
        source_system=SOURCE_SYSTEM, source_key=item.key, producer_system="rss-audio", workload_type="rss_publish",
        route_id=f"schedule.{item.source_id}.v1", route_kind="scheduled_source", channel_id=item.source_id,
        matched_rule_version="v1",
        source_json={"schemaVersion": 1, "sourceId": item.source_id, "itemKey": item.key,
                     "publishedAt": item.input_json["publishedAt"]}))
    receipt_id = ((receipt or {}).get("receipt") or {}).get("receiptId")
    if not receipt_id:
        raise RuntimeError("runtime admit_source_receipt returned no receiptId")
    submitted = client.submit_job(job_payload(item))
    job_id = ((submitted or {}).get("job") or {}).get("jobId")
    if not job_id:
        raise RuntimeError("runtime job submit returned no jobId")
    client.submit_source_receipt(receipt_id=str(receipt_id), job_id=str(job_id))
    return client.get_job(job_id=str(job_id))["job"]


def _fresh(job: Mapping[str, Any], now: datetime | None) -> bool:
    """Whether this run is the first to see the job's end. No usable end time counts as fresh: unknown is
    not quiet."""
    try:
        ended = datetime.fromisoformat(str(job["updatedAt"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return True
    return now is None or ended.tzinfo is None or now - ended <= FRESH_FAILURE


def describe_job(job: Mapping[str, Any], now: datetime | None = None) -> tuple[str, bool]:
    """(text for the item's line, whether it counts as a failure). A skip is not a failure here. A failure that an earlier
    run already reported (``now`` more than FRESH_FAILURE after the job ended) is printed, not counted."""
    text, bad = _describe(job)
    if bad and not _fresh(job, now):
        return f"{text} (reported by an earlier run)", False
    return text, bad


def _describe(job: Mapping[str, Any]) -> tuple[str, bool]:
    status, error = job.get("status"), job.get("lastErrorClass") or "unknown"
    if status == "succeeded":
        return "published", False
    if status == "dead":
        if error == SKIPPED_ERROR_CLASS:
            heard = " ".join(str(job.get("lastErrorText") or "").split())[:240]
            return f"skipped ({error}: {heard})" if heard else f"skipped ({error})", False
        return f"failed {error}", True
    if status == "cancelled":
        return "failed cancelled", True
    if status == "leased":
        return "running", False
    return "queued", False   # pending, or retryable (waiting for its next attempt)


def run(sources, client, now: datetime, *, fetch_feed=None, second: YtDlpPlaylist | None = None, out=None,
        submit: bool = True) -> int:
    out = out or sys.stdout
    failed = False
    for source in sources:
        notes: list[str] = []
        try:
            items = expected_items(source, now, fetch_feed, second=second, notes=notes,
                                   job_published=jobs_published(client, source["id"]) if client is not None else None)
        except Exception as exc:
            print(f"{source['id']}: cannot list items: {exc}", file=out)
            failed = True
            continue
        for note in notes:
            print(note, file=out)
        if not items:
            print(f"{source['id']}: nothing expected", file=out)
        for item in items:
            if not submit:
                print(f"{item.source_id} {item.title}: expected ({item.key}, published {item.input_json['publishedAt']})",
                      file=out)
                continue
            try:
                state, bad = describe_job(submit_item(client, item), now)
            except Exception as exc:
                state, bad = f"failed submit: {exc}", True
            print(f"{item.source_id} {item.title}: {state}", file=out)
            failed = failed or bad
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--now", help="ISO 8601 UTC time to act as (default: now)")
    parser.add_argument("--list", action="store_true", help="print the expected items; submit nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(UTC)
    if now.tzinfo is None:
        parser.error("--now must carry a UTC offset")
    try:
        sources = load_inventory(args.inventory)
    except InventoryError as exc:
        print(f"inventory: {exc}", file=sys.stderr)
        return 1
    client = None if args.list else RuntimeClient(load_settings())
    return run(sources, client, now, submit=not args.list, second=YtDlpPlaylist(_tool_settings))


def _tool_settings():
    """yt-dlp's settings for the second listing, read only when the feed fails (``--list`` needs none otherwise)."""
    from pipeline import _source_tool_settings
    return _source_tool_settings(load_settings())


if __name__ == "__main__":
    sys.exit(main())
