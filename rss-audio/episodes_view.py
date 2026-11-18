"""rss-audio-episodes: the episodes a feed has stored, and which artefacts each one holds. Read only.

One line per episode (newest first, `--limit` of them, `--date` filters a day): the date, whether it is the
full show or a condensed edition, the clock base, the episode's own name and the artefacts present, with
`@n` on a kind whose newest name is a version above 1, `of <parent>` on a condensed edition and any suffix
the map cannot name under `other`. `--json` prints the same data as one object (schema
`rss-audio.episodes/1`).

The source is the object listing of ``<feed>/episodes/`` in the bucket the rss-audio paths upload to, read
with ``b2 ls``: names and nothing else, no download, no API key of the feed service, no write. A prefix
that holds nothing and a feed name that is not a prefix both print one line and exit 2; a listing that
fails prints ``unknown: <reason>`` and exits 1, never an empty table.

Everything printed goes through ``jobs_view.redact``: no URL, no token of 28+ characters survives (episode
names are lowercase slugs and are kept whole; they are the handle every other view takes).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable, Iterable

from jobs_view import clean, redact

SCHEMA = "rss-audio.episodes/1"
# The bucket is deployment config, not code: RSS_AUDIO_EPISODES_BUCKET, else the RSS_AUDIO_B2_BUCKET the
# rss-audio units upload to (bin/rss-audio-episodes reads it from ~/.config/tg-tools/rss-audio.env).
BUCKET = os.environ.get("RSS_AUDIO_EPISODES_BUCKET") or os.environ.get("RSS_AUDIO_B2_BUCKET") or ""
B2_BIN = os.environ.get("RSS_AUDIO_B2_BIN") or "b2"
DEFAULT_LIMIT = 20
TIMEOUT = 120

# The suffixes the rss-audio paths write (``episode_enrichment.naming``, ``notes_job``,
# ``surveillance_condensed``). Longest first: ``.segments.json`` must not be read as ``.json``.
KIND_SUFFIX = (
    (".structure.json", "structure"),
    (".chapters.json", "chapters"),
    (".segments.json", "segments"),
    (".manifest.json", "manifest"),
    (".plan.json", "plan"),
    (".script.md", "script"),
    (".notes.md", "notes"),
    (".page.html", "page"),
    (".mp3", "mp3"),
)
# The order kinds are listed in: the audio first, then the cut and the documents derived from it.
KIND_ORDER = ("mp3", "script", "segments", "notes", "manifest", "page", "chapters", "plan", "structure")
CONDENSED = "-condensed"
TAIL = re.compile(r"^(?P<stem>.+?)(?:\.v(?P<version>\d+))?(?P<suffix>(?:\.[A-Za-z0-9]+)+)$")
EPISODE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}|undated)-(?P<slug>.+)-(?P<digest>[0-9a-f]{12})"
                     r"(?P<condensed>-condensed)?$")
FEED = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Listing(Exception):
    """The object listing could not be read: reported, never shown as an empty feed."""


def suffix_of(name: str) -> tuple[str, int, str]:
    """One object name -> (episode stem, version, suffix). Unversioned names are version 1; a name with no
    file extension is its own stem with no suffix (kept, so nothing in the prefix is silently dropped)."""
    match = TAIL.match(name)
    if not match:
        return name, 1, ""
    return match.group("stem"), int(match.group("version") or 1), match.group("suffix")


def kind_of(suffix: str) -> str | None:
    for tail, kind in KIND_SUFFIX:
        if suffix == tail:
            return kind
    return None


def episode_of(stem: str) -> dict[str, Any]:
    """The name, day, edition and parent of one episode stem. A name off the ``<date>-<slug>-<digest>``
    shape keeps its own text and takes the date it starts with, if any."""
    match = EPISODE.match(stem)
    if match:
        condensed = bool(match.group("condensed"))
        return {"name": stem, "date": match.group("date"), "condensed": condensed,
                "parent": stem[: -len(CONDENSED)] if condensed else None}
    condensed = stem.endswith(CONDENSED) and len(stem) > len(CONDENSED)
    date = stem[:10] if DAY.match(stem[:10]) else "undated" if stem.startswith("undated") else ""
    return {"name": stem, "date": date, "condensed": condensed,
            "parent": stem[: -len(CONDENSED)] if condensed else None}


def clocks(condensed: bool, kinds: Iterable[str]) -> str:
    """The clock base a quote from this edition is timed in. Ordinary episodes carry no segments, so their
    clocks are the episode's own; a condensed edition's are the cut's."""
    if "segments" not in kinds:
        return "none"
    return "condensed-audio" if condensed else "audio"


def finish(found: dict[str, Any]) -> dict[str, Any]:
    kinds = [kind for kind in KIND_ORDER if kind in found["kinds"]]
    versions = {kind: found["versions"].get(kind, 1) for kind in kinds}
    return {"name": found["name"], "date": found["date"], "condensed": found["condensed"],
            "parent": found["parent"], "kinds": kinds, "versions": versions,
            "clocks": clocks(found["condensed"], kinds), "other": sorted(found["other"])}


def dated(episode: dict[str, Any]) -> bool:
    return bool(DAY.match(episode["date"]))


def order(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first, and within one day by name (so a full show precedes its condensed edition). Undated
    episodes come last, however their names sort."""
    episodes = sorted(episodes, key=lambda episode: episode["name"])
    return sorted(episodes, key=lambda episode: (dated(episode), episode["date"]), reverse=True)


def group(names: Iterable[str]) -> list[dict[str, Any]]:
    """Object names -> episodes, by the stem they share. A name whose suffix the map does not know is listed
    under ``other``, so an unexpected artefact is visible rather than lost."""
    found: dict[str, dict[str, Any]] = {}
    for raw in names:
        name = raw.strip()
        if not name or name.endswith("/"):
            continue                       # a folder in the prefix: nothing stored for one episode
        stem, version, suffix = suffix_of(name)
        episode = found.get(stem)
        if episode is None:
            episode = found[stem] = {**episode_of(stem), "kinds": set(), "versions": {}, "other": []}
        kind = kind_of(suffix)
        if kind is None:
            if suffix and suffix not in episode["other"]:
                episode["other"].append(suffix)
            continue
        episode["kinds"].add(kind)
        episode["versions"][kind] = max(episode["versions"].get(kind, 1), version)
    return order([finish(episode) for episode in found.values()])


def bare(name: str, feed: str) -> str:
    """``b2 ls`` prints keys from the bucket root; the episode name is what follows the prefix."""
    head = f"{feed}/episodes/"
    return name[len(head):] if name.startswith(head) else name.rsplit("/", 1)[-1]


def list_objects(feed: str) -> list[str]:
    """The names under ``<feed>/episodes/``. ``b2 ls`` needs no feed key and reads nothing else."""
    if not BUCKET:
        raise Listing("no bucket: set RSS_AUDIO_EPISODES_BUCKET or RSS_AUDIO_B2_BUCKET")
    prefix = f"b2://{BUCKET}/{feed}/episodes/"
    try:
        result = subprocess.run([B2_BIN, "ls", prefix], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, check=False, timeout=TIMEOUT)
    except FileNotFoundError:
        raise Listing(f"{B2_BIN} is not installed") from None
    except subprocess.TimeoutExpired:
        raise Listing(f"the listing did not answer in {TIMEOUT}s") from None
    if result.returncode:
        reason = oneline(result.stderr or result.stdout, 160)
        raise Listing(reason or f"b2 ls exited {result.returncode}")
    return result.stdout.splitlines()


def oneline(text: Any, limit: int) -> str:
    text = " ".join(redact(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def collect(feed: str, lister: Callable[[str], list[str]] | None = None) -> dict[str, Any]:
    lister = lister or list_objects
    prefix = f"{feed}/episodes/"
    episodes = group(bare(name, feed) for name in lister(feed))
    return {"schema": SCHEMA, "feed": feed, "prefix": prefix, "date": None,
            "total": len(episodes), "count": len(episodes), "episodes": episodes}


def kinds_text(episode: dict[str, Any]) -> str:
    """The artefacts present, space separated (one long comma list is one token, and ``redact`` cuts every
    token of 28+ characters that is not a slug), with ``@n`` on a kind whose newest name is version n > 1."""
    return " ".join(kind if episode["versions"].get(kind, 1) <= 1 else f"{kind}@{episode['versions'][kind]}"
                    for kind in episode["kinds"]) or "-"


def line(episode: dict[str, Any]) -> str:
    text = (f"{episode['date'] or '?':<10}  {'condensed' if episode['condensed'] else 'full':<9}  "
            f"{episode['clocks']:<15}  {episode['name']}  | {kinds_text(episode)}")
    if episode["parent"]:
        text += f"  of {episode['parent']}"
    if episode["other"]:
        text += "  other " + " ".join(episode["other"])
    return redact(text)


def render(data: dict[str, Any], out) -> None:
    header = (f"rss-audio episodes: {data['feed']} — {data['count']} of {data['total']} under {data['prefix']}"
              + (f" on {data['date']}" if data["date"] else "") + " (newest first)")
    print(redact(header), file=out)
    for episode in data["episodes"]:
        print(line(episode), file=out)


def main(argv: list[str] | None = None, lister: Callable[[str], list[str]] | None = None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        description="The episodes stored for one rss-audio feed and the artefacts each holds (read only).")
    parser.add_argument("feed", help="the feed's prefix in the bucket (surveillance, audio, ...)")
    parser.add_argument("--date", help="only this day, YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"episodes to list (default {DEFAULT_LIMIT})")
    parser.add_argument("--json", action="store_true", help="the same data as one JSON object")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.date and not DAY.match(args.date):
        parser.error("--date needs YYYY-MM-DD")
    if not FEED.match(args.feed):
        print(f"unknown: {oneline(args.feed, 40)} is not a feed prefix", file=out)
        return 2
    try:
        data = collect(args.feed, lister)
    except Listing as exc:
        print(f"unknown: {exc}", file=out)
        return 1
    if args.date:
        data["date"] = args.date
        data["episodes"] = [episode for episode in data["episodes"] if episode["date"] == args.date]
    if not data["episodes"]:
        print(redact(f"unknown: nothing stored under {data['prefix']}"
                     + (f" on {args.date}" if args.date else "")), file=out)
        return 2
    shown = data["episodes"][:args.limit]
    data["total"], data["count"], data["episodes"] = len(data["episodes"]), len(shown), shown
    if args.json:
        print(json.dumps(clean(data), ensure_ascii=False, indent=1), file=out)
    else:
        render(data, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
