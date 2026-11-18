"""A show's own metadata — its chapter list, its guest list — read from where its source says it lives.

The pilot's fetch step carried the YouTube playlist id and the Omny playlist/collection GUIDs as
constants, so "which show is this" had two definitions: the schedule's and the code's (SV-condensed
D14). Here the source's own entry in ``config/scheduled-sources.json`` says where its metadata comes
from::

    {"showMetadata": {"chapters": true}}                                  # this source's playlist
    {"showMetadata": {"guestList": {"from": "omny", "rssUrl": "https://…"}}}

and the show is identified the way the schedule identifies it, by the date its own title carries.

The two parsers take bytes, so the tests read a stored entry and no network is involved: the chapter
list is a YouTube playlist entry's description, the guest list the numbered list in the radio item that
names the day. A source with no ``showMetadata`` has no metadata; a read that fails raises, and the
caller records it — an unread chapter list is unknown, never an empty one that looks like "no chapters".
"""
from __future__ import annotations

import html
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

UA = "rss-audio-bot/1.0"
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")
ATOM = "http://www.w3.org/2005/Atom"
NS = {"a": ATOM, "m": "http://search.yahoo.com/mrss/"}
_DATE_TEXT = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")


def sources(inventory: Path | None = None) -> list[dict]:
    """The scheduled sources, as the schedule reads them (one definition of the inventory).

    Imported here, not at the top: ``scheduled_sources`` imports ``surveillance``, which reaches this module
    through ``surveillance_condensed``, so the inventory's own module cannot be imported first.
    """
    from scheduled_sources import DEFAULT_INVENTORY, load_inventory

    return load_inventory(inventory or DEFAULT_INVENTORY)


def source(source_id: str, inventory: Path | None = None) -> dict:
    for candidate in sources(inventory):
        if candidate["id"] == source_id:
            return candidate
    raise KeyError(f"no scheduled source {source_id!r}")


def shown_date(title: str) -> str:
    """The date a show's own title carries, spelled as the show spells it (``11/5/2026``)."""
    match = _DATE_TEXT.search(title or "")
    if match is None:
        raise ValueError(f"no show date in title {title!r}")
    return match.group(0)


def fetch(url: str, headers: dict | None = None, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def show_metadata(source_: dict, title: str, *, get=None, playlist=None) -> dict:
    """``{"chapters", "guest_list", "guest_item", "video_id"}`` for the show whose title this is.

    ``get`` and ``playlist`` are the two readers (an Omny/RSS fetch and the playlist Atom fetch); a
    test passes its own. A source whose ``showMetadata`` names neither returns ``{}``.
    """
    shown = shown_date(title)
    declared = source_.get("showMetadata") or {}
    chapters, video = [], None
    if declared.get("chapters"):
        if not source_.get("playlistId"):
            raise ValueError(f"{source_['id']}: chapters asked for, but the source names no playlistId")
        from scheduled_sources import fetch_playlist_feed   # the reader, imported with the branch that reads
        chapters, video = chapter_list((playlist or fetch_playlist_feed)(source_["playlistId"]), source_, shown)
    guests, item = [], None
    if declared.get("guestList"):
        url = str((declared["guestList"] or {}).get("rssUrl") or "")
        if not url.startswith("https://"):
            raise ValueError(f"{source_['id']}: guestList.rssUrl must be an https URL, not {url!r}")
        body = (get or fetch)(url, {"Range": "bytes=0-1000000"}).decode("utf-8", "replace")
        guests, item = guest_list(body, shown)
    if not chapters and not guests:
        return {}
    return {"chapters": chapters, "guest_list": guests, "guest_item": item, "video_id": video}


def chapter_list(body: bytes, source_: dict, shown: str) -> tuple[list[dict], str | None]:
    """The chapter list of the playlist entry whose title carries this show's date, and its video id."""
    root = ET.fromstring(body)
    pattern = re.compile(source_["titleRegex"])
    for entry in root.iter(f"{{{ATOM}}}entry"):
        title = entry.findtext("a:title", default="", namespaces=NS)
        if not pattern.fullmatch(title):
            continue
        try:
            if shown_date(title) != shown:
                continue
        except ValueError:
            continue
        description = entry.findtext("m:group/m:description", default="", namespaces=NS)
        return chapters_from_description(description), entry.findtext("a:id", default="", namespaces=NS)
    return [], None


def chapters_from_description(description: str) -> list[dict]:
    """``0:00 Headline`` lines of a show's own description, in its own clock."""
    chapters = []
    for line in description.splitlines():
        match = re.match(r"^(\d+:\d+(?::\d+)?)\s+(.+)$", line.strip())
        if not match:
            continue
        parts = [int(x) for x in match.group(1).split(":")]
        seconds = parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]
        chapters.append({"t": seconds, "clock": match.group(1), "title": match.group(2).strip()})
    return chapters


def _text(chunk: str, tag: str) -> str:
    match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", chunk, re.S)
    if not match:
        return ""
    body = match.group(1).strip()
    body = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return html.unescape(body)


def guest_list(body: str, shown: str) -> tuple[list[str], str | None]:
    """The numbered guest list of the item whose body names this day, and that item's title."""
    month, day, year = (int(part) for part in shown.split("/"))
    day_word = f"{MONTHS[month - 1]} {day}"
    for chunk in re.findall(r"<item>(.*?)</item>", body, re.S):
        text = _text(chunk, "description")
        if day_word not in text or str(year) not in text:
            continue
        names = []
        flat = " ".join(text.replace("\u00a0", " ").split())
        for match in re.finditer(r"(?:^|\s)\d\)\s*(.+?)(?=\s\d\)\s|$)", flat):
            line = match.group(1).strip()
            if line:
                names.append(line[:260])
        return names, _text(chunk, "title")[:120]
    return [], None


