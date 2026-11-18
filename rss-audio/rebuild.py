"""Rebuild an episode's transcript and notes from its stored mp3, by slug, in any feed (X-76 step 4).

    rebuild.py SLUG --feed F [--producer-ref REF] [--apply]

Dry run by default: it prints the plan and submits nothing. ``--apply`` submits one rebuild job.

The job (``inputKind: "rebuild"``) never refetches the source. It runs speech-to-text on the episode's
stored mp3 and uploads ``<prefix>.v<n>.script.md`` and ``.v<n>.segments.json`` (D1: uploads are
immutable, a new version never overwrites) and returns the notes request; the worker submits the
notes job as for a publish. The episode does not change until the notes job's one PUT switches its
script, notes and manifest together; only then are the older versions' documents deleted. A rebuild
that fails anywhere leaves the episode on its old documents. ``n`` is fixed when
the job is submitted (the episode's current script version plus one, or 1 when it has no script), so a
retry rebuilds the same version.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

from config import Settings
from episode_enrichment.naming import document_version, names_from_prefix
from pipeline import (PipelineError, PublishResult, _get_episode_by_slug, _probe_duration_seconds,
                      _transcribe, _upload_artifact, build_public_script_url, build_public_segments_url, feed_spec,
                      notes_input)
import notes_job

INPUT_KIND = "rebuild"
# Below every publish (scheduled 20, Telegram 60); its notes go lower still (notes_job.notes_priority).
PRIORITY = 10


def next_version(episode: Mapping[str, Any]) -> int:
    url = episode.get("speech_text_url")
    return (document_version(str(episode["slug"]), url) or 1) + 1 if url else 1


def rebuild_input(episode: Mapping[str, Any], feed: str, *, version: int) -> dict[str, Any]:
    return {"kind": "rss", "inputKind": INPUT_KIND, "feed": feed, "slug": episode["slug"], "version": version,
            "title": episode.get("title") or episode["slug"], "publishedAt": episode.get("pub_date"),
            "delivery": "none"}


def rebuild_job_payload(rebuild: Mapping[str, Any], *, producer_ref: str | None = None) -> dict[str, Any]:
    """Idempotent on (slug, version): submitting the same rebuild twice is one job."""
    from tg_tools_common.intent_contract import RuntimeJobIntent
    key = f"rss-audio:rebuild:{rebuild['slug']}:v{int(rebuild['version'])}"
    return RuntimeJobIntent(
        producer_system="rss-audio", producer_ref=producer_ref or f"rebuild:{rebuild['slug']}",
        result_owner="rss-audio", result_route="telegram.rss-audio.confirmation.v1",
        workload_type="rss_publish", queue_class="foreground", execution_profile="cpu_media",
        execution_shape="local_bounded", priority=PRIORITY, input_json=dict(rebuild),
        output_contract_json={"version": 1, "result": "a rebuilt episode transcript, no delivery"},
        idempotency_key=key, effect_key=key.replace("rss-audio:rebuild:", "rss-audio:feed-rebuild:", 1),
        max_attempts=3).to_submit_payload()


def run_rebuild(settings: Settings, rebuild: Mapping[str, Any], work_dir: Path, tier: str | None) -> PublishResult:
    """Speech-to-text from the stored mp3, publish version n's script, and return the notes request."""
    feed, slug, version = rebuild["feed"], rebuild["slug"], int(rebuild["version"])
    if not settings.enrichment_enabled:
        raise PipelineError("input", "a rebuild needs speech-to-text, and enrichment is off", retryable=False)
    episode = _get_episode_by_slug(settings, slug, feed)
    if episode is None:
        raise PipelineError("rebuild", f"no episode {slug} in feed {feed}", retryable=False)
    current = document_version(slug, episode.get("speech_text_url")) or 0
    if current > version:
        raise PipelineError("rebuild", f"{slug} is already at v{current}, past this rebuild's v{version}",
                            retryable=False)
    work = Path(work_dir)
    stored = work / "stored"   # not the work root: the mp3 is not this job's artifact
    stored.mkdir(parents=True, exist_ok=True)
    mp3 = stored / f"{slug}.mp3"
    # terminal only when the store confirms the mp3 is gone; a failed read raises, retryable
    if not mp3.is_file() and not notes_job._download(settings, feed, mp3.name, mp3):
        raise PipelineError("rebuild", f"{mp3.name} is not in the feed's store", retryable=False)
    script = _transcribe(settings, mp3, work / "enrichment", tier)
    if script is None:
        raise PipelineError("stt", f"speech-to-text failed for {mp3.name}", retryable=True)

    names, prefix = names_from_prefix(slug, version), feed_spec(feed)[0]
    script_url = build_public_script_url(settings, names, feed)
    segments_url = None
    for text, file_name in ((script.text, names.script_name), (script.timed_transcript, names.segments_name)):
        if text is None:
            continue
        (work / file_name).write_text(text, encoding="utf-8")
        _upload_artifact(settings, work / file_name, f"{prefix}/episodes/{file_name}")
    if script.timed_transcript is not None:
        segments_url = build_public_segments_url(settings, names, feed)
    notes = notes_input(feed=feed, slug=slug, version=version, script_sha256=script.sha256,
                        title=str(rebuild.get("title") or episode.get("title") or slug),
                        published_at=rebuild.get("publishedAt") or episode.get("pub_date"),
                        date_basis=str(rebuild.get("dateBasis") or "unknown"),
                        duration_seconds=_probe_duration_seconds(settings, mp3) or None,
                        source_url=rebuild.get("originalUrl"), stt_engine=script.stt_engine)
    size = episode.get("mp3_size") or 0
    return PublishResult(slug=slug, title=str(episode.get("title") or slug), public_url=str(episode.get("mp3_url") or ""),
                         file_size_bytes=int(size) if str(size).isdigit() else 0, script_url=script_url,
                         segments_url=segments_url, notes=notes)


def submit_line(payload: Mapping[str, Any], submitted: Mapping[str, Any]) -> str:
    """What the controller did with a submit: ``created``, or ``existing`` with that job's status (a dead
    one means this key is spent)."""
    job = submitted.get("job") or {}
    return (f"{submitted.get('action') or 'submitted'} {payload['idempotencyKey']}: "
            f"job {job.get('jobId')} {job.get('status')}")


def plan(settings: Settings, slug: str, feed: str, *, producer_ref: str | None = None) -> tuple[dict, list[str]]:
    """(the submit payload, the plan's lines). Reads the episode; writes nothing."""
    episode = _get_episode_by_slug(settings, slug, feed)
    if episode is None:
        raise PipelineError("rebuild", f"no episode {slug} in feed {feed}", retryable=False)
    version = next_version(episode)
    rebuild = rebuild_input(episode, feed, version=version)
    names = names_from_prefix(slug, version)
    current = episode.get("speech_text_url")
    lines = [f"rebuild {slug} in {feed}: v{version}",
             f"  now        script {document_version(slug, current) if current else 'none'}, "
             f"notes {'yes' if episode.get('md_url') else 'none'}",
             f"  stt from   {slug}.mp3 (stored; no refetch)",
             f"  writes     {names.script_name}, {names.segments_name}, then {names.notes_name}, {names.manifest_name}",
             "  after      older versions' documents deleted" if version > 1 else "  after      nothing deleted"]
    return rebuild_job_payload(rebuild, producer_ref=producer_ref), lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("slug")
    parser.add_argument("--feed", required=True)
    parser.add_argument("--producer-ref")
    parser.add_argument("--apply", action="store_true", help="submit the job (default: print the plan)")
    args = parser.parse_args(argv)
    import config
    from runtime_client import RuntimeClient
    try:
        settings = config.worker_settings()[0]
    except Exception as exc:   # the environment holds credentials: name the type only
        print(f"rebuild: {config.WORKER_UNIT} settings unreadable ({type(exc).__name__})", file=sys.stderr)
        return 1
    try:
        payload, lines = plan(settings, args.slug, args.feed, producer_ref=args.producer_ref)
    except PipelineError as exc:
        print(f"rebuild: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    if not args.apply:
        print("dry run: nothing submitted (--apply submits)")
        return 0
    submitted = RuntimeClient(settings).submit_job(payload) or {}
    print(submit_line(payload, submitted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
