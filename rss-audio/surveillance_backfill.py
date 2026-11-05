"""surveillance_backfill.py: the X-76 Surveillance backfill, one fixed inventory. Dry run by default.

    surveillance_backfill.py [--apply] [--only SELECTOR ...]

A selector is an item key, or ``[SOURCE:]FROM[..TO]`` (air days, inclusive), to pace the run, e.g.
``--only surveillance-tv:2026-10-14..2026-10-16``.

* **Rebuild** (``rebuild.py``: speech-to-text from the stored mp3 into a new version, then hour-split
  notes): radio and TV of 3 Nov, radio of 28 and 29 Oct (published with the dictation-vocab damage).
* **Publish from a capture**: radio of 30 Oct and 2 Nov. Their window jobs were cancelled; the windows
  were captured with ``live_window`` into ``/var/tmp/sv-capture/<day>/radio-<day>.mp3``. Each becomes an
  ordinary local-file publish with the timer's own title, ``publishedAt``, segmenter and window context
  (``scheduled_sources.window_item``), keyed ``<item key>:local``.
* **TV days before the timer's ``since``**: 14 shows, 14 Oct to 2 Nov, pinned here (the playlist's Atom
  feed lists only its newest 15 entries, so they fall out of it day by day). Each is the nightly's own
  item (``scheduled_sources.playlist_item``): keyed ``surveillance-tv:<video id>``, the timer's title,
  ``publishedAt`` (the feed's published time) and segmenter.
* **Pages** (``notes_job.run_page``, no model call): every Surveillance episode that has notes and no
  ``page_url`` gets its page from its stored notes and script, keyed per episode,
  ``surveillance-page:<slug>``. An
  episode with another job still running (a rebuild, say) waits, since that job writes its own page.
  While the episode API has no ``page_url`` column, the pages wait.

Each item's jobs carry its key as their producer ref. An item with a succeeded job is done and one with
a job still pending, leased or retrying is running: either is reported and nothing is submitted, so a
second run after a finished rebuild does not plan the next version. Only an item with no job, or only
dead or cancelled ones, is planned. Every job runs below the scheduled publishes (``rebuild.PRIORITY``). An item that cannot be planned prints why and makes the run
exit 1; the others still go. Reads the episode API with the worker unit's settings; writes only by
submitting jobs (``--apply``).
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import notes_job
import rebuild
import scheduled_sources as sched

FEED = "surveillance"
CAPTURE = Path("/var/tmp/sv-capture")
REBUILD = (("surveillance-radio", "2026-11-03"), ("surveillance-tv", "2026-11-03"),
           ("surveillance-radio", "2026-10-28"), ("surveillance-radio", "2026-10-29"))
LOCAL = (("surveillance-radio", "2026-10-30"), ("surveillance-radio", "2026-11-02"))
# (video id, the Atom feed's published time), read from the playlist feed on 4 Nov 2026.
TV = (("9bCiqNAyJT8", "2026-10-14", "2026-10-14T16:02:33Z"), ("kqIZbRqSLuU", "2026-10-15", "2026-10-15T15:39:19Z"),
      ("j2jSLruhTz8", "2026-10-16", "2026-10-16T15:45:26Z"), ("AIKBTD63Ff0", "2026-10-19", "2026-10-19T15:01:23Z"),
      ("LF-xV92nVX0", "2026-10-20", "2026-10-20T15:21:14Z"), ("5MJdjLcDUF0", "2026-10-21", "2026-10-21T15:04:25Z"),
      ("RT7Wizxyy_w", "2026-10-22", "2026-10-22T15:19:26Z"), ("1cpN2NvJepQ", "2026-10-23", "2026-10-23T15:18:02Z"),
      ("iyvEQqRmHIs", "2026-10-26", "2026-10-26T15:45:22Z"), ("42SaCJ3fv38", "2026-10-27", "2026-10-27T15:44:19Z"),
      ("KVyBp7SFrxg", "2026-10-28", "2026-10-28T15:36:58Z"), ("isJd6_0V7Bk", "2026-10-29", "2026-10-29T15:52:08Z"),
      ("aGX2Ezs0FG4", "2026-10-30", "2026-10-30T15:38:45Z"), ("JQZkM3iXIpw", "2026-11-02", "2026-11-02T16:16:32Z"))
SELECTOR = re.compile(r"^(?:(?P<source>[a-z0-9-]+):)?(?P<first>\d{4}-\d{2}-\d{2})(?:\.\.(?P<last>\d{4}-\d{2}-\d{2}))?$")


def selected(only: tuple[str, ...], key: str, source_id: str, day: date) -> bool:
    """Whether an item is chosen: no selectors, its key, or a [SOURCE:]FROM[..TO] range that covers it."""
    if not only:
        return True
    for token in only:
        if token == key:
            return True
        if m := SELECTOR.match(token):
            first = date.fromisoformat(m["first"])
            last = date.fromisoformat(m["last"]) if m["last"] else first
            if (m["source"] in (None, source_id)) and first <= day <= last:
                return True
    return False


def check_selectors(only: tuple[str, ...], keys: set[str]) -> list[str]:
    """Selectors that are neither an item key nor a day range (a typo would otherwise select nothing)."""
    return [t for t in only if t not in keys and not SELECTOR.match(t)]


def _title(source: Mapping[str, Any], day: date) -> str:
    return source["title"].replace("{date}", sched._short_date(day))


def _rebuild(source: Mapping[str, Any], day: date, episodes: list[dict]) -> tuple[str, dict]:
    title = _title(source, day)
    rows = [row for row in episodes if row.get("title") == title]
    if len(rows) != 1:
        raise ValueError(f"{len(rows)} episodes titled {title!r} in {FEED}")
    context = (sched.window_item(source, day).input_json.get("segmentContext")
               if source["kind"] == "youtube_live_window" else None)
    episode = rows[0]
    version = rebuild.next_version(episode)
    job = rebuild.rebuild_input(episode, FEED, version=version, segmenter=source.get("segmenter"),
                                segment_context=context)
    line = (f"rebuild  {title}: {episode['slug']} v{version} "
            f"(now script {'v' + str(version - 1) if episode.get('speech_text_url') else 'none'}, "
            f"notes {'yes' if episode.get('md_url') else 'none'}), {source.get('segmenter') or 'single'}")
    return line, rebuild.rebuild_job_payload(job, producer_ref=f"{source['id']}:{day.isoformat()}:rebuild")


def _local(source: Mapping[str, Any], day: date, capture: Path) -> tuple[str, dict]:
    item = sched.window_item(source, day)
    path = capture / day.isoformat() / f"radio-{day.isoformat()}.mp3"
    if not path.is_file():
        raise ValueError(f"no capture at {path}")
    job = {k: v for k, v in item.input_json.items() if k != "liveWindow"}
    job.update(inputKind="local_file", sourceKey=f"{item.key}:local", localSourcePath=str(path))
    local = sched.Item(item.source_id, f"{item.key}:local", item.title, job)
    line = (f"publish  {item.title}: {path} ({path.stat().st_size // 1_000_000} MB), "
            f"{job.get('segmenter') or 'single'}, window {job.get('segmentContext', {}).get('start')}")
    return line, sched.job_payload(local, priority=rebuild.PRIORITY)


LIVE = {"pending", "leased", "retryable"}


def _already(key: str, jobs: list[dict]) -> str | None:
    """The line for an item that is done or running, from the controller's jobs; None to plan it."""
    mine = sorted((j for j in jobs if j.get("producerRef") == key), key=lambda j: j.get("createdAt") or "")
    notes = sorted((j for j in jobs if j.get("producerRef") == f"{key}:notes"), key=lambda j: j.get("createdAt") or "")
    done = [j for j in mine if j.get("status") == "succeeded"]
    if done:
        tail = f", notes {notes[-1]['status']}" if notes else ""
        return f"done     {key}: job {done[-1]['jobId']} succeeded {str(done[-1].get('updatedAt'))[:16]}{tail}"
    running = [j for j in mine if j.get("status") in LIVE]
    if running:
        return f"running  {key}: job {running[-1]['jobId']} {running[-1]['status']}"
    return None


def _tv(source: Mapping[str, Any], video_id: str, day: date, published: str) -> tuple[str, dict]:
    entry = sched.Entry(video_id=video_id, title=f"Bloomberg Surveillance {sched._short_date(day)}",
                        published=datetime.fromisoformat(published.replace("Z", "+00:00")))
    item = sched.playlist_item(source, entry)
    line = f"publish  {item.title}: {item.input_json['sourceUrl']}, {item.input_json.get('segmenter') or 'single'}"
    return line, sched.job_payload(item, priority=rebuild.PRIORITY)


def _page_items(episodes: list[dict], jobs: list[dict]) -> list[tuple[str, str, date, Callable]]:
    items = []
    for row in episodes:
        shown = sched._DATE_TEXT.search(str(row.get("title") or ""))
        if not row.get("md_url") or row.get("page_url") or shown is None:
            continue
        month, day_, year = (int(part) for part in shown.group(0).split("/"))
        source_id = "surveillance-radio" if " Radio " in f" {row['title']} " else "surveillance-tv"
        busy = [j for j in jobs if (j.get("inputJson") or {}).get("title") == row["title"] and j.get("status") in LIVE
                and (j.get("inputJson") or {}).get("inputKind") != notes_job.PAGE_KIND]

        def build(src, day, row=row, busy=busy):
            if busy:
                return f"waiting  {row['title']}: page after job {busy[0]['jobId']} ({busy[0]['status']})", None
            page = notes_job.page_input(row, FEED)
            return (f"page     {row['title']}: {row['slug']} v{page['version']} from its stored notes and script",
                    notes_job.page_job_payload(page, producer_ref=f"surveillance-page:{row['slug']}"))
        day = date(year, month, day_)
        items.append((f"surveillance-page:{row['slug']}", source_id, day, build))   # per episode, not per day
    return sorted(items, key=lambda item: (item[2], item[1]))


def inventory(episodes: list[dict], capture: Path, jobs: list[dict] = ()) -> list[tuple[str, str, date, Callable]]:
    """(key, source id, air day, build) for every item, in order: each key is also its jobs' producer ref."""
    items = [(f"{s}:{d}:rebuild", s, date.fromisoformat(d), lambda src, day: _rebuild(src, day, episodes))
             for s, d in REBUILD]
    items += [(f"{s}:{d}:local", s, date.fromisoformat(d), lambda src, day: _local(src, day, capture)) for s, d in LOCAL]
    items += [(f"surveillance-tv:{video}", "surveillance-tv", date.fromisoformat(d),
               lambda src, day, video=video, published=published: _tv(src, video, day, published))
              for video, d, published in TV]
    if any("page_url" in row for row in episodes):   # the episode API has the column (d4bd8086)
        items += _page_items(episodes, list(jobs))
    return items


def plan(sources: list[dict], episodes: list[dict], capture: Path = CAPTURE, only: tuple[str, ...] = (),
         jobs: list[dict] = ()) -> list[tuple[str, str, dict | None, bool]]:
    """(key, line, payload to submit or None, ok) for every selected item of the inventory."""
    by_id = {source["id"]: source for source in sources}
    items = inventory(episodes, capture, jobs)
    out = [("pages", "pages    waiting: the episode API has no page_url column yet", None, True)] if episodes and not any(
        "page_url" in row for row in episodes) and not only else []
    out += [(token, f"cannot plan {token}: not an item key or a [SOURCE:]FROM[..TO] day range", None, False)
           for token in check_selectors(only, {key for key, *_ in items})]
    for key, source_id, day, build in items:
        if not selected(only, key, source_id, day):
            continue
        if line := _already(key, list(jobs)):
            out.append((key, line, None, True))
            continue
        try:
            line, payload = build(by_id[source_id], day)
        except (ValueError, KeyError) as exc:
            out.append((key, f"cannot plan {key}: {exc}", None, False))
            continue
        out.append((key, line, payload, True))
    return out


def run(sources, episodes, submit: Callable[[dict], Any] | None, out=None, capture: Path = CAPTURE,
        only: tuple[str, ...] = (), jobs: list[dict] = ()) -> int:
    out = out or sys.stdout
    failed = 0
    for key, line, payload, ok in plan(sources, episodes, capture, only, jobs):
        print(line, file=out)
        if not ok:
            failed += 1
        elif payload is not None and submit is not None:
            print(f"         {rebuild.submit_line(payload, submit(payload) or {})}", file=out)
    if submit is None:
        print("dry run: nothing submitted (--apply submits)", file=out)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="submit the jobs (default: print the plan)")
    parser.add_argument("--only", nargs="*", default=(),
                        help="item keys or [SOURCE:]FROM[..TO] day ranges, e.g. surveillance-tv:2026-10-14..2026-10-16")
    args = parser.parse_args(argv)
    import config
    import pipeline
    from runtime_client import RuntimeClient
    try:
        settings = config.worker_settings()[0]
    except Exception as exc:   # the environment holds credentials: name the type only
        print(f"backfill: {config.WORKER_UNIT} settings unreadable ({type(exc).__name__})", file=sys.stderr)
        return 1
    episodes = pipeline._api_json(settings, "GET", f"/api/episodes?feed={FEED}")
    client = RuntimeClient(settings)
    jobs = client._request("GET", "/v1/jobs", query={"producerSystem": "rss-audio", "producerRefPrefix": "surveillance-",
                                                     "limit": 500}, timeout=10)["items"]
    return run(sched.load_inventory(sched.DEFAULT_INVENTORY), episodes, client.submit_job if args.apply else None,
               only=tuple(args.only), jobs=jobs)


if __name__ == "__main__":
    raise SystemExit(main())
