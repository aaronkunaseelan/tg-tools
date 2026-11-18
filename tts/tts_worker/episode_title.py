"""Episode title resolution for faithful reads.

The explain path gets a model-authored title; faithful reads historically fell
back to raw source metadata or the first line of the markdown, which produced
feed items like "---", "Process note. I converted the source ..." and
"<page> - Wikipedia". A faithful read now names its episode with one contained
harness turn on the tier the enrichment tier file gives the ``titles`` pipeline
(``pi_harness``; role ``episode_title``, X-104.3).

A title is never worth the episode: a failed call or an unusable reply raises
``TitleError`` with a named ``error_class``, and ``resolve_title`` publishes
under the deterministic ``heuristic_title`` instead, recording the class as a
title fallback the job result carries (C4 of the 7af619b..aa52235 review).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 120
TITLE_ROLE = "episode_title"
# One call over the source's first page; xhigh on a small model answers in seconds.
TITLE_TIMEOUT_S = 300
_SITE_SUFFIX_RE = re.compile(r"\s*[-–—|]\s*(wikipedia|wikipedia, the free encyclopedia)\s*$", re.IGNORECASE)
_BOILERPLATE_PREFIXES = ("process note", "table of contents", "skip to content", "source:", "converted the source")
_BOILERPLATE_EXACT = {"figure", "concept paper", "mdpi", "entropy", "check for updates"}
_MARKUP_RE = re.compile(r"<!--|-->|<\/?[A-Za-z][^>]*>")
_EXCERPT_CHARS = 6000


class TitleError(RuntimeError):
    """The title call failed; ``error_class`` is the job's failure class (tts_worker.failure_policy)."""

    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


def _strip_code_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and "\n" in value:
        value = value.split("\n", 1)[1]
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    return value.strip()


def parse_title_reply(text: str) -> str:
    """The reply's title, cleaned; an unusable reply is a named failure."""
    cleaned = _strip_code_fence(text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise TitleError("model_schema_invalid", "title reply was not JSON: %s" % cleaned[:200]) from exc
    if not isinstance(payload, dict):
        raise TitleError("model_schema_invalid", "title reply was not a JSON object: %s" % cleaned[:200])
    title = clean_title(payload.get("title"))
    if not title:
        raise TitleError("model_schema_invalid", "title reply held no usable title: %s" % cleaned[:200])
    return title


def clean_title(raw: Any) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip().strip("\"'").strip()
    if _MARKUP_RE.search(text):
        return ""
    text = _SITE_SUFFIX_RE.sub("", text)
    folded = text.casefold()
    if sum(1 for ch in text if ch.isalnum()) < 3:
        return ""
    if folded in _BOILERPLATE_EXACT or any(folded.startswith(prefix) for prefix in _BOILERPLATE_PREFIXES):
        return ""
    return text[:MAX_TITLE_CHARS].rstrip()


def heuristic_title(source_md: Path, meta: dict[str, Any]) -> str:
    """The deterministic title this module produced before X-104.3: metadata, then headings, then the first
    usable line. It never fails and never returns empty."""
    title = clean_title(meta.get("title"))
    if title:
        return title
    lines = source_md.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() == "title":
                title = clean_title(value)
                if title:
                    return title
    # Prefer authored headings over arbitrary OCR/layout lines.
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        title = clean_title(stripped.lstrip("#"))
        if title:
            return title
    for line in lines:
        stripped = line.strip()
        if stripped in {"---", "..."} or stripped.startswith("{{"):
            continue
        title = clean_title(stripped)
        if title:
            return title
    return "TTS episode"


def llm_title(source_text: str, hint: str, *, runner) -> str:
    """One harness call names the episode; failures raise ``TitleError``."""
    prompt = f"TITLE HINT (may be junk): {hint}\n\nSOURCE TEXT:\n{source_text[:_EXCERPT_CHARS]}"
    result = runner.run(prompt, allow_tools=False, timeout_s=TITLE_TIMEOUT_S,
                        session_name="episode-title")
    if not result.ok:
        raise TitleError("model_transient", "title call failed: %s" % result.error)
    return parse_title_reply(result.text)


def resolve_title(source_md: Path, meta: dict[str, Any], *, runner, title_hint: Any = None,
                  notes: list[str] | None = None) -> str:
    """An explicit title hint wins; otherwise one model call on ``runner`` names the episode.

    A failed or unusable reply is not fatal: the deterministic title covers it, and ``notes`` (when
    given) receives one ``titleFallback: <error_class>`` line for the job result. Faithful reads are
    admitted with one attempt, so a title failure that reached the job would lose the episode.
    """
    explicit = clean_title(title_hint)
    if explicit:
        return explicit
    fallback = heuristic_title(source_md, meta)
    hint = str(meta.get("title") or "").strip() or fallback
    try:
        return llm_title(source_md.read_text(encoding="utf-8"), hint, runner=runner)
    except TitleError as exc:
        logger.warning("episode title call failed (%s): %s; publishing as %r",
                       exc.error_class, exc, fallback)
        if notes is not None:
            notes.append("titleFallback: %s (%s)" % (exc.error_class, fallback))
        return fallback
