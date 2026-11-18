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

import re
from dataclasses import dataclass, field

from .paragraphs import normalize
from .prompts import MAIN_POINTS, MENTION_TYPES, REFERENCED_AS_MAX_CHARS, SUMMARY_TARGET_CHARS
from .speakers import surname

HEADING_SOFT_CHARS = 80
_PASSING = "mentioned in passing"

# The lede gate: a section cannot open with a pronoun the reader has no antecedent for.
LEDE_FIRST_BANNED = frozenset("they it their its he she this that these those".split())
# The reference gate: these pronouns need a noun in the heading or the lede to resolve against.
REF_PRONOUNS = frozenset("their its they it".split())
# Words that talk about the notes' own instructions instead of the content.
META_WORDS = ("roster", "guest list", "paragraph id", "paragraph range", "instructions")
# Small on purpose: only the words that carry no subject of their own.
STOPWORDS = frozenset("""
    a an the and or but if so than then that this these those there here
    i you we he she they it its their theirs our ours your yours his her him them us
    am is are was were be been being have has had do does did done
    of in on at to for from by with into over under about after before between during
    will would can could should may might must shall not no yes all any both each few more most
    other some such only own same very too also just even like much many any every
    what when where which while who whom whose why how
    says said say make makes made get gets got go goes went one two three
""".split())


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


def validate_topics(stream, analysis, *, sections: bool = False) -> GateReport:
    """Gate pass-1 output: coverage is the whole point.

    ``sections`` is the notes shape that asks each segment for a ``lede`` and the episode for its
    ``mainPoints`` (X-104): both are then part of the schema, so a payload without them is an answered
    question that the answer left out, and gets the pass's one repair like any other structural gap.
    """
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

    points = analysis.get("mainPoints") if sections else None
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
        if sections and not str(topic.get("lede") or "").strip():
            report.errors.append(
                f"topic[{index}] has no lede: every segment opens on one sentence that names the "
                "speaker and its claim")

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

    if sections:
        _main_points(stream, topics, points, report)

    report.stats = {
        "topics": len(topics),
        "summaryChars": len(summary),
        "summaryTargetChars": SUMMARY_TARGET_CHARS,
    }
    if sections:
        report.stats.update(
            ledes=sum(1 for topic in topics if str(topic.get("lede") or "").strip()),
            mainPoints=len(points or []), mainPointsAsked=MAIN_POINTS)
    if len(summary) > SUMMARY_TARGET_CHARS:
        report.notes.append(
            f"summary is {len(summary)} chars against a "
            f"{SUMMARY_TARGET_CHARS}-char target (not enforced)")
    return report


def _main_points(stream, topics, points, report: GateReport) -> None:
    """The episode's ranked points, in the schema: present, three to six, each on a real paragraph."""
    if not isinstance(points, list) or not points:
        report.errors.append(f"pass-1 returned no mainPoints: give {MAIN_POINTS} ranked points")
        return
    fewest = min(3, len(topics))
    if not fewest <= len(points) <= 6:
        report.errors.append(f"{len(points)} mainPoints: give {fewest} to 6")
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            report.errors.append(f"mainPoints[{index}] is not an object")
            continue
        if not str(point.get("text") or "").strip():
            report.errors.append(f"mainPoints[{index}] is empty")
        paragraph_id = str(point.get("paragraphId") or "")
        if stream.position(paragraph_id) is None:
            report.errors.append(f"mainPoints[{index}] cites {paragraph_id!r}, not a transcript paragraph")


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


# ---------------------------------------------------------------- the notes' own form gates (X-104)
#
# These read the returned payload and never ask again: a failing section is the report's, not a third
# model call. Surveillance runs the same helpers as errors inside its call loop (a block answer is cheap
# to repair and the show is the owner's notes surface); an ordinary episode, which has one topics call for
# the whole transcript, logs them.


def content_words(text: str) -> set[str]:
    """The content nouns of a sentence: 4+ letters, lowercased, minus the small stopword set."""
    return {word for word in re.findall(r"[A-Za-z]{4,}", str(text or "").lower()) if word not in STOPWORDS}


def sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", str(text or "").strip()) if sentence.strip()]


def lede_problem(lede: str) -> str | None:
    """Why a section's lede is unusable, or None. The gate and the report read this."""
    text = " ".join(str(lede or "").split())
    if not text:
        return "no lede: open the section with one sentence that names the speaker and its claim"
    words = text.split()
    if len(words) < 8:
        return f"its lede is {len(words)} words: write 12 to 30, and never less than 8"
    first = re.sub(r"^[^A-Za-z]+", "", words[0]).lower()
    if first in LEDE_FIRST_BANNED:
        return f"its lede opens with {words[0]!r}: name the speaker or the subject first, not a pronoun"
    return None


def unknown_refs(heading: str, lede: str, text: str) -> list[str]:
    """'their vaults'-style fragments in a sentence that share no content noun with heading + lede.

    A possessive or bare pronoun is only readable when the section has already named the thing: the reader
    needs "central banks" in the heading or the lede before a bullet can say "their vaults". A sentence under
    five words is skipped, and so is one with no pronoun at all.
    """
    known = content_words(f"{heading} {lede}")
    out = []
    for sentence in sentences(text):
        words = sentence.split()
        if len(words) < 5:
            continue
        tokens = [re.sub(r"^[^A-Za-z]+", "", word).lower() for word in words]
        hit = next((j for j, token in enumerate(tokens) if token in REF_PRONOUNS), None)
        if hit is None or content_words(sentence) & known:
            continue
        follow = re.sub(r"[^A-Za-z]+$", "", words[hit + 1]).lower() if hit + 1 < len(words) else ""
        out.append(f"{tokens[hit]} {follow}".strip())
    return out


def quote_words(mention: dict) -> object:
    """The words a quote will have: the section path's count, or the episode path's evidence quote."""
    words = (mention.get("quote") or {}).get("words")
    if isinstance(words, int) and not isinstance(words, bool):
        return words
    evidence = str(mention.get("evidenceQuote") or "").strip()
    return len(evidence.split()) if evidence else None


def notes_gates(analysis: dict, mentions: dict, *, speakers: list | tuple = (),
                limit: int = 40) -> GateReport:
    """The notes' form, over the payload both calls returned: notes only, so it never costs a call.

    What it reports: a section with no lede or one that opens on a pronoun; a lede or reference sentence
    whose pronoun resolves to nothing the heading or lede names; a quote outside 3-15 words; a sentence
    about the notes' own instructions; and — when the episode's metadata names a speaker — a section whose
    lede and first reference never name them. ``limit`` caps the notes kept; the counts are always whole.
    """
    report = GateReport()
    owners = {surname(name) for name in speakers if surname(name)}
    by_heading = {normalize(topic.get("heading", "")): topic.get("mentions") or []
                  for topic in (mentions or {}).get("topics") or []}
    counts = {"sections": 0, "ledesMissing": 0, "referenceErrors": 0, "quotesOutOfRange": 0,
              "metaWords": 0, "topicsWithoutName": 0}
    for index, topic in enumerate(analysis.get("topics") or []):
        counts["sections"] += 1
        heading = str(topic.get("heading") or "").strip()
        lede = str(topic.get("lede") or "").strip()
        found = by_heading.get(normalize(heading), [])
        label = f"section[{index}] ({heading!r})" if heading else f"section[{index}]"
        problem = lede_problem(lede)
        if problem:
            counts["ledesMissing"] += 1
            report.notes.append(f"{label}: {problem}")
        first_reference = str(found[0].get("referencedAs") or "").strip() if found else ""
        texts = [lede, first_reference] if first_reference else [lede]
        for text in texts:
            for fragment in unknown_refs(heading, lede, text):
                counts["referenceErrors"] += 1
                report.notes.append(f"{label}: '{fragment}' refers to something the section never names")
            lowered = text.lower()
            for word in META_WORDS:
                if word in lowered:
                    counts["metaWords"] += 1
                    report.notes.append(f"{label}: mentions its own instructions ({word!r})")
        for mention in found:
            words = quote_words(mention)
            name = str(mention.get("displayText") or "?").strip()
            if not isinstance(words, int) or isinstance(words, bool) or not 3 <= words <= 15:
                counts["quotesOutOfRange"] += 1
                report.notes.append(f"{label}: the quote for {name!r} is {words!r} words, not 3 to 15")
        if owners:
            named = f"{lede} {first_reference}"
            if not any(sur in named for sur in owners):
                counts["topicsWithoutName"] += 1
                report.notes.append(
                    f"{label}: neither its lede nor its first reference names the speaker "
                    f"({', '.join(sorted(owners))})")
    report.stats = counts
    if len(report.notes) > limit:
        report.stats["notesOmitted"] = len(report.notes) - limit
        report.notes = report.notes[:limit] + [f"… and {report.stats['notesOmitted']} more"]
    return report
