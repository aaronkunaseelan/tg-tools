"""Deterministic renderers for separate episode script and notes documents.

Two notes shapes live here: the episode one (``render_notes_markdown``: summary, topics, references) and
the section one (``render_section_notes``: code owns names, firms, clocks and quotes; the model writes the
surnames, the counts and the ledes). Both are pure: no model call, no clock of their own.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from tg_tools_common.public_source import public_source_reference

from .paragraphs import normalize
from .speakers import surname

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


def _facts(*, published=None, duration_seconds=None, mp3_url=None, script_url=None,
           source_url=None, source_kind="audio") -> list[str]:
    """The provenance lines under the title: one shape for both notes documents."""
    facts = [f"- Original published: {published or UNKNOWN_DATE}"]
    duration = _format_duration(duration_seconds)
    if duration:
        facts.append(f"- Duration: {duration}")
    links = []
    if mp3_url:
        links.append(f"[MP3]({mp3_url})")
    if script_url:
        links.append(f"[Script]({script_url})")
    source = public_source_reference(source_url)
    if source:
        links.append(f"[Original]({source})")
    if links:
        facts.append("- Links: " + " · ".join(links))
    facts.append("- Analysis: Machine-generated from the transcript" if source_kind == "audio"
                 else "- Analysis: Machine-generated from the final reader script")
    return facts


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
    sections: list | None = None,
) -> str:
    """Render analysis only; the complete script lives in its own document.

    ``sections`` [(label, analysis, mentions)], from a segmented enrichment, gives one ``## label``
    section per segment; without it the layout is the single-pass one.
    """
    lines: list[str] = [f"# {title}".rstrip(), ""]
    facts = _facts(published=published, duration_seconds=duration_seconds, mp3_url=mp3_url,
                   script_url=script_url, source_url=source_url, source_kind=source_kind)
    lines.extend(facts)
    if sections:
        for label, part_analysis, part_mentions in sections:
            lines.extend(["", f"## {label}", "", str(part_analysis.get("summary", "")).strip(), ""])
            _analysis_lines(lines, part_analysis, part_mentions, "###")
    else:
        lines.extend(["", "## Summary", "", str(analysis.get("summary", "")).strip(), ""])
        _analysis_lines(lines, analysis, mentions, "##")
    return "\n".join(lines).rstrip() + "\n"


def _analysis_lines(lines: list[str], analysis: dict, mentions: dict, level: str) -> None:
    """The topic list and each topic's references, under ``level`` headings."""
    by_heading = {
        normalize(topic.get("heading", "")): topic.get("mentions") or []
        for topic in (mentions.get("topics") or [])
    }
    lines.extend([f"{level} Topics", ""])
    for topic in analysis.get("topics") or []:
        lines.append(
            f"- **{topic['heading']}** — {topic['startParagraphId']} to "
            f"{topic['endParagraphId']}")

    for topic in analysis.get("topics") or []:
        topic_mentions = by_heading.get(normalize(topic["heading"]), [])
        if not topic_mentions:
            continue
        lines.extend(["", f"{level} {topic['heading']}", ""])
        for mention in topic_mentions:
            entry = f"- **{_label(mention['type'])}: {mention['displayText']}**"
            referenced = str(mention.get("referencedAs") or "").strip()
            if referenced:
                entry += f" — {referenced}"
            lines.append(entry)
            lines.append(
                f"  - Evidence ({mention['paragraphId']}): “{mention['evidenceQuote']}”")


# ---------------------------------------------------------------- section notes (X-104)
#
# The model writes a surname, a section count and a lede; this code writes the rest of the document: the
# full name at its first appearance, the firm with it, and every clock. A section's clock is the caller's
# lookup (``at``), already formatted, so this module never has a clock format of its own.


def _owner(speakers: list | None, paragraph_id: str) -> tuple[list[str], dict]:
    """The interval that owns a paragraph: its guest names (a panel has several) and its firm map."""
    for entry in speakers or []:
        first = str(entry.get("first") or "")
        last = str(entry.get("last") or "")
        if first and last and first <= paragraph_id <= last:
            names = [str(n).strip() for n in entry.get("names") or [] if str(n).strip()]
            return names, entry.get("firms") or {}
    return [], {}


def _name_lead(text: object, names: list[str], firms: dict, introduced: set) -> tuple[str, bool]:
    """Expand a leading owning surname to "Full Name of Firm" on its first appearance.

    The second element says the text starts with an owner's surname, so the section is attributed even when
    that guest was named earlier: the caller stops looking, exactly as the first-bullet rule always did. Two
    names with one surname in the same interval (two Kabras on a panel) leave the surname alone: the answer
    cannot say which one, and guessing would put one guest's firm under the other's claim (D19).
    """
    text = str(text or "").lstrip()
    surs = [surname(name) for name in names]
    shared = {s for s in surs if surs.count(s) > 1}
    for name in names:
        sur = surname(name)
        if not sur or not text.startswith(sur + " "):
            continue
        if sur in shared or name in introduced:
            return text, True
        firm = str(firms.get(name) or "").strip()
        if "contributor" in firm.lower():                      # "Bloomberg Contributor" is not a firm
            firm = ""
        rest = text[len(sur) + 1:]                              # the leading token only
        if firm:
            dup = f"of {firm} "
            while rest[:len(dup)].lower() == dup.lower():        # "Gardner of Stifel" -> one firm
                rest = rest[len(dup):]
            prefix = f"{name} of {firm} "
        else:
            prefix = f"{name} "
        introduced.add(name)
        return prefix + rest, True
    return text, False


def _attributed(topics: list[dict], speakers: list[dict], introduced: set | None = None) -> list[dict]:
    """Copy the sections, expanding each guest's first surname in the lede (else the first bullet).

    ``introduced`` carries the document's state when a part is rendered before this one (the episode
    notes' main points): one set over the whole document, and the first appearance wins.
    """
    introduced = set() if introduced is None else introduced
    out = []
    for topic in topics:
        bullets = [dict(b) for b in topic.get("bullets") or []]
        lede = str(topic.get("lede") or "").strip()
        start = str(topic.get("startParagraphId") or "")
        names, firms = _owner(speakers, start) if start else ([], {})
        if names:
            # the model often writes the full name in the lede itself: that is the first appearance.
            for name in names:
                if name.lower() in lede.lower():
                    introduced.add(name)
            # document order: the lede is read before the bullets, so it gets the first mention.
            for kind in (["lede"] if lede else []) + (["bullets"] if bullets else []):
                text = lede if kind == "lede" else str(bullets[0].get("text") or "")
                expanded, matched = _name_lead(text, names, firms, introduced)
                if kind == "lede":
                    lede = expanded
                else:
                    bullets[0]["text"] = expanded
                if matched:
                    break
        out.append({**topic, "lede": lede, "bullets": bullets})
    return out


def _mention(mention: dict) -> str:
    text = str(mention.get("displayText") or "").strip()
    note = str(mention.get("note") or "").strip()
    return f"{text} ({note})" if note else text


def quote_of(texts: dict, paragraph_id: object, words: object) -> str:
    """The first ``words`` words of one paragraph, verbatim; "" when the paragraph or the count is not usable.

    The count is the model's only say in the quote: 3..15 words, taken from the start of the paragraph.
    Nothing is searched for, so a quote cannot drift into the middle of a sentence.
    """
    if not isinstance(words, int) or isinstance(words, bool) or not 3 <= words <= 15:
        return ""
    text = texts.get(paragraph_id)
    if not isinstance(text, str):
        return ""
    return " ".join(text.split()[:words])


def _word_key(word: str) -> str:
    """One word's comparison key: normalized, its leading and trailing punctuation folded away.

    The answer's evidence quote ends where the sentence does ("…per litre"), while the paragraph's word
    carries the full stop ("litre."): the quote is a substring of the paragraph, not a word boundary.
    """
    return re.sub(r"^[^0-9a-z]+|[^0-9a-z]+$", "", normalize(word))


def quote_from(texts: dict, paragraph_id: object, mention: dict | None = None) -> str:
    """The quote text, written by code from the transcript paragraph, never by the answer.

    Two contracts reach this one function. The section path's ``quote.words`` names a count, so the quote
    is the paragraph's first ``words`` words (``quote_of``). The episode path's answer carries an
    ``evidenceQuote`` that its own gate proved verbatim in the paragraph: the paragraph's own words under
    it are returned, so the notes never show text the transcript does not.
    """
    mention = mention or {}
    paragraph_id = str(paragraph_id or mention.get("paragraphId") or "")
    text = texts.get(paragraph_id)
    if not isinstance(text, str):
        return ""
    quote = quote_of(texts, paragraph_id, (mention.get("quote") or {}).get("words"))
    if quote:
        return quote
    evidence = normalize(mention.get("evidenceQuote") or "")
    if not evidence:
        return ""
    words = text.split()
    wanted = [_word_key(word) for word in evidence.split()]
    keys = [_word_key(word) for word in words]
    for start in range(len(words) - len(wanted) + 1):
        if keys[start:start + len(wanted)] == wanted:
            return " ".join(words[start:start + len(wanted)])
    return ""


def render_episode_sections(*, title: str, summary: str, main_points: list[dict], topics: list[dict],
                            references: list[dict] | None = None, speakers: list[dict] | None = None,
                            at=None, texts: dict | None = None, published: str | None = None,
                            duration_seconds: int | float | None = None, mp3_url: str | None = None,
                            script_url: str | None = None, source_url: str | None = None,
                            source_kind: str = "audio") -> str:
    """An ordinary episode's notes in the section shape: the summary, the ranked points, then one
    ``### clock · heading`` section per topic with its lede and its references (X-104 upstream).

    The code owns what the pilot's renderer owned: every clock (``at(paragraph_id)``), every quote (read
    from ``texts`` by ``quote_from``), and the first appearance of a known speaker's surname, which becomes
    "Full Name of Firm" once per document — main points first, then the sections. ``topics``
    [{"heading", "lede", "startParagraphId", "endParagraphId", "paragraphId"}], ``main_points``
    [{"text", "paragraphId"}], ``references`` the mentions answer's [{"heading", "mentions"}].
    """
    speakers = speakers or []
    introduced: set = set()
    points = _attributed_points(main_points or [], speakers, introduced)
    topics = _attributed(topics or [], speakers, introduced)
    by_heading = {normalize(topic.get("heading", "")): topic.get("mentions") or []
                  for topic in references or []}
    texts = texts or {}
    lines: list[str] = [f"# {title}".rstrip(), ""]
    lines.extend(_facts(published=published, duration_seconds=duration_seconds, mp3_url=mp3_url,
                        script_url=script_url, source_url=source_url, source_kind=source_kind))
    summary_text = str(summary or "").strip()
    if summary_text:
        lines.extend(["", "## Summary", "", summary_text])
    if points:
        lines.extend(["", "## Main points", ""])
        for point in points:
            clock = _clock_of(at, point.get("paragraphId"))
            text = str(point.get("text") or "").strip()
            lines.append(f"- {clock} · {text}" if clock else f"- {text}")
    lines.extend(["", "## Topics"])
    for topic in topics:
        heading = str(topic.get("heading") or "").strip()
        clock = _clock_of(at, topic.get("startParagraphId"))
        lines.extend(["", f"### {clock} · {heading}" if clock else f"### {heading}", ""])
        lede = str(topic.get("lede") or "").strip()
        if lede:
            lines.extend([lede, ""])
        for mention in by_heading.get(normalize(heading), []):
            entry = f"- **{_label(mention['type'])}: {mention['displayText']}**"
            referenced = str(mention.get("referencedAs") or "").strip()
            if referenced:
                entry += f" — {referenced}"
            lines.append(entry)
            paragraph_id = str(mention.get("paragraphId") or "")
            evidence_clock = _clock_of(at, paragraph_id)
            pointer = f"{paragraph_id} · {evidence_clock}" if evidence_clock else paragraph_id
            quote = quote_from(texts, paragraph_id, mention)
            lines.append(f"  - Evidence ({pointer}): “{quote}”" if quote else f"  - Evidence ({pointer})")
    return "\n".join(line.rstrip() for line in "\n".join(lines).split("\n")).rstrip() + "\n"


def _clock_of(at, paragraph_id) -> str:
    """The section's own clock, already formatted by the caller; "" when it has none or no lookup."""
    paragraph_id = str(paragraph_id or "")
    return str(at(paragraph_id) or "").strip() if at and paragraph_id else ""


def _attributed_points(points: list[dict], speakers: list[dict], introduced: set) -> list[dict]:
    """The main points with the same first-appearance rule, before the sections in document order."""
    out = []
    for point in points:
        text = str(point.get("text") or "").strip()
        cited = str(point.get("paragraphId") or "")
        names, firms = _owner(speakers, cited) if cited else ([], {})
        if names:
            for name in names:
                if name.lower() in text.lower():
                    introduced.add(name)
            text, _ = _name_lead(text, names, firms, introduced)
        out.append({**point, "text": text})
    return out


def render_section_notes(*, title: str, summary: str, main_points: list[dict], topics: list[dict],
                         speakers: list[dict] | None = None, at=None) -> str:
    """The section-notes document: one section per model answer, code-written names and clocks.

    ``main_points`` [{"text", "clock"}], ``topics`` [{"heading", "lede", "startParagraphId", "bullets",
    "mentions"}], ``speakers`` [{"first", "last", "names", "firms"}] and ``at(paragraph_id) -> clock``.
    A section without a lede keeps the older layout: heading, blank line, bullets.
    """
    topics = _attributed(topics or [], speakers or [])
    blocks = [f"# {title or ''}"]
    summary_text = str(summary or "").strip()
    if summary_text:
        blocks.append(summary_text)
    points = []
    for point in main_points or []:
        clock = str(point.get("clock") or "").strip()
        text = str(point.get("text") or "").strip()
        points.append(f"- {clock} · {text}" if clock else f"- {text}")
    blocks.append("## Main points" + ("\n\n" + "\n".join(points) if points else ""))
    blocks.append("## Topics")
    for topic in topics:
        start = str(topic.get("startParagraphId") or "")
        clock = str(at(start) or "").strip() if start and at else ""
        heading = str(topic.get("heading") or "").strip()
        head = f"{clock} · {heading}" if clock else heading
        # a plain paragraph, never a bullet and never italic; an absent or empty lede adds nothing.
        lede = str(topic.get("lede") or "").strip()
        bullets = [f"- {str(b.get('text') or '').strip()}" for b in topic.get("bullets") or []]
        if not bullets:
            blocks.append(f"*{head}*" + (f"\n\n{lede}" if lede else ""))
            continue
        block = "\n".join([f"### {head}", ""] + ([lede, ""] if lede else []) + bullets)
        mentions = topic.get("mentions") or []
        if mentions:
            block += "\n\n" + f"Worth noting: {' · '.join(_mention(m) for m in mentions)}"
        blocks.append(block)
    return "\n".join(line.rstrip() for line in "\n\n".join(blocks).split("\n")) + "\n"


# ---------------------------------------------------------------- the feed description (seek list, X-104)
#
# A condensed item and its parent share one shape: a headline line, the seek list (who speaks when), then
# links. The seek list lives between two markers of its own so a rewrite replaces our block and never the
# publisher's text, and so the parent's summary is never parsed back out of the description (D17).

SEEK_HEAD = "<p><strong>Seek</strong><br>"


def seek_paragraph(lines: list[str]) -> str:
    """The seek list as one HTML paragraph: one line per interval, already stamped by the caller."""
    lines = [str(line).strip() for line in lines or [] if str(line).strip()]
    return f"{SEEK_HEAD}\n{'<br>\n'.join(lines)}</p>" if lines else ""


def render_item_description(*, head: str = "", seek: list[str] | None = None,
                            links: list[str] | None = None) -> str:
    """A condensed item's description: what it is, then who speaks when, then its links."""
    blocks = [f"<p>{head}</p>" if head else "", seek_paragraph(list(seek or []))]
    if links:
        blocks.append("<p>" + " · ".join(links) + "</p>")
    return "\n".join(block for block in blocks if block)


def with_seek(description: str | None, *, seek: list[str] | None = None,
              links: list[str] | None = None) -> str:
    """A description with the seek list under it: the text above is kept, our own old block replaced."""
    base = split_seek(str(description or ""))[0]
    blocks = [base, seek_paragraph(list(seek or []))]
    if links:
        blocks.append("<p>" + " · ".join(links) + "</p>")
    return "\n".join(block for block in blocks if block)


def split_seek(description: str | None) -> tuple[str, str]:
    """(the text above our seek block, the block itself with its links), so a rewrite can keep the block."""
    base = str(description or "")
    if SEEK_HEAD not in base:
        return base, ""
    head, block = base.split(SEEK_HEAD, 1)
    return head.strip(), SEEK_HEAD + block


def keep_seek(description: str, existing: str | None) -> str:
    """A rewritten description that has no seek list of its own keeps the one the live row already carries.

    The parent's own notes job rewrites its description too (``build_feed_description``); whichever job writes
    last, the seek list stays.
    """
    if SEEK_HEAD in description:
        return description
    _, block = split_seek(existing)
    head = description.strip()
    return f"{head}\n{block}" if block and head else description


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
    timing: dict | None = None, segments_url: str | None = None, segments: list | None = None,
    feed_description: str | None = None,
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
            **({"segmentsUrl": segments_url} if segments_url else {}),
        },
        "script": {
            "paragraphs": len(stream),
            "words": stream.total_words,
            "streamSha256": stream.stream_sha256,
            "scriptSha256": script_sha256,
            "sttModel": stt_model,
            "contentOrigin": content_origin,
            # When each paragraph was spoken: basis words | segments | chunks (Azure, per 10-min chunk), or
            # null with the reason. The times are in artifacts.segmentsUrl.
            **({"timing": timing} if timing is not None else {}),
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
            # The parts the notes were enriched in (episode_enrichment.segments); one for a single pass.
            **({"segments": segments} if segments is not None else {}),
        },
        "gates": reports,
        "disclaimer": disclaimer,
    }
    if feed_description is not None:   # the episode row's description, so a later attempt can finish this one
        payload["feedDescription"] = feed_description
    if source_kind == "script":
        payload.update({
            "sourceKind": source_kind,
            "contentOrigin": content_origin,
            "topicOrigin": topic_origin,
            "summaryOrigin": summary_origin,
            "scriptSha256": script_sha256,
        })
    return json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
