#!/usr/bin/env python3
"""Offline transcript enrichment: audio + metadata -> Markdown, manifest, JSON.

Writes everything into a work directory and uploads nothing. Use this to review
output before it can reach a feed, and to re-run tuning against fixtures.

    ./enrich.py --audio episode.mp3 --title 'Episode title' \
        --workdir /tmp/enrich-check --published 2026-08-03

Re-running against the same work directory reuses completed stages, so a Luna
failure does not re-pay for transcription.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

from episode_enrichment import AudioSttSource, EnrichmentError, enrich  # noqa: E402
from episode_enrichment.stt import DEFAULT_STT_MODEL  # noqa: E402

log = logging.getLogger("enrich")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enrich a transcript offline; no uploads, no episode API")
    parser.add_argument("--audio", required=True, help="Path to a local audio file")
    parser.add_argument("--workdir", required=True, help="Durable work directory")
    parser.add_argument("--title", required=True, help="Episode title")
    parser.add_argument("--slug", default=None)
    parser.add_argument(
        "--published", default=None,
        help="Original publication date. Omit when genuinely unknown; "
             "it renders as 'Unknown' rather than a fabricated date.")
    parser.add_argument(
        "--date-basis", default="unknown",
        choices=["extractor_upload_date", "rss_item_pubdate", "operator", "unknown"])
    parser.add_argument("--duration", type=int, default=None, help="Seconds")
    parser.add_argument("--mp3-url", default=None)
    parser.add_argument("--notes-url", "--md-url", dest="notes_url", default=None)
    parser.add_argument("--script-url", default=None)
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--stt-model", default=DEFAULT_STT_MODEL,
                        help="mini (default) or full")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)

    audio = Path(args.audio).expanduser()
    if not audio.is_file():
        log.error("audio file not found: %s", audio)
        return 2

    try:
        result = enrich(
            source=AudioSttSource(audio, stt_model=args.stt_model, jobs=args.jobs),
            work_dir=Path(args.workdir).expanduser(),
            title=args.title,
            slug=args.slug,
            published=args.published,
            date_basis=args.date_basis,
            duration_seconds=args.duration,
            mp3_url=args.mp3_url,
            notes_url=args.notes_url,
            script_url=args.script_url,
            source_url=args.source_url,
            stt_model=args.stt_model,
            jobs=args.jobs,
        )
    except EnrichmentError as exc:
        log.error("enrichment failed at %s: %s", exc.stage, exc)
        return 1

    dropped = (result.reports.get("mentions") or {}).get("stats", {}).get("mentionsDropped")
    summary = {
        "workdir": str(Path(args.workdir).expanduser()),
        "paragraphs": len(result.stream),
        "words": result.stream.total_words,
        "topics": result.topic_count,
        "mentions": result.mention_count,
        "mentionsDropped": dropped,
        "scriptPath": str(Path(args.workdir).expanduser() / "script.md"),
        "notesPath": str(Path(args.workdir).expanduser() / "notes.md"),
        "scriptChars": len(result.script_markdown),
        "notesChars": len(result.notes_markdown),
        "descriptionChars": len(result.description),
    }
    print(json.dumps(summary, indent=1))
    for note in (result.reports.get("mentions") or {}).get("notes", [])[:20]:
        log.info("note: %s", note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
