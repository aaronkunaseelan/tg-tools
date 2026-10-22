"""Deterministic presentation cleanup for faithful-read episodes.

The extractor output remains untouched as source evidence.  This module creates
only the publication/speech copy: it removes layout markup, normalizes major
headings, and derives a bounded topic list without rewriting source prose.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

CLEANUP_VERSION = "faithful_cleanup_v1"
MAX_TOPICS = 14
MAX_DESCRIPTION_CHARS = 3000

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_FIGURE_RE = re.compile(r"<figure(?:\s[^>]*)?>\s*(.*?)\s*</figure>", re.S | re.I)
_FIGCAPTION_RE = re.compile(r"<figcaption(?:\s[^>]*)?>(.*?)</figcaption>", re.S | re.I)
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.\s*(?:\[[^]]+\]\s*)?(.+)$")
_PART_SUFFIX_RE = re.compile(r"\s*[—–-]\s*Part\s+\d+\s+of\s+\d+\s*$", re.I)


@dataclass(frozen=True)
class FaithfulCleanup:
    markdown: str
    topics: list[str]
    section_headings: list[str]
    diagnostics: dict[str, Any]


def _plain(text: str) -> str:
    text = html.unescape(_TAG_RE.sub("", text))
    return re.sub(r"\s+", " ", text).strip()


def _base_title(title: str) -> str:
    return _PART_SUFFIX_RE.sub("", title).strip()


def _figure_replacement(match: re.Match[str], counts: dict[str, int]) -> str:
    body = match.group(1)
    caption = _FIGCAPTION_RE.search(body)
    if caption:
        counts["figure_captions_preserved"] += 1
        return "\n\n" + _plain(caption.group(1)) + "\n\n"
    counts["decorative_figures_removed"] += 1
    return "\n"


def _find_byline(lines: list[str], base_title: str, abstract_index: int) -> str:
    wanted = re.sub(r"\W+", "", base_title).casefold()
    for index, line in enumerate(lines[:abstract_index]):
        match = _HEADING_RE.match(line.strip())
        if not match:
            continue
        candidate = re.sub(r"\W+", "", _plain(match.group(2))).casefold()
        if candidate != wanted:
            continue
        for following in lines[index + 1 : min(index + 6, abstract_index)]:
            value = _plain(following)
            if value and not value.startswith("#") and len(value) <= 500:
                return value
    return ""


def clean_faithful_markdown(markdown: str, *, episode_title: str) -> FaithfulCleanup:
    """Create a clean publication copy while preserving substantive prose."""
    counts = {
        "html_comments_removed": len(_COMMENT_RE.findall(markdown)),
        "decorative_figures_removed": 0,
        "figure_captions_preserved": 0,
        "duplicate_headings_removed": 0,
        "headings_normalized": 0,
        "academic_preamble_removed": 0,
    }
    text = _COMMENT_RE.sub("\n", markdown)
    text = _FIGURE_RE.sub(lambda match: _figure_replacement(match, counts), text)
    text = _TAG_RE.sub("", text)
    lines = [line.rstrip() for line in text.splitlines()]

    # Academic PDFs often put journal furniture, affiliations, and publication
    # metadata before an explicit abstract. Keep the byline and begin the read
    # at the abstract; the raw materialised Markdown remains available intact.
    abstract_index = next(
        (i for i, line in enumerate(lines[:400]) if re.match(r"^\s*Abstract\s*:\s*\S", line, re.I)),
        -1,
    )
    byline = ""
    if abstract_index >= 0:
        byline = _find_byline(lines, _base_title(episode_title), abstract_index)
        counts["academic_preamble_removed"] = abstract_index
        lines = lines[abstract_index:]

    output = [f"# {episode_title}", ""]
    if byline:
        output.extend([byline, ""])

    base_folded = _base_title(episode_title).casefold()
    topics: list[str] = []
    section_headings: list[str] = []
    for line in lines:
        stripped = line.strip()
        abstract = re.match(r"^Abstract\s*:\s*(.*)$", stripped, re.I)
        if abstract:
            output.extend(["## Abstract", "", abstract.group(1).strip()])
            counts["headings_normalized"] += 1
            continue
        heading = _HEADING_RE.match(stripped)
        if not heading:
            output.append(line)
            continue
        value = _plain(heading.group(2))
        folded = value.casefold()
        if folded == base_folded or folded == episode_title.casefold() or (
            folded.startswith("concept paper") and base_folded in folded
        ) or folded == "concept paper":
            counts["duplicate_headings_removed"] += 1
            continue
        numbered = _NUMBERED_HEADING_RE.match(value)
        if numbered:
            number, topic = numbered.groups()
            topic = _plain(topic)
            normalized = f"{number}. {topic}"
            output.append(f"## {normalized}")
            section_headings.append(normalized)
            if topic and topic not in topics and len(topics) < MAX_TOPICS:
                topics.append(topic)
        else:
            level = 2 if value.casefold() in {"abstract", "glossary", "references", "keywords"} else min(len(heading.group(1)), 3)
            output.append(f"{'#' * max(2, level)} {value}")
        counts["headings_normalized"] += 1

    cleaned = "\n".join(output)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip() + "\n"
    diagnostics = {"version": CLEANUP_VERSION, **counts, "topics_extracted": len(topics)}
    return FaithfulCleanup(cleaned, topics, section_headings, diagnostics)


def episode_description(title: str, topics: list[str]) -> str:
    lines = [f'Faithful read of “{title}”.']
    if topics:
        lines.extend(["", "Topics:", *(f"• {topic}" for topic in topics)])
    description = "\n".join(lines)
    if len(description) <= MAX_DESCRIPTION_CHARS:
        return description
    return description[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
