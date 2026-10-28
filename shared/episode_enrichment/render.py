"""Deterministic renderers for separate episode script and notes documents."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from tg_tools_common.public_source import public_source_reference

from .paragraphs import normalize

UNKNOWN_DATE = "Unknown"
_TYPE_LABELS = {
    "product_or_service": "product",
    "article_or_paper": "article",
    "poem_song_or_film": "work",
    "speech_or_document": "document",
    "historical_event": "historical event",
    "meeting_or_institution": "institution",
    "named_phenomenon": "phenomenon",
}


def _label(kind: str) -> str:
    return _TYPE_LABELS.get(kind, kind.replace("_", " "))


def _format_duration(seconds: int | float | None) -> str | None:
    if not seconds or seconds <= 0:
        return None
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def render_script_markdown(script_text: str) -> str:
    """Return the canonical script without adding unspoken presentation text."""
    if not isinstance(script_text, str) or not script_text.strip():
        raise ValueError("script contains no text")
    return script_text


def render_notes_markdown(
    *,
    stream,
    analysis: dict,
    mentions: dict,
    title: str,
    published: str | None = None,
    duration_seconds: int | float | None = None,
    mp3_url: str | None = None,
    script_url: str | None = None,
    source_url: str | None = None,
    source_kind: str = "audio",
) -> str:
    """Render analysis only; the complete script lives in its own document."""
    by_heading = {
        normalize(topic.get("heading", "")): topic.get("mentions") or []
        for topic in (mentions.get("topics") or [])
    }
    lines: list[str] = [f"# {title}".rstrip(), ""]
    facts = [f"- Original published: {published or UNKNOWN_DATE}"]
    duration = _format_duration(duration_seconds)
    if duration:
        facts.append(f"- Duration: {duration}")
    links = []
    if mp3_url:
        links.append(f"[MP3]({mp3_url})")
    if script_url:
        links.append(f"[Script]({script_url})")
    source_url = public_source_reference(source_url)
    if source_url:
        links.append(f"[Original]({source_url})")
    if links:
        facts.append("- Links: " + " · ".join(links))
    facts.append("- Analysis: Machine-generated from the transcript" if source_kind == "audio"
                 else "- Analysis: Machine-generated from the final reader script")
    lines.extend(facts)
    lines.extend(["", "## Summary", "", str(analysis.get("summary", "")).strip(), ""])
    lines.extend(["## Topics", ""])
    for topic in analysis.get("topics") or []:
        lines.append(
            f"- **{topic['heading']}** — {topic['startParagraphId']} to "
            f"{topic['endParagraphId']}")

    for topic in analysis.get("topics") or []:
        topic_mentions = by_heading.get(normalize(topic["heading"]), [])
        if not topic_mentions:
            continue
        lines.extend(["", f"## {topic['heading']}", ""])
        for mention in topic_mentions:
            entry = f"- **{_label(mention['type'])}: {mention['displayText']}**"
            referenced = str(mention.get("referencedAs") or "").strip()
            if referenced:
                entry += f" — {referenced}"
            lines.append(entry)
            lines.append(
                f"  - Evidence ({mention['paragraphId']}): “{mention['evidenceQuote']}”")

    return "\n".join(lines).rstrip() + "\n"


def build_feed_description(
    *, analysis: dict, published: str | None = None,
    duration_seconds: int | float | None = None, mp3_url: str | None = None,
    notes_url: str | None = None, script_url: str | None = None,
    source_url: str | None = None, source_kind: str = "audio",
) -> str:
    parts = [str(analysis.get("summary", "")).strip()]
    headings = [topic["heading"] for topic in analysis.get("topics") or []]
    if headings:
        parts.append("Topics: " + "; ".join(headings) + ".")
    facts = [f"Original published: {published or UNKNOWN_DATE}."]
    duration = _format_duration(duration_seconds)
    if duration:
        facts.append(f"Duration: {duration}.")
    parts.append(" ".join(facts))
    document_label = "Notes" if script_url else ("Transcript" if source_kind == "audio" else "Script")
    for label, url in (("MP3", mp3_url), (document_label, notes_url), ("Script", script_url),
                       ("Original", public_source_reference(source_url))):
        if url:
            parts.append(f"{label}: {url}")
    return "\n\n".join(part for part in parts if part).strip()


def build_manifest(
    *, stream, analysis: dict, mentions: dict, reports: dict, title: str,
    slug: str | None = None, published: str | None = None,
    date_basis: str = "unknown", duration_seconds: int | float | None = None,
    mp3_url: str | None = None, notes_url: str | None = None,
    script_url: str | None = None, source_url: str | None = None,
    stt_model: str | None = None,
    roles: dict | None = None, source_kind: str = "audio",
    content_origin: str = "stt_transcript", topic_origin: str = "luna_topics",
    summary_origin: str = "luna_summary", script_sha256: str | None = None,
) -> str:
    disclaimer = (
        "Summary, topic headings, and reference notes are machine-generated from "
        "the transcript. Evidence quotes prove only that words occur in the script; "
        "the script is unmodified speech-to-text output."
        if source_kind == "audio" else
        "Summary, topic headings, and reference notes are machine-generated or "
        "plan-derived from the final speech script. Evidence quotes are script-bound."
    )
    payload = {
        "schemaVersion": 2,
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "slug": slug,
        "title": title,
        "source": {
            "publishedAt": published,
            "dateBasis": date_basis,
            "sourceUrl": public_source_reference(source_url),
            "durationSeconds": duration_seconds,
        },
        "artifacts": {
            "mp3Url": mp3_url,
            "scriptUrl": script_url,
            "notesUrl": notes_url,
        },
        "script": {
            "paragraphs": len(stream),
            "words": stream.total_words,
            "streamSha256": stream.stream_sha256,
            "scriptSha256": script_sha256,
            "sttModel": stt_model,
            "contentOrigin": content_origin,
        },
        "transcript": {
            "paragraphs": len(stream),
            "words": stream.total_words,
            "streamSha256": stream.stream_sha256,
            "sttModel": stt_model,
            **({"scriptSha256": script_sha256} if script_sha256 else {}),
        },
        "analysis": {
            "roles": roles or {},
            "topicOrigin": topic_origin,
            "summaryOrigin": summary_origin,
            "topics": [
                {"heading": topic["heading"],
                 "startParagraphId": topic["startParagraphId"],
                 "endParagraphId": topic["endParagraphId"]}
                for topic in analysis.get("topics") or []
            ],
            "mentionCount": sum(
                len(topic.get("mentions") or [])
                for topic in mentions.get("topics") or []),
        },
        "gates": reports,
        "disclaimer": disclaimer,
    }
    if source_kind == "script":
        payload.update({
            "sourceKind": source_kind,
            "contentOrigin": content_origin,
            "topicOrigin": topic_origin,
            "summaryOrigin": summary_origin,
            "scriptSha256": script_sha256,
        })
    return json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
