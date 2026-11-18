"""The condensed Surveillance edition: the interviews-only item of a show that is already published (X-104).

``surveillance.run`` publishes the full show, then submits this job (``inputKind: "condensed"``). It

1. reads the parent's stored script, timed transcript and mp3, and the show's own metadata (its chapter
   list and guest list, from where the scheduled source says they live);
2. runs the structure pass — one model call and at most one repair, the tier from the enrichment file;
3. names the guests the way the show's own metadata spells them;
4. cuts the audio to the interviews and panels, with the cut's one interval map and no model call;
5. uploads six artefacts under the condensed prefix;
6. creates or updates the condensed episode row, sorted just above the parent;
7. puts the seek list on the parent's description;
8. submits the section notes job for the condensed item (one call per block, then one ranking call).

The six artefacts are the cut's and the structure's: the mp3, its script and timed transcript, the chapter
list the feed's items carry, the plan (the record of what was cut and why) and the structure document
itself. The notes job adds the notes, the page and the manifest.

**The edition is versioned, and the version is the parent's.** A show at v1 has its condensed edition at v1
(``<parent>-condensed.mp3``); a rebuild that writes the parent at v2 gets ``<parent>-condensed.v2.*``,
leaving v1's bytes alone (D9: uploads are immutable, a document set is never overwritten). The job's
idempotency key is the parent and that version, and a re-run of it re-uses the stored structure and plan: no
second structure call, no second row, no second object (the immutable upload refuses a different body).

The parent keeps its own description and gains the seek list under it; its own summary is never parsed back
out of that description (D17) — everything we write is a field we hold. Its notes job is off unless the
source's ``parentNotes`` switch says otherwise (``config/scheduled-sources.json``).

**Two switches in the source's own entry, one title, and two operator verbs.** ``condensedTitle`` is this
item's title, with ``{day}`` filled from the day the show was published for (``Surveillance: TV, Nov 6``);
a source without it keeps ``<parent title> — interviews``. ``parentListed`` false publishes the full show's
row with ``block: "yes"``, so the feed lists only this edition while the parent stays uploaded and stored
for the cut to read; this item is always listed. ``--relist --parent SLUG --source ID`` applies those rules
to a show that is already published (title and block only: no model call, no cut, no upload, ``--dry`` to
print without writing), and ``--submit --parent SLUG --source ID`` submits the condensed job for a
published parent again through the worker's own payload (a backlog show back onto the real worker path).

Chain: ``tests/test_chain_surveillance.py``. Dry run, writing only under a work dir and publishing nothing::

    surveillance_condensed.py --parent <parent slug> --source surveillance-tv --work /tmp/x
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from tg_tools_common.clocks import clock, itunes

import notes_job
import surveillance_cut as cut
import surveillance_metadata as show_metadata
import surveillance_structure as structure
from config import Settings
from episode_enrichment.render import render_item_description, with_seek
from episode_enrichment.naming import document_version, names_from_prefix
from episode_enrichment.paragraphs import build_stream
from pipeline import (PipelineError, PublishResult, _api_json, _get_episode_by_slug, _run, _upload_artifact,
                      build_public_object_url, feed_spec, notes_input)

logger = logging.getLogger(__name__)

INPUT_KIND = "condensed"
SUFFIX = "-condensed"
BELOW_PARENT = 5              # the controller's priority: a publish is 20, its condensed job 15, its notes 10
STRUCTURE_SCHEMA = "tg-tools/condensed-structure/1"
ABOVE_PARENT_S = 60           # the item sorts above its parent in the feed
KIND = {"youtube_playlist": "tv", "youtube_live_window": "radio"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_SHOW_DATE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")   # how a show's own title spells its date
INTERVIEWS = " — interviews"                        # the item's title when its source names no condensedTitle


def condensed_slug(parent_slug: str) -> str:
    return f"{parent_slug}{SUFFIX}"


@dataclass(frozen=True)
class Names:
    """One edition's object names.

    The documents share the stem ``names_from_prefix`` gives the notes job (``<prefix>``, or
    ``<prefix>.v<n>`` from v2), so the notes job finds these artefacts by the version it is told. The audio is
    versioned too: a new structure cuts different audio, so it may not overwrite the old file.
    """
    prefix: str
    version: int
    stem: str

    @classmethod
    def of(cls, parent_slug: str, version: int) -> "Names":
        prefix = condensed_slug(parent_slug)
        return cls(prefix=prefix, version=version, stem=prefix if version <= 1 else f"{prefix}.v{version}")

    @property
    def mp3(self) -> str:
        return f"{self.stem}.mp3"

    @property
    def script(self) -> str:
        return f"{self.stem}.script.md"

    @property
    def segments(self) -> str:
        return f"{self.stem}.segments.json"

    @property
    def chapters(self) -> str:
        return f"{self.stem}.chapters.json"

    @property
    def plan(self) -> str:
        return f"{self.stem}.plan.json"

    @property
    def structure(self) -> str:
        return f"{self.stem}.structure.json"

    @property
    def notes(self) -> str:
        return f"{self.stem}.notes.md"

    @property
    def page(self) -> str:
        return f"{self.stem}.page.html"

    @property
    def manifest(self) -> str:
        return f"{self.stem}.manifest.json"


def parent_version(parent: Mapping[str, Any]) -> int:
    """The edition's version, from the parent's own document set: a rebuilt parent (v2) gets a v2 edition."""
    return document_version(str(parent["slug"]), parent.get("speech_text_url") or parent.get("md_url")) or 1


def above_parent(parent: Mapping[str, Any]) -> str | None:
    """The item's feed ordering time: the parent's own, plus a minute, so the item sits above it."""
    stamp = str(parent.get("pub_date") or "")
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment + timedelta(seconds=ABOVE_PARENT_S)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- the show's date, its title and its listing

def spelled_date(title: str) -> date | None:
    """The ``M/D/YYYY`` a show's own title carries, as a date; None when it carries none."""
    found = _SHOW_DATE.search(title or "")
    if found is None:
        return None
    month, day, year = (int(part) for part in found.group(0).split("/"))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def show_date(job: Mapping[str, Any], parent: Mapping[str, Any] | None = None) -> date | None:
    """The day the show was published for: the job's own ``showDate``, else the date its parent title spells.

    The schedule fills the source's ``title`` template with the show's own date and carries that same date as
    ``showDate`` (``scheduled_sources``); a show published before that reached its job, or one an operator
    names by hand, has only the ``M/D/YYYY`` in its parent's title.
    """
    declared = str(job.get("showDate") or "")
    if declared:
        try:
            return date.fromisoformat(declared)
        except ValueError:
            logger.warning("condensed: showDate %r is not an ISO date; reading the parent's title", declared)
    return spelled_date(str((parent or {}).get("title") or job.get("title") or ""))


def day_label(day: date) -> str:
    """The date as the source's template asks for it: ``Nov 6`` (English month, day, no leading zero, no year)."""
    return f"{MONTHS[day.month - 1]} {day.day}"


def condensed_title(source: Mapping[str, Any] | None, parent_title: str, day: date | None) -> str:
    """The item's title: its source's ``condensedTitle`` with ``{day}`` filled, else its parent's + " — interviews".

    A source that names no template, or a caller with no show date to fill it, keeps today's title.
    """
    template = str((source or {}).get("condensedTitle") or "")
    if template and day is not None:
        return template.replace("{day}", day_label(day))
    return f"{parent_title}{INTERVIEWS}"


def parent_block(source: Mapping[str, Any] | None) -> str:
    """The full show's row value for ``block``: ``"yes"`` when its source says ``"parentListed": false``.

    The one listing switch: a hidden row is published and stored all the same (this job reads its script, timed
    transcript and mp3), the feed simply never lists it (rss-edge lists ``block != 'yes'``).
    """
    return "no" if (source or {}).get("parentListed") is not False else "yes"


def source_entry(source_id: str | None) -> dict:
    """The source's own entry in ``config/scheduled-sources.json``, or {} when none is named or found."""
    if not source_id:
        return {}
    try:
        return show_metadata.source(source_id)
    except (KeyError, ValueError) as exc:
        logger.warning("condensed: the scheduled source %r is not in the inventory: %s", source_id, exc)
        return {}


def duration_seconds(*sources: Mapping[str, Any] | None) -> float | None:
    """The parent's length: the job input's seconds, else the row's own ``HH:MM:SS`` field."""
    for source in sources:
        value = (source or {}).get("durationSeconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    for source in sources:
        text = str((source or {}).get("duration") or "")
        if text.count(":") == 2 and all(part.isdigit() for part in text.split(":")):
            hours, minutes, seconds = (int(part) for part in text.split(":"))
            return float(hours * 3600 + minutes * 60 + seconds)
    return None


# ---------------------------------------------------------------- the seek list, the chapters, the description

def clip(text: str, limit: int = 120) -> str:
    """A seek line's topic, cut at a word so the line stays one line."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:·") + "…"


def topics_of(blocks: list[dict], heads: list[int], index: Mapping[str, int], lo: int, hi: int) -> list[str]:
    """The topics of the kept blocks this interval holds.

    ``heads`` are the blocks' own start indices, not the interval's ``lo``: a trimmed head starts later than
    its block, so the block start is the reliable key.
    """
    want = set(heads or [])
    out = []
    for block in blocks:
        if block.get("type") not in structure.KEPT_TYPES:
            continue
        at = index.get(str(block.get("startParagraphId")), -1)
        if (at in want) if want else (lo <= at <= hi):
            out.append(str(block.get("topic") or block.get("title") or ""))
    return [topic for topic in dict.fromkeys(out) if topic]


def seek_lines(intervals: list[dict], *, blocks: list[dict] | None = None,
               paragraphs: list[dict] | None = None, parent: bool = False) -> list[str]:
    """Who speaks when: one line per interval, ``clock guests — topic``.

    The clock is the interval's start in the file it belongs to: inside the condensed file by default, or
    (``parent=True``) in the full show's own file, which is what the parent's description shows.
    """
    index = {str(p["id"]): i for i, p in enumerate(paragraphs or [])}
    out, at = [], 0.0
    for interval in intervals:
        who = ", ".join(dict.fromkeys(str(g) for g in interval.get("guests") or [] if str(g or "").strip()))
        topic = clip(" · ".join(dict.fromkeys(topics_of(
            list(blocks or []), list(interval.get("heads") or []), index,
            int(interval.get("lo") or 0), int(interval.get("hi") or 0)))))
        start = float(interval.get("audio_start") or 0.0) if parent else at
        out.append(f"{clock(start)} {who or 'segment'} — {topic}" if topic else f"{clock(start)} {who or 'segment'}")
        at += float(interval.get("seconds") or 0.0)
    return out


def chapters(intervals: list[dict], lines: list[str]) -> dict:
    """The item's chapter list (Podcasting 2.0 chapters/1.2.0), from the same lines the seek list shows."""
    at, out = 0.0, []
    for interval, line in zip(intervals, lines):
        out.append({"startTime": int(round(at)), "title": line.split(" ", 1)[1] if " " in line else line})
        at += float(interval.get("seconds") or 0.0)
    return {"version": "1.2.0", "chapters": out}


def item_head(count: int, short: float | None, total: float | None) -> str:
    """The item's first line: what it is, how much of the show it carries."""
    if short is None:
        return f"<strong>Interviews only</strong> · {count} segments"
    if total:
        return f"<strong>Interviews only</strong> · {count} segments · {clock(short)} of {clock(total)}"
    return f"<strong>Interviews only</strong> · {count} segments · {clock(short)}"


def parent_head(count: int, total: float | None) -> str:
    return (f"<strong>Full show</strong> · {count} interviews · {clock(total)}" if total
            else f"<strong>Full show</strong> · {count} interviews")


def anchor(label: str, url: str | None) -> str:
    return f'<a href="{url}">{label}</a>' if url else label


def parent_description(existing: str, *, seek: list[str], links: list[str]) -> str:
    """The parent's description: its own text, then the seek list (``render.with_seek``, one definition)."""
    return with_seek(existing, seek=seek, links=links)


# ---------------------------------------------------------------- the show, as the structure pass reads it

def paragraphs_of(script: str, timed: Mapping[str, Any]) -> list[dict]:
    """The show's paragraphs with their times, from its stored script and timed transcript."""
    stream = build_stream(script)
    rows = [row for row in (timed or {}).get("paragraphs") or [] if isinstance(row, dict)]
    by_id = {str(row.get("id")): row for row in rows}
    if len(rows) != len(stream.paragraphs) or any(p.id not in by_id for p in stream.paragraphs):
        raise PipelineError("structure", f"the stored timed transcript is not this script's "
                            f"({len(rows)} timed paragraphs, {len(stream.paragraphs)} in the script)",
                            retryable=False)
    return [{"id": p.id, "t": float(by_id[p.id]["start"]), "end": float(by_id[p.id]["end"]), "text": p.text}
            for p in stream.paragraphs]


def structure_input(*, title: str, kind: str, length: float | None, paragraphs: list[dict],
                    chapters: list[dict] = (), guest_list: list[str] = ()) -> dict:
    return {"title": title, "kind": kind, "duration_s": round(length or 0.0), "chapters": list(chapters),
            "guest_list": list(guest_list), "paragraphs": paragraphs}


def show_metadata_for(source_id: str | None, title: str) -> tuple[dict, str | None, str]:
    """(the show's own chapter and guest lists, why they could not be read, its kind).

    Unknown is not empty: a read that fails is recorded and the cut goes on without the anchors.
    """
    if not source_id:
        return {}, "the job names no scheduled source", ""
    try:
        source = show_metadata.source(source_id)
    except (KeyError, ValueError) as exc:
        return {}, f"the scheduled source {source_id!r} is not in the inventory: {exc}", ""
    kind = KIND.get(str(source.get("kind") or ""), "")
    try:
        return show_metadata.show_metadata(source, title), None, kind
    except Exception as exc:                       # a network read: recorded, never fatal to the cut
        return {}, f"{type(exc).__name__}: {exc}", kind


# ---------------------------------------------------------------- the job

def _read(settings: Settings, feed: str, name: str, work: Path) -> bytes | None:
    """One stored object's bytes, or None when the store's listing confirms it is not there."""
    dest = work / "stored" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not notes_job._download(settings, feed, name, dest):
        return None
    return dest.read_bytes()


def _stored_json(settings: Settings, feed: str, name: str, work: Path) -> dict | None:
    body = _read(settings, feed, name, work)
    if body is None:
        return None
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise PipelineError("condensed", f"the stored {name} is not JSON: {exc}", retryable=False) from exc
    return parsed if isinstance(parsed, dict) else None


def _stored_size(settings: Settings, feed: str, name: str) -> int:
    """One stored object's size, for the row of an edition this attempt did not upload again."""
    folder = f"{settings.b2_remote}:{settings.b2_bucket}/{feed_spec(feed)[0]}/episodes/"
    listed = _run([settings.rclone_bin, "lsf", "--files-only", "--format", "sp", folder],
                  stage="condensed", timeout=120).stdout.split()
    for row in listed:
        size, _, path = row.partition(";")
        if path.rsplit("/", 1)[-1] == name and size.isdigit():
            return int(size)
    raise PipelineError("condensed", f"the stored {name} is not in the feed's store", retryable=False)


def _parent_script(settings: Settings, feed: str, parent: Mapping[str, Any], work: Path) -> tuple[str, dict]:
    """(the parent's script, its timed transcript) as this job reads them from the store."""
    names = names_from_prefix(str(parent["slug"]), parent_version(parent))
    script = _read(settings, feed, names.script_name, work)
    if script is None:
        raise PipelineError("condensed", f"the parent's {names.script_name} is not in the feed's store",
                            retryable=False)
    segments = _read(settings, feed, names.segments_name, work)
    if segments is None:
        raise PipelineError("condensed", f"the parent's {names.segments_name} is not in the feed's store, so "
                            f"there is no timed transcript to cut", retryable=False)
    try:
        return script.decode("utf-8"), json.loads(segments)
    except ValueError as exc:
        raise PipelineError("condensed", f"the parent's {names.segments_name} is not JSON: {exc}",
                            retryable=False) from exc


def _parent_audio(settings: Settings, feed: str, parent: Mapping[str, Any], work: Path) -> Path:
    """The parent's mp3 on disk, the cut's source. Read once per attempt; the parent's object is never written."""
    name = names_from_prefix(str(parent["slug"]), parent_version(parent)).audio_name
    dest = work / "source" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest.is_file() and dest.stat().st_size > 0):
        if not notes_job._download(settings, feed, name, dest):
            raise PipelineError("condensed", f"the parent's {name} is not in the feed's store", retryable=False)
    return dest


def _write(settings: Settings, feed: str, name: str, body: bytes, work: Path, *, dry: bool) -> None:
    """One artefact: written under the work dir, and uploaded to its exact key unless this is a dry run."""
    path = work / "artefacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if dry:
        logger.info("dry run: %s (%d bytes) stays in %s", name, len(body), path.parent)
        return
    _upload_artifact(settings, path, f"{feed_spec(feed)[0]}/episodes/{name}")


def _json(body: Any) -> bytes:
    return (json.dumps(body, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


@dataclass(frozen=True)
class Edition:
    """One attempt's outcome: the names, the plan and structure, the row, the parent's text, the notes job."""
    names: Names
    plan: dict
    structure: dict
    row: dict
    parent_description: str
    notes: dict
    report: dict


def edition(settings: Settings, job: Mapping[str, Any], work: Path, tier: str | None, *,
            dry: bool = False) -> Edition:
    """Structure, names, cut and the row's payload for one parent — everything before the feed writes."""
    feed = str(job.get("feed") or "surveillance")
    feed_spec(feed)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    parent_slug = str(job.get("parentSlug") or "").strip()
    if not parent_slug:
        raise PipelineError("input", "a condensed job needs parentSlug", retryable=False)
    parent = _get_episode_by_slug(settings, parent_slug, feed)
    if parent is None:
        raise PipelineError("input", f"no episode {parent_slug} in feed {feed}", retryable=False)
    version = parent_version(parent)
    if job.get("parentVersion") not in (None, version):
        raise PipelineError("input", f"{parent_slug} is at v{version}, the job says v{job['parentVersion']}",
                            retryable=False)
    names = Names.of(parent_slug, version)
    started = time.monotonic()
    source = source_entry(str(job.get("sourceId") or ""))
    title = str(parent.get("title") or parent_slug)          # the show's own title: the structure input
    item_title = condensed_title(source, title, show_date(job, parent))
    report: dict = {"slug": names.prefix, "version": version, "parent": parent_slug, "tier": tier}

    parent_script, parent_timed = _parent_script(settings, feed, parent, work)
    paragraphs = paragraphs_of(parent_script, parent_timed)
    stored_structure = _stored_json(settings, feed, names.structure, work)
    stored_plan = _stored_json(settings, feed, names.plan, work)

    if stored_structure and stored_plan:
        structure_doc, plan = stored_structure, stored_plan
        report["reused"] = [names.structure, names.plan]
        audio_bytes = _stored_size(settings, feed, names.mp3)
        script = (_read(settings, feed, names.script, work) or b"").decode("utf-8")
        if not script:
            raise PipelineError("condensed", f"the stored {names.structure} and {names.plan} are there but "
                                f"{names.script} is not", retryable=False)
        logger.info("condensed %s v%d: the stored structure and plan are reused", names.prefix, version)
    else:
        meta, meta_error, kind = show_metadata_for(str(job.get("sourceId") or ""), title)
        inp = structure_input(title=title, kind=kind or "tv", length=duration_seconds(job, parent),
                              paragraphs=paragraphs, chapters=meta.get("chapters") or [],
                              guest_list=meta.get("guest_list") or [])
        if stored_structure:
            structure_doc = stored_structure
            report["reused"] = [names.structure]
        else:
            structure_doc, structure_report = structure.structure(inp, work_dir=work / "structure", tier=tier)
            structure_report["nameRepairs"] = structure.canonicalise(inp, structure_doc)
            structure_report["metadataError"] = meta_error
            structure_doc = {"schema": STRUCTURE_SCHEMA, "blocks": structure_doc["blocks"],
                             "report": structure_report}
            _write(settings, feed, names.structure, _json(structure_doc), work, dry=dry)
            report["structure"] = structure_report
        audio = _parent_audio(settings, feed, parent, work / "cut")
        measured = cut.cut(audio, inp, structure_doc, work / "cut", structure_name=names.structure,
                           parent=parent_timed)
        plan = measured["plan"]
        cut.publishable(plan)                     # a plan-only plan never reaches the feed (D15)
        audio_bytes = int(measured["audio"].stat().st_size)
        script = measured["script"]
        lines = seek_lines(plan["intervals"], blocks=structure_doc["blocks"], paragraphs=paragraphs)
        for name, body in ((names.segments, measured["transcript"]),
                           (names.chapters, chapters(plan["intervals"], lines)),
                           (names.script, script.encode("utf-8")),
                           (names.mp3, measured["audio"].read_bytes())):
            _write(settings, feed, name, body if isinstance(body, bytes) else _json(body), work, dry=dry)
        _write(settings, feed, names.plan, _json(plan), work, dry=dry)
        report["cut"] = {"intervals": len(plan["intervals"]), "plannedSeconds": plan.get("plannedSeconds"),
                         "measuredSeconds": plan.get("measuredSeconds"), "audioBytes": audio_bytes}

    blocks = list(structure_doc.get("blocks") or [])
    lines = seek_lines(plan["intervals"], blocks=blocks, paragraphs=paragraphs)
    total = duration_seconds(job, parent)
    head_line = item_head(len(plan["intervals"]), plan.get("measuredSeconds"), total)
    # the full show is the parent's page when it has one (its own notes job writes that); else its audio
    full = (f"Full show: {anchor('page', parent['page_url'])}" if parent.get("page_url")
            else f"Full show: {anchor('MP3', parent.get('mp3_url'))}")
    item_links = [full,
                  f"Transcript: {anchor('script', build_public_object_url(settings, feed, names.script))}",
                  f"Notes: {anchor('notes', build_public_object_url(settings, feed, names.notes))}"]
    row = {
        "feed": feed, "slug": names.prefix, "title": item_title,
        "pub_date": above_parent(parent),
        "description": render_item_description(head=head_line, seek=lines, links=item_links),
        "mp3_url": build_public_object_url(settings, feed, names.mp3), "mp3_size": audio_bytes,
        "duration": itunes(float(plan.get("measuredSeconds") or 0.0)),
        "explicit": str(parent.get("explicit") or "no"), "keywords": "auto,rss-audio-bot",
        # the edition is always listed, whatever the full show's own listing says
        "block": "no",
        "speech_text_url": build_public_object_url(settings, feed, names.script),
        # no md_url, page_url or manifest_url here: the notes job writes those documents, and switches the row
        # to them in one PUT when they exist (notes_job._switch).
    }
    parent_links = [anchor(f"Interviews only ({clock(float(plan.get('measuredSeconds') or 0.0))})",
                           build_public_object_url(settings, feed, names.page))]
    seek = seek_lines(plan["intervals"], blocks=blocks, paragraphs=paragraphs, parent=True)
    report["seconds"] = round(time.monotonic() - started)
    notes = notes_input(feed=feed, slug=names.prefix, version=names.version,
                        script_sha256=hashlib.sha256(script.encode("utf-8")).hexdigest(), title=row["title"],
                        published_at=str(parent.get("pub_date") or "") or None,
                        date_basis=str(job.get("dateBasis") or "unknown"),
                        duration_seconds=plan.get("measuredSeconds"), source_url=job.get("originalUrl"),
                        stt_engine=job.get("sttEngine"))
    notes.update(notesKind="sections", blocks=blocks, intervals=list(plan.get("intervals") or []),
                 head=head_line, seek=lines, links=item_links)
    return Edition(names=names, plan=plan, structure=structure_doc, row=row, notes=notes, report=report,
                   parent_description=parent_description(str(parent.get("description") or ""), seek=seek,
                                                         links=parent_links))


def run(settings: Settings, job: Mapping[str, Any], work_dir: Path, tier: str | None,
        *, dry: bool = False) -> PublishResult:
    """The job: the edition, then the row, the parent's seek list, and the notes job it hands on."""
    work = Path(work_dir)
    try:
        built = edition(settings, job, work, tier, dry=dry)
    except (structure.StructureError, cut.CutError, cut.PlanError) as exc:
        # What is retryable is what the exception says: a call that never answered (a 429, a timeout, a
        # reply that is not JSON) gets another attempt, while a document that is still wrong after its
        # repair, a plan-only plan and a cut that misses its plan are the same on every attempt and end the
        # job (D15, D16).
        raise PipelineError(str(getattr(exc, "error_class", "condensed") or "condensed"), str(exc),
                            retryable=bool(getattr(exc, "retryable", False))) from exc
    names, row, feed = built.names, dict(built.row), str(built.row["feed"])
    existing = _get_episode_by_slug(settings, names.prefix, feed)
    if dry:
        (work / f"{names.prefix}.episode.json").write_text(json.dumps(row, indent=1, ensure_ascii=False) + "\n",
                                                           encoding="utf-8")
        (work / "parent-description.html").write_text(built.parent_description + "\n", encoding="utf-8")
        (work / "condensed.report.json").write_text(json.dumps(built.report, indent=1, ensure_ascii=False) + "\n",
                                                    encoding="utf-8")
        logger.info("dry run: %s would be %s at v%d (nothing uploaded, no feed write, no notes job)",
                    names.prefix, "updated" if existing else "created", names.version)
    elif existing:
        row = _api_json(settings, "PUT", f"/api/episodes/{existing['id']}",
                        {key: value for key, value in row.items() if key not in ("feed", "slug")})
        logger.info("condensed %s: episode %s updated at v%d", names.prefix, existing.get("id"), names.version)
    else:
        row = _api_json(settings, "POST", "/api/episodes", row)
        logger.info("condensed %s: episode %s created at v%d", names.prefix, row.get("id"), names.version)
    parent = _get_episode_by_slug(settings, str(job.get("parentSlug")), feed)
    if parent is not None and parent.get("description") != built.parent_description:
        if dry:
            logger.info("dry run: parent %s would gain its seek list", parent.get("id"))
        else:
            _api_json(settings, "PUT", f"/api/episodes/{parent['id']}",
                      {"description": built.parent_description})
            logger.info("parent %s: its description carries the seek list now", parent.get("id"))
    return PublishResult(slug=names.prefix, title=str(row.get("title") or names.prefix),
                         public_url=str(row.get("mp3_url") or ""), file_size_bytes=int(row.get("mp3_size") or 0),
                         script_url=str(row.get("speech_text_url") or ""),
                         segments_url=build_public_object_url(settings, feed, names.segments),
                         notes=None if dry else built.notes)


def parent_job_input(published: Any, input_json: Mapping[str, Any]) -> dict[str, Any] | None:
    """The condensed job a just-published show hands on, or None for a row that is itself an edition.

    The provenance the notes job would have carried (its date basis, the original's link, the STT engine)
    comes from the parent's own notes input, which the publish path built before the switch dropped it.
    """
    slug = str(getattr(published, "slug", "") or "")
    if not slug or slug.endswith(SUFFIX):
        return None
    provenance = getattr(published, "notes", None) or {}
    source_key = str(input_json.get("sourceKey") or "")
    return {"kind": "rss", "inputKind": INPUT_KIND, "feed": str(input_json.get("feed") or "surveillance"),
            "parentSlug": slug, "parentVersion": document_version(slug, getattr(published, "script_url", None)) or 1,
            "sourceId": source_key.split(":", 1)[0] if source_key else "",
            "title": str(getattr(published, "title", "") or ""),
            "publishedAt": str(input_json.get("publishedAt") or "") or None,
            "dateBasis": str(provenance.get("dateBasis") or "unknown"),
            "durationSeconds": provenance.get("durationSeconds"),
            "originalUrl": provenance.get("originalUrl"), "sttEngine": provenance.get("sttEngine"),
            "delivery": "none"}


def condensed_job_payload(parent: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
    """The controller submit payload. Idempotent on the parent slug and the edition's version."""
    from tg_tools_common.intent_contract import RuntimeJobIntent
    key = f"rss-audio:condensed:{job['parentSlug']}:v{int(job.get('parentVersion') or 1)}"
    return RuntimeJobIntent(
        producer_system="rss-audio", producer_ref=f"{parent.get('producerRef') or job['parentSlug']}:condensed",
        result_owner="rss-audio", result_route=str(parent.get("resultRoute") or "rss-audio.scheduled.publish.v1"),
        workload_type="rss_publish", queue_class="foreground", execution_profile="cpu_media",
        execution_shape="local_bounded", priority=max(int(parent.get("priority") or 0) - BELOW_PARENT, 0),
        input_json=dict(job),
        output_contract_json={"version": 1, "result": "the condensed edition of a published show, no delivery"},
        idempotency_key=key, effect_key=key.replace("rss-audio:condensed:", "rss-audio:feed-condensed:", 1),
        max_attempts=3).to_submit_payload()


def existing_job(parent: Mapping[str, Any], source_id: str, feed: str = "surveillance") -> dict[str, Any]:
    """The condensed job input of a show that is already published, built from its row.

    The row is what the feed holds, so it carries no structured air date: the edition's title falls back to the
    date the parent's own title spells (``show_date``).
    """
    return {"kind": "rss", "inputKind": INPUT_KIND, "feed": feed, "parentSlug": str(parent["slug"]),
            "parentVersion": parent_version(parent), "sourceId": source_id,
            "title": str(parent.get("title") or ""),
            "publishedAt": str(parent.get("pub_date") or "") or None,
            "dateBasis": "scheduled_source", "delivery": "none"}


def submit(settings: Settings, parent_slug: str, source_id: str, *, client=None) -> str:
    """Submit an already-published show's condensed job through the worker's own payload and client.

    The idempotency key is the parent and its version, so a job the publish already handed on is answered with
    that job, not a second one. The job id is printed; "" when the show is not published (the CLI exits 3).
    """
    from runtime_client import RuntimeClient
    parent = _get_episode_by_slug(settings, parent_slug, "surveillance")
    if parent is None:
        print(f"{parent_slug}: no episode in feed surveillance", file=sys.stderr)
        return ""
    submitted = (client or RuntimeClient(settings)).submit_job(
        condensed_job_payload(parent, existing_job(parent, source_id)))
    job_id = str(((submitted or {}).get("job") or {}).get("jobId") or "")
    if not job_id:
        print(f"{parent_slug}: the controller returned no job id", file=sys.stderr)
        return ""
    print(job_id)
    return job_id


def relist(settings: Settings, parent_slug: str, source_id: str, *, dry: bool = False) -> int:
    """Apply the current title and listing rules to a show that is already published.

    Only the two rows' ``title`` and ``block`` move, through the same update call the parent's description
    takes: no model call, no cut, no upload, and the artefacts, the notes and the seek list stay as they are.
    Exit 3 when the show has no condensed edition row (nothing is written).
    """
    feed = "surveillance"
    parent = _get_episode_by_slug(settings, parent_slug, feed)
    if parent is None:
        print(f"{parent_slug}: no episode in feed {feed}", file=sys.stderr)
        return 3
    item = _get_episode_by_slug(settings, condensed_slug(parent_slug), feed)
    if item is None:
        print(f"{condensed_slug(parent_slug)}: no condensed edition row, so nothing is relisted", file=sys.stderr)
        return 3
    source = source_entry(source_id)
    parent_title = str(parent.get("title") or parent_slug)
    wanted = {"title": condensed_title(source, parent_title, show_date({}, parent)), "block": "no"}
    parent_wanted = {"block": parent_block(source)}
    print(f"parent {parent_slug}: title {parent_title!r} (unchanged), "
          f"block {str(parent.get('block') or 'no')!r} -> {parent_wanted['block']!r}")
    print(f"condensed {condensed_slug(parent_slug)}: title {str(item.get('title') or '')!r} -> {wanted['title']!r}, "
          f"block {str(item.get('block') or 'no')!r} -> {wanted['block']!r}")
    if dry:
        print("dry run: nothing written")
        return 0
    if any(str(item.get(key) or "") != value for key, value in wanted.items()):
        _api_json(settings, "PUT", f"/api/episodes/{item['id']}", wanted)
        logger.info("condensed %s: its title and listing applied", condensed_slug(parent_slug))
    if str(parent.get("block") or "no") != parent_wanted["block"]:
        _api_json(settings, "PUT", f"/api/episodes/{parent['id']}", parent_wanted)
        logger.info("parent %s: block %s", parent_slug, parent_wanted["block"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The condensed Surveillance edition, published or (default) not.")
    parser.add_argument("--parent", required=True, help="the published full show's slug")
    parser.add_argument("--source", default="", help="its scheduled source id, e.g. surveillance-tv")
    parser.add_argument("--work", default="/tmp/tg-condensed", help="where this run writes")
    parser.add_argument("--live", action="store_true", help="upload and publish (default: a dry run)")
    parser.add_argument("--relist", action="store_true",
                        help="apply the current title and listing rules to an existing show: no model call, "
                             "cut or upload")
    parser.add_argument("--submit", action="store_true",
                        help="submit the condensed job for an existing published parent, and print its job id")
    parser.add_argument("--dry", action="store_true", help="with --relist: print the change and write nothing")
    args = parser.parse_args(argv)
    import config
    from pi_harness import enrichment_tier
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # An operator tool, like surveillance_backfill.py: it borrows the runtime unit's own env to reach the store
    # and the API (D8 governs the job, which is handed its settings by the worker).
    settings = config.worker_settings()[0]
    if args.relist or args.submit:
        if not args.source:
            print("--relist and --submit read the source's entry, so they need --source, e.g. surveillance-tv",
                  file=sys.stderr)
            return 2
        if args.relist and args.submit:
            print("--relist and --submit are two different runs; give one of them", file=sys.stderr)
            return 2
        if args.relist:
            return relist(settings, args.parent, args.source, dry=args.dry)
        return 0 if submit(settings, args.parent, args.source) else 3
    job: dict[str, Any] = {"kind": "rss", "inputKind": INPUT_KIND, "feed": "surveillance",
                           "parentSlug": args.parent, "sourceId": args.source, "delivery": "none"}
    row = _get_episode_by_slug(settings, args.parent, "surveillance")
    if row is not None:
        job.update(parentVersion=parent_version(row), title=row.get("title"), dateBasis="scheduled_source")
    published = run(settings, job, Path(args.work), enrichment_tier("surveillance"), dry=not args.live)
    print(json.dumps({"slug": published.slug, "notesJob": published.notes is not None}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
