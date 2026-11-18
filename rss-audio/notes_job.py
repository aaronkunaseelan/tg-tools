"""The notes job: per-episode notes, written after the episode is already live (X-76 step 4).

The publish job puts the mp3, ``script.md`` and ``segments.json`` in the feed and then submits this
job, so nothing waits on notes. The job reads the stored script and segments (never the source again),
writes the notes and ``manifest.json``, and switches the episode over with one partial ``PUT``. When the
episode is not on this version's script yet (a rebuild), the same PUT moves ``speech_text_url``; a version
>= 2 then deletes the older versions' documents (D1: uploads are immutable, nothing is overwritten).

Two notes shapes live behind this job. An episode runs the shared enrichment (one pass, or the segments
an earlier release split it into). A Surveillance show whose input names it (``notesKind: "sections"``)
runs section notes instead: one call per interview block of its structure, one ranking call, and code
writes the names, firms and clocks (``surveillance_notes.py``). That path runs the edition's ten
end-to-end checks before the upload; a failing check is the job's own error, never a silent pass.

A retry after the uploads finishes that attempt: it PUTs the stored notes and manifest (the manifest
records the feed description) without running the model again, since a re-run would not reproduce
immutable bytes. A failure leaves the episode on what it had (its script, or its previous version); the job's error,
naming the segment and stage, is the visible outcome.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import surveillance_notes
from config import Settings
from episode_enrichment import EnrichmentError, PreparedScript, enrich_script
from episode_enrichment import render as render_mod
from episode_enrichment.speakers import speaker_entry, surname
from episode_enrichment import timing as timing_mod
from episode_enrichment.luna import EnrichmentModelError
from episode_enrichment.naming import document_version, names_from_prefix
from episode_enrichment.page import PAGE_LAYOUT, render_page
from episode_enrichment.paragraphs import build_stream
from episode_enrichment.pipeline import NOTES_SHAPE_SECTIONS, default_runner_factory
from pi_harness import EnrichmentConfigError, resolve_role
from pipeline import (PipelineError, PublishResult, _api_json, _get_episode_by_slug, _run, _upload_artifact,
                      build_public_manifest_url, build_public_notes_url, build_public_script_url,
                      build_public_page_url, build_public_segments_url, delete_documents, feed_spec, notes_input)
from tg_tools_common.lease import LeaseLost

logger = logging.getLogger(__name__)

INPUT_KIND = "notes"
PAGE_KIND = "page"   # the page alone, from stored notes and script: no model call
PAGE_PRIORITY = 10


@dataclass(frozen=True)
class StoredScript:
    """The provenance of a published speech-to-text script, read back from B2."""
    kind: str = "audio"
    content_origin: str = "stt_transcript"
    topic_origin: str = "luna_topics"
    summary_origin: str = "luna_summary"


NOTES_BELOW_PARENT = 5


def notes_priority(parent: Mapping[str, Any]) -> int:
    """Just below the publish job that submitted the notes (the controller's range is 0-100)."""
    return max(int(parent.get("priority") or 0) - NOTES_BELOW_PARENT, 0)


def notes_job_payload(parent: Mapping[str, Any], notes: Mapping[str, Any]) -> dict[str, Any]:
    """The controller submit payload. Idempotent on (slug, version): a version's script is immutable, and a
    retry of the parent that reads the episode back (no script sha at hand) resubmits the same key.

    The producer ref extends the parent's, so whoever follows the parent finds it.
    Claims go by priority, then age, so notes run just below their parent's class: a Telegram episode's
    notes (55) wait for Telegram publishes (60), a scheduled show's (15) for every publish (20 and up),
    and a queue of backfill notes never holds a publish for more than the one notes job in flight.
    """
    from tg_tools_common.intent_contract import RuntimeJobIntent
    key = f"rss-audio:notes:{notes['slug']}:v{int(notes.get('version') or 1)}"
    return RuntimeJobIntent(
        producer_system="rss-audio", producer_ref=f"{parent.get('producerRef') or notes['slug']}:notes",
        result_owner="rss-audio", result_route=str(parent.get("resultRoute") or "telegram.rss-audio.confirmation.v1"),
        workload_type="rss_publish", queue_class="foreground", execution_profile="cpu_media",   # the controller's rss_publish policy
        execution_shape="local_bounded", priority=notes_priority(parent), input_json=dict(notes),
        output_contract_json={"version": 1, "result": "episode notes and manifest, no delivery"},
        idempotency_key=key, effect_key=key.replace("rss-audio:notes:", "rss-audio:feed-notes:", 1),
        max_attempts=3).to_submit_payload()


def _download(settings: Settings, feed: str, name: str, dest: Path) -> bool:
    """Copy one stored object to ``dest``. False only when the store's listing confirms it is not there;
    a failed read raises a retryable error, so a network blip never passes for a missing file."""
    folder = f"{settings.b2_remote}:{settings.b2_bucket}/{feed_spec(feed)[0]}/episodes/"
    try:
        listed = _run([settings.rclone_bin, "lsf", "--files-only", "--include", name, folder],
                      stage="notes", timeout=120).stdout.split()
        if name not in listed:
            return False
        _run([settings.rclone_bin, "copyto", folder + name, str(dest)], stage="notes", timeout=600)
    except PipelineError as exc:
        raise PipelineError("notes", f"could not read {name} from the store: {exc}", retryable=True) from exc
    if not Path(dest).is_file():
        raise PipelineError("notes", f"{name} is listed in the store but was not copied", retryable=True)
    return True


def _named_in(speaker: Any, text: str) -> dict | None:
    """The metadata's speaker, kept only when the transcript says their surname.

    A title that is exactly two capitalised words can be a show ("Hard Fork") as easily as a person
    ("Jane Doe"); the transcript decides, and nobody named in the prompt is worse than nobody named at all.
    """
    names, _firms = speaker_entry(speaker)
    if not names:
        return None
    words = text.lower()
    return speaker if any(surname(name).lower() in words for name in names) else None


def _stored_script(settings: Settings, notes: Mapping[str, Any], work: Path) -> PreparedScript:
    feed, slug, version = notes["feed"], notes["slug"], int(notes.get("version") or 1)
    names = names_from_prefix(slug, version)
    stored = work / "stored"   # not the work root: only the documents this job writes are registered from there
    stored.mkdir(parents=True, exist_ok=True)
    script_path, timed_path = stored / names.script_name, stored / names.segments_name
    if not _download(settings, feed, names.script_name, script_path):
        raise PipelineError("notes", f"{names.script_name} is not in the feed's store", retryable=False)
    text = script_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if notes.get("scriptSha256") and digest != notes["scriptSha256"]:
        raise PipelineError("notes", f"{names.script_name} is not the script this job was submitted for",
                            retryable=False)
    timed, summary = None, timing_mod.no_timing("no segments.json was stored")
    if _download(settings, feed, names.segments_name, timed_path):
        stored = timed_path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(stored)
        except ValueError:
            parsed = {}
        if parsed.get("scriptSha256") == digest:
            timed = stored
            summary = {"basis": parsed.get("basis"), "paragraphs": len(parsed.get("paragraphs") or []),
                       "segments": len(parsed.get("segments") or [])}
        else:
            summary = timing_mod.no_timing("the stored segments.json belongs to another script")
    return PreparedScript(text=text, path=script_path, sha256=digest, source=StoredScript(),
                          stt_engine=notes.get("sttEngine"), timed_transcript=timed, timing=summary)


def _finish_earlier(settings: Settings, notes_input_: Mapping[str, Any], names, episode: Mapping[str, Any],
                   urls: Mapping[str, str], version: int, work: Path) -> PublishResult | None:
    """Switch the episode to the notes and manifest an earlier attempt of this job already stored.

    Uploads are immutable, and a re-run would not reproduce those bytes (the model's output differs),
    so a retry after the uploads finishes the earlier attempt: no enrichment, no model calls. Notes
    stored without their manifest (an attempt stopped between its two uploads) are terminal; a
    rebuild recovers the episode. None when nothing is stored yet.
    """
    feed = notes_input_["feed"]
    stored = work / "stored"
    stored.mkdir(parents=True, exist_ok=True)
    notes = _download(settings, feed, names.notes_name, stored / names.notes_name)
    manifest = _download(settings, feed, names.manifest_name, stored / names.manifest_name)
    if not (notes or manifest):
        return None
    if not (notes and manifest):
        raise PipelineError("notes", f"{names.notes_name if notes else names.manifest_name} is stored without "
                            f"its {'manifest' if notes else 'notes'} (an attempt stopped between its uploads); "
                            "rebuild the episode", retryable=False)
    try:
        description = json.loads((stored / names.manifest_name).read_text(encoding="utf-8")).get("feedDescription")
    except ValueError as exc:
        raise PipelineError("notes", f"the stored {names.manifest_name} is not JSON", retryable=False) from exc
    for file_name in (names.notes_name, names.manifest_name):   # registered from the work root, like a fresh run's
        (work / file_name).write_bytes((stored / file_name).read_bytes())
    logger.info("notes for %s v%d are already stored; finishing the earlier attempt", names.prefix, version)
    page = _page_addon(lambda: _upload_page(settings, feed, names, (stored / names.notes_name).read_text(
        encoding="utf-8"), _stored_script(settings, notes_input_, work), work), names.prefix)
    return _switch(settings, feed, names, episode, urls, version, description, page=page)


def _upload_page(settings: Settings, feed: str, names, notes_markdown: str, script: PreparedScript, work: Path) -> None:
    """Render and upload the episode page.

    The page is derived from the notes and script and nothing refers to it by hash, so it is uploaded
    allowing overwrite: a renderer change, or a retry after a release, replaces it.
    """
    page = render_page(notes_markdown=notes_markdown, script_text=script.text, timed=script.timed_transcript)
    (work / names.page_name).write_text(page, encoding="utf-8")
    _overwrite(settings, work / names.page_name, f"{feed_spec(feed)[0]}/episodes/{names.page_name}")


def _overwrite(settings: Settings, local: Path, key: str) -> None:
    _run([settings.rclone_bin, "copyto", str(local), f"{settings.b2_remote}:{settings.b2_bucket}/{key}"],
         stage="upload", timeout=600)


def _page_addon(write: Callable[[], None], slug: str) -> bool:
    """The page is an add-on to the notes: a failure to build or upload it never blocks the notes switch.
    It is logged, the PUT goes without page_url, and a page job fills it later."""
    try:
        write()
        return True
    except LeaseLost:   # not the page's failure: the attempt is over, nothing may PUT
        raise
    except Exception as exc:
        logger.warning("page for %s not written (%s: %s); the notes switch goes ahead without page_url",
                       slug, type(exc).__name__, exc)
        return False


def _put(settings: Settings, episode: Mapping[str, Any], change: Mapping[str, Any], *, require_page: bool) -> None:
    """The partial PUT. An episode API without the page_url column ignores it silently: say so."""
    row = _api_json(settings, "PUT", f"/api/episodes/{episode['id']}", dict(change))
    if "page_url" in change and not (isinstance(row, dict) and row.get("page_url") == change["page_url"]):
        if require_page:
            raise PipelineError("page", "the episode API did not store page_url (no page_url column yet)",
                                retryable=False)
        logger.warning("the episode API did not store page_url for %s; the page is uploaded, not linked",
                       episode.get("slug"))


def _switch(settings: Settings, feed: str, names, episode: Mapping[str, Any], urls: Mapping[str, str],
            version: int, description: str | None, *, page: bool) -> PublishResult:
    """The one PUT that moves the episode to this version's documents, then the older versions' delete."""
    change = {"md_url": urls["notes"], "manifest_url": urls["manifest"]}
    if page:
        change["page_url"] = urls["page"]
    if description is not None:
        change["description"] = render_mod.keep_seek(description, episode.get("description"))
    if episode.get("speech_text_url") != urls["script"]:   # a rebuild switches its script here, with its notes
        change["speech_text_url"] = urls["script"]
    _put(settings, episode, change, require_page=False)
    if version > 1:
        delete_documents(settings, names.prefix, feed, below_version=version)
    size = episode.get("mp3_size") or episode.get("file_size_bytes") or 0
    return PublishResult(slug=names.prefix, title=str(episode.get("title") or names.prefix),
                         public_url=str(episode.get("mp3_url") or ""),
                         file_size_bytes=int(size) if str(size).isdigit() else 0,
                         script_url=urls["script"], notes_url=urls["notes"], manifest_url=urls["manifest"],
                         page_url=urls["page"] if page else None)


def _sections(notes: Mapping[str, Any], script: PreparedScript, *, slug: str, title: str, tier: str | None,
              urls: Mapping[str, str], work: Path, runner_factory=None) -> tuple[str, str, str]:
    """The section-notes path: (notes markdown, manifest, feed description) for one Surveillance show.

    The structure's blocks and the cut's intervals travel with the job, so this module never re-derives the
    cut. The ten end-to-end checks run here, before anything is uploaded: a failure names the checks and is
    the job's own error (retryable false: the model answered, the answer does not read).
    """
    blocks, intervals = notes.get("blocks"), notes.get("intervals")
    if not isinstance(blocks, list) or not isinstance(intervals, list):
        raise PipelineError("sections", "section notes need the structure's blocks and the cut's intervals in "
                            "the job input", retryable=False)
    if not script.timed_transcript:
        raise PipelineError("sections", "the stored segments.json is missing: section notes need the cut's clocks",
                            retryable=False)
    timed = json.loads(script.timed_transcript)
    starts = [float(p["start"]) for p in timed.get("paragraphs") or []]
    stream = build_stream(script.text)
    if len(starts) != len(stream.paragraphs):
        raise PipelineError("sections", f"{len(starts)} timed paragraphs vs {len(stream.paragraphs)} in the "
                            f"script: the cut's clocks are not this script's", retryable=False)
    try:
        parts = surveillance_notes.parts(stream, intervals, blocks)
    except surveillance_notes.SectionsError as exc:
        raise PipelineError(exc.stage, str(exc), retryable=False) from exc
    if tier is None:   # the notes job needs it: fail before any paid step, as the enrichment does
        raise PipelineError("enrichment_config", "the section notes ran without a model tier", retryable=False)
    try:
        role = resolve_role(surveillance_notes.SECTION_ROLE, tier=tier)
    except (EnrichmentConfigError, ValueError, KeyError) as exc:
        raise PipelineError("enrichment_config", str(exc), retryable=False) from exc
    try:
        result = surveillance_notes.run(
            stream=stream, parts=parts, starts=starts, title=title, slug=slug, work=work / "sections",
            runner_factory=runner_factory or default_runner_factory(work / "sections", tier),
            model={"role": surveillance_notes.SECTION_ROLE, **{k: role.get(k) for k in
                                                              ("provider", "model", "thinking")}})
    except (surveillance_notes.SectionsError, EnrichmentModelError) as exc:
        raise PipelineError(getattr(exc, "stage", "sections") or "sections", str(exc), retryable=True) from exc
    checks = surveillance_notes.checks(notes=result.notes, report=result.report, stream=stream, starts=starts,
                                       parts=parts, blocks=blocks)
    failed = [check for check in checks if not check.ok]
    if failed:
        raise PipelineError("notes_gate", "; ".join(f"{check.name}: {check.reason}" for check in failed),
                            retryable=False)
    analysis = {"summary": result.summary,
                "topics": [{"heading": topic["heading"], "startParagraphId": topic["startParagraphId"],
                            "endParagraphId": topic["endParagraphId"]} for topic in result.topics]}
    if notes.get("head"):
        # A condensed item's description is its head, its seek list and its links, as the condensed job
        # wrote them (surveillance_condensed.row): the notes job must not replace them with a summary.
        description = render_mod.render_item_description(
            head=str(notes["head"]), seek=list(notes.get("seek") or []),
            links=list(notes.get("links") or []))
    else:
        description = render_mod.build_feed_description(
            analysis=analysis, published=notes.get("publishedAt"),
            duration_seconds=notes.get("durationSeconds"), mp3_url=str(notes.get("mp3Url") or ""),
            notes_url=urls["notes"], script_url=urls["script"], source_url=notes.get("originalUrl"))
    manifest = render_mod.build_manifest(
        stream=stream, analysis=analysis, mentions={"topics": []},
        reports={"sections": result.report, "checks": [check.to_json() for check in checks]},
        title=title, slug=slug, published=notes.get("publishedAt"),
        date_basis=str(notes.get("dateBasis") or "unknown"), duration_seconds=notes.get("durationSeconds"),
        mp3_url=str(notes.get("mp3Url") or ""), notes_url=urls["notes"], script_url=urls["script"],
        source_url=notes.get("originalUrl"), stt_model=script.stt_engine, script_sha256=script.sha256,
        roles={"sections": surveillance_notes.SECTION_ROLE},
        topic_origin="surveillance_sections", summary_origin="surveillance_sections",
        timing=script.timing, segments_url=urls["segments"])
    logger.info("section notes for %s: %d blocks, %d sections, %d checks passed",
                slug, len(parts), int(result.report["counts"]["topics"]), len(checks))
    return result.notes, manifest, description


def _upload_documents(settings: Settings, feed: str, names, notes_markdown: str, manifest: str,
                      script: PreparedScript, work: Path, slug: str) -> bool:
    """Upload the notes and manifest once, then the page (an add-on: its failure never blocks them)."""
    prefix = feed_spec(feed)[0]
    for text, file_name in ((notes_markdown, names.notes_name), (manifest, names.manifest_name)):
        (work / file_name).write_text(text, encoding="utf-8")
        _upload_artifact(settings, work / file_name, f"{prefix}/episodes/{file_name}")
    return _page_addon(lambda: _upload_page(settings, feed, names, notes_markdown, script, work), slug)


def run_notes(settings: Settings, notes: Mapping[str, Any], work_dir: Path, tier: str | None,
              *, runner_factory=None) -> PublishResult:
    """Write the notes of one published episode, and switch the episode to them."""
    feed, slug, version = notes["feed"], notes["slug"], int(notes.get("version") or 1)
    episode = _get_episode_by_slug(settings, slug, feed)
    if episode is None:
        raise PipelineError("notes", f"no episode {slug} in feed {feed}", retryable=False)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    names = names_from_prefix(slug, version)
    urls = {"script": build_public_script_url(settings, names, feed),
            "notes": build_public_notes_url(settings, names, feed),
            "manifest": build_public_manifest_url(settings, names, feed),
            "segments": build_public_segments_url(settings, names, feed),
            "page": build_public_page_url(settings, names, feed)}
    finished = _finish_earlier(settings, notes, names, episode, urls, version, work)
    if finished is not None:
        return finished
    script = _stored_script(settings, notes, work)
    mp3_url = str(episode.get("mp3_url") or "")
    if notes.get("notesKind") == surveillance_notes.SECTIONS_KIND:
        title = str(notes.get("title") or episode.get("title") or slug)
        text, manifest, description = _sections(
            {**notes, "mp3Url": mp3_url}, script, slug=slug, title=title, tier=tier, urls=urls, work=work,
            runner_factory=runner_factory)
        page = _upload_documents(settings, feed, names, text, manifest, script, work, slug)
        return _switch(settings, feed, names, episode, urls, version, description, page=page)
    context = {"durationSeconds": notes.get("durationSeconds")}
    try:
        result = enrich_script(
            script=script, work_dir=work / "enrichment", title=str(notes.get("title") or episode.get("title") or slug),
            slug=slug, published=notes.get("publishedAt"), date_basis=str(notes.get("dateBasis") or "unknown"),
            duration_seconds=notes.get("durationSeconds"), mp3_url=mp3_url, notes_url=urls["notes"],
            script_url=urls["script"], source_url=notes.get("originalUrl"), tier=tier,
            segments_url=urls["segments"], runner_factory=runner_factory,
            # an episode's notes: a lede and the ranked points from the topics call, code-written clocks,
            # quotes and names, and the form gates in the manifest (X-104)
            notes_shape=NOTES_SHAPE_SECTIONS, speaker=_named_in(notes.get("speaker"), script.text),
            # the publisher's own description: the model reads it for spellings only (A-17)
            source_description=notes.get("sourceDescription"))
    except EnrichmentError as exc:
        if getattr(exc, "error_class", None) == "enrichment_config":
            raise PipelineError("enrichment_config", str(exc), retryable=False) from exc
        raise PipelineError("notes", f"{exc.stage}: {exc}", retryable=True) from exc
    except Exception as exc:
        if getattr(exc, "error_class", None) == "enrichment_config":
            raise PipelineError("enrichment_config", str(exc), retryable=False) from exc
        raise
    page = _upload_documents(settings, feed, names, result.notes_markdown, result.manifest, script, work, slug)
    logger.info("notes for %s v%d: %d segment(s), %d topics", slug, version,
                len(json.loads(result.manifest)["analysis"].get("segments") or []), result.topic_count)
    return _switch(settings, feed, names, episode, urls, version, result.description, page=page)


def page_job_payload(page: Mapping[str, Any], *, producer_ref: str | None = None) -> dict[str, Any]:
    """The page-only job, idempotent on (slug, version, page layout)."""
    from tg_tools_common.intent_contract import RuntimeJobIntent
    key = f"rss-audio:page:{page['slug']}:v{int(page['version'])}:l{PAGE_LAYOUT}"
    return RuntimeJobIntent(
        producer_system="rss-audio", producer_ref=producer_ref or f"page:{page['slug']}",
        result_owner="rss-audio", result_route="telegram.rss-audio.confirmation.v1",
        workload_type="rss_publish", queue_class="foreground", execution_profile="cpu_media",
        execution_shape="local_bounded", priority=PAGE_PRIORITY, input_json=dict(page),
        output_contract_json={"version": 1, "result": "an episode page from its stored notes, no delivery"},
        idempotency_key=key, effect_key=key.replace("rss-audio:page:", "rss-audio:feed-page:", 1),
        max_attempts=3).to_submit_payload()


def page_input(episode: Mapping[str, Any], feed: str) -> dict[str, Any]:
    """The page job for an episode that already has notes, at the version of its notes."""
    return {"kind": "rss", "inputKind": PAGE_KIND, "feed": feed, "slug": episode["slug"],
            "version": document_version(str(episode["slug"]), episode.get("md_url")) or 1,
            "title": episode.get("title") or episode["slug"], "delivery": "none"}


def run_page(settings: Settings, page: Mapping[str, Any], work_dir: Path) -> PublishResult:
    """The page of an episode that already has notes, from its stored notes and script: no model call."""
    feed, slug, version = page["feed"], page["slug"], int(page["version"])
    episode = _get_episode_by_slug(settings, slug, feed)
    if episode is None:
        raise PipelineError("page", f"no episode {slug} in feed {feed}", retryable=False)
    notes_version = document_version(slug, episode.get("md_url"))
    script_version = document_version(slug, episode.get("speech_text_url"))
    if notes_version != version or script_version != version:
        raise PipelineError("page", f"{slug} has notes v{notes_version} and script v{script_version}; this page is "
                            f"for v{version}", retryable=False)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    names = names_from_prefix(slug, version)
    stored = work / "stored"
    stored.mkdir(parents=True, exist_ok=True)
    if not _download(settings, feed, names.notes_name, stored / names.notes_name):
        raise PipelineError("page", f"{names.notes_name} is not in the feed's store", retryable=False)
    script = _stored_script(settings, {"feed": feed, "slug": slug, "version": version}, work)
    _upload_page(settings, feed, names, (stored / names.notes_name).read_text(encoding="utf-8"), script, work)
    page_url = build_public_page_url(settings, names, feed)
    _put(settings, episode, {"page_url": page_url}, require_page=True)
    size = episode.get("mp3_size") or 0
    return PublishResult(slug=slug, title=str(episode.get("title") or slug), public_url=str(episode.get("mp3_url") or ""),
                         file_size_bytes=int(size) if str(size).isdigit() else 0, page_url=page_url)
