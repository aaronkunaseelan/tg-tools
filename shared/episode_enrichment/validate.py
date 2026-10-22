"""Deterministic gates over Luna output.

Two severities, and the split matters:

* **errors** are structural. They mean the model did not answer the question
  asked (coverage holes, mutated headings, unknown paragraph ids). The run
  fails and nothing is published.
* **notes** are per-item defects. The offending mention is dropped and the run
  continues. Measured on live traffic, ~4% of mentions on long content fail the
  evidence gate, and every observed failure was a genuine paraphrase, stitch,
  or silent correction (e.g. the model writing "Pope Leo XIV" where the
  transcript says "Pope Bob I"). Failing a whole episode over that would be
  wrong; keeping the mention would be worse.

Lengths (summary, heading) and topic counts are never gated - only reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .paragraphs import normalize
from .prompts import MENTION_TYPES, REFERENCED_AS_MAX_CHARS, SUMMARY_TARGET_CHARS

HEADING_SOFT_CHARS = 80
_PASSING = "mentioned in passing"


@dataclass
class GateReport:
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_json(self) -> dict:
        return {"ok": self.ok, "errors": self.errors,
                "notes": self.notes, "stats": self.stats}


def validate_topics(stream, analysis) -> GateReport:
    """Gate pass-1 output: coverage is the whole point."""
    report = GateReport()
    if not isinstance(analysis, dict):
        report.errors.append("pass-1 output is not a JSON object")
        return report

    summary = str(analysis.get("summary") or "").strip()
    if not summary:
        report.errors.append("summary is empty")

    topics = analysis.get("topics")
    if not isinstance(topics, list) or not topics:
        report.errors.append("pass-1 returned no topics")
        return report

    ids = stream.ids
    cursor = 0
    for index, topic in enumerate(topics):
        if not isinstance(topic, dict):
            report.errors.append(f"topic[{index}] is not an object")
            return report
        heading = str(topic.get("heading") or "").strip()
        if not heading:
            report.errors.append(f"topic[{index}] has an empty heading")
        elif len(heading) > HEADING_SOFT_CHARS:
            report.notes.append(
                f"topic[{index}] heading is {len(heading)} chars "
                f"(soft guide {HEADING_SOFT_CHARS})")

        start = topic.get("startParagraphId")
        end = topic.get("endParagraphId")
        start_pos = stream.position(str(start)) if start is not None else None
        end_pos = stream.position(str(end)) if end is not None else None
        if start_pos is None or end_pos is None:
            report.errors.append(
                f"topic[{index}] references unknown paragraphs {start!r}..{end!r}")
            return report
        if end_pos < start_pos:
            report.errors.append(
                f"topic[{index}] ends at {end} before it starts at {start}")
            return report
        if start_pos != cursor:
            expected = ids[cursor] if cursor < len(ids) else "<end of transcript>"
            report.errors.append(
                f"topic[{index}] starts at {start}, expected {expected} "
                "(gap or overlap in coverage)")
            return report
        cursor = end_pos + 1

    if cursor != len(ids):
        report.errors.append(
            f"topic coverage stops after {cursor} of {len(ids)} paragraphs")

    report.stats = {
        "topics": len(topics),
        "summaryChars": len(summary),
        "summaryTargetChars": SUMMARY_TARGET_CHARS,
    }
    if len(summary) > SUMMARY_TARGET_CHARS:
        report.notes.append(
            f"summary is {len(summary)} chars against a "
            f"{SUMMARY_TARGET_CHARS}-char target (not enforced)")
    return report


def _alias_of(kind: str, candidate: str, seen: dict) -> tuple[str, str] | None:
    """Return an already-seen key that ``candidate`` is an alias of.

    Deliberately narrow: **people only**, and only on whole-word containment.
    The observed cross-topic alias pairs were all bare personal names -
    "Dario" / "Dario Amadei", "Jefferson" / "Thomas Jefferson".

    Applying the same containment rule to other types destroys real distinct
    entities: "China" would swallow "South China Sea" and "East China Sea",
    and "Biden" would swallow "Biden administration". For every non-person
    type, only an exact name match counts as a duplicate.
    """
    if kind != "person":
        return None
    words = set(candidate.split())
    if not words:
        return None
    for other_kind, other_name in seen:
        if other_kind != "person":
            continue
        other_words = set(other_name.split())
        if words < other_words or other_words < words:
            return (other_kind, other_name)
    return None


def validate_mentions(stream, analysis, mentions_payload) -> tuple[dict, GateReport]:
    """Gate pass-2 output and return a cleaned copy alongside the report.

    Structural problems (mutated or reordered headings) are errors. Per-mention
    problems drop that mention and are recorded as notes.
    """
    report = GateReport()
    if not isinstance(mentions_payload, dict):
        report.errors.append("pass-2 output is not a JSON object")
        return {"topics": []}, report

    frozen = analysis["topics"]
    returned = mentions_payload.get("topics")
    if not isinstance(returned, list):
        report.errors.append("pass-2 returned no topics list")
        return {"topics": []}, report
    if len(returned) != len(frozen):
        report.errors.append(
            f"pass-2 changed the topic count: {len(frozen)} frozen, "
            f"{len(returned)} returned")
        return {"topics": []}, report

    cleaned: list[dict] = []
    kept = dropped = total = 0
    # Alias tracking is episode-wide so "Dario" in a later topic collapses
    # into "Dario Amadei" from an earlier one.
    seen_global: dict[str, str] = {}

    for index, (frozen_topic, returned_topic) in enumerate(zip(frozen, returned)):
        if not isinstance(returned_topic, dict):
            report.errors.append(f"pass-2 topic[{index}] is not an object")
            return {"topics": []}, report
        heading = str(returned_topic.get("heading") or "").strip()
        if heading != str(frozen_topic["heading"]).strip():
            report.errors.append(
                f"pass-2 topic[{index}] mutated the heading to {heading!r}")
            return {"topics": []}, report

        low = stream.position(frozen_topic["startParagraphId"])
        high = stream.position(frozen_topic["endParagraphId"])
        raw_mentions = returned_topic.get("mentions")
        if raw_mentions is None:
            raw_mentions = []
        if not isinstance(raw_mentions, list):
            report.errors.append(
                f"pass-2 topic[{index}] mentions is not a list")
            return {"topics": []}, report

        seen_local: dict[str, str] = {}
        keep: list[dict] = []
        for mention in raw_mentions:
            total += 1
            if not isinstance(mention, dict):
                dropped += 1
                report.notes.append(f"topic[{index}]: dropped non-object mention")
                continue

            display = str(mention.get("displayText") or "").strip()
            kind = str(mention.get("type") or "").strip()
            paragraph_id = str(mention.get("paragraphId") or "").strip()
            quote = str(mention.get("evidenceQuote") or "")
            referenced = str(mention.get("referencedAs") or "").strip()

            if not display:
                dropped += 1
                report.notes.append(f"topic[{index}]: dropped mention with no displayText")
                continue
            if kind not in MENTION_TYPES:
                dropped += 1
                report.notes.append(
                    f"topic[{index}]: dropped {display!r}, unknown type {kind!r}")
                continue

            position = stream.position(paragraph_id)
            if position is None:
                dropped += 1
                report.notes.append(
                    f"topic[{index}]: dropped {display!r}, unknown paragraph "
                    f"{paragraph_id!r}")
                continue
            if not (low <= position <= high):
                dropped += 1
                report.notes.append(
                    f"topic[{index}]: dropped {display!r}, paragraph "
                    f"{paragraph_id} outside span")
                continue

            # The evidence gate: proves the reference was spoken, not that any
            # claim about it is true.
            normalized_quote = normalize(quote)
            if not normalized_quote:
                dropped += 1
                report.notes.append(
                    f"topic[{index}]: dropped {display!r}, empty evidenceQuote")
                continue
            if normalized_quote not in normalize(stream.text_of(paragraph_id)):
                dropped += 1
                report.notes.append(
                    f"topic[{index}]: dropped {display!r}, evidenceQuote not "
                    f"found verbatim in {paragraph_id}")
                continue

            # Entities the episode says nothing about are almost always the
            # hosts; they add noise and no information.
            if referenced.lower().rstrip(".") == _PASSING:
                dropped += 1
                report.notes.append(
                    f"topic[{index}]: dropped {display!r}, only mentioned in passing")
                continue
            if len(referenced) > REFERENCED_AS_MAX_CHARS:
                report.notes.append(
                    f"topic[{index}]: {display!r} referencedAs is "
                    f"{len(referenced)} chars (soft cap {REFERENCED_AS_MAX_CHARS})")

            key = (kind, normalize(display))
            if key in seen_local:
                dropped += 1
                report.notes.append(
                    f"topic[{index}]: dropped duplicate {display!r}")
                continue
            alias = _alias_of(kind, key[1], seen_local)
            if alias is not None:
                dropped += 1
                report.notes.append(
                    f"topic[{index}]: dropped {display!r} as an alias of "
                    f"{seen_local[alias]!r}")
                continue

            canonical = display
            if key in seen_global:
                canonical = seen_global[key]
            else:
                global_alias = _alias_of(kind, key[1], seen_global)
                if global_alias is not None:
                    canonical = seen_global[global_alias]
                    # Prefer the longer, more specific form across the episode.
                    if len(display) > len(canonical):
                        canonical = display
                        seen_global[global_alias] = display
            seen_global.setdefault(key, canonical)
            seen_local[key] = canonical

            keep.append({
                "type": kind,
                "displayText": canonical,
                "referencedAs": referenced,
                "paragraphId": paragraph_id,
                "evidenceQuote": quote,
            })
            kept += 1

        cleaned.append({"heading": frozen_topic["heading"], "mentions": keep})

    report.stats = {
        "mentionsReturned": total,
        "mentionsKept": kept,
        "mentionsDropped": dropped,
        "keepRate": round(kept / total, 3) if total else None,
    }
    return {"topics": cleaned}, report
