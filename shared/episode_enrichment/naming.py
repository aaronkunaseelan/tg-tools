"""Deterministic, metadata-led names for public episode artifacts."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

_GENERIC = frozenset({
    "a", "an", "and", "audio", "by", "clip", "discussion", "episode", "explained",
    "featuring", "for", "from", "full", "in", "interview", "live", "of", "official",
    "on", "part", "podcast", "show", "talk", "the", "to", "video", "with",
})
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?")


@dataclass(frozen=True)
class EpisodeNamingMetadata:
    title: str
    identity: str
    creators: tuple[str, ...] = ()
    channel: str = ""
    series: str = ""
    published_at: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class EpisodeNames:
    prefix: str
    audio_name: str
    script_name: str
    notes_name: str
    manifest_name: str


def _ascii(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return normalized.encode("ascii", "ignore").decode("ascii")


def _slug_words(value: str) -> list[str]:
    words = []
    for match in _TOKEN_RE.finditer(_ascii(value)):
        word = match.group(0).replace("’", "'").lower().strip("'-")
        if len(word) > 1 and word not in _GENERIC and not word.isdigit():
            words.append(word)
    return words


def _proper_phrases(value: str) -> list[str]:
    """Return title-case/acronym runs in source order.

    Metadata extractors often expose names only inside a title. This bounded
    heuristic prefers those names without adding another stochastic model call.
    """
    tokens = _TOKEN_RE.findall(_ascii(value))
    phrases: list[str] = []
    current: list[str] = []
    for token in tokens + [""]:
        bare = token.strip("'-")
        proper = bool(bare) and (
            bare.isupper() and len(bare) > 1
            or bare[:1].isupper() and bare[1:].islower()
            or any(char.isdigit() for char in bare)
        ) and bare.lower() not in _GENERIC
        if proper:
            current.append(bare)
        elif current:
            phrase = " ".join(current)
            if _slug_words(phrase):
                phrases.append(phrase)
            current = []
    return phrases


def _date_prefix(metadata: EpisodeNamingMetadata) -> str:
    if metadata.published_at:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", metadata.published_at)
        if match:
            return match.group(1)
    created = metadata.created_at
    if isinstance(created, date):
        return created.strftime("%Y-%m-%d")
    # Unknown is preferable to a retry-dependent ingestion date. Callers that
    # own a durable creation timestamp may pass it explicitly.
    return "undated"


def _identity_suffix(identity: str) -> str:
    cleaned = re.sub(r"[^0-9a-f]", "", (identity or "").lower())
    if len(cleaned) >= 8:
        return cleaned[:12]
    return hashlib.sha256((identity or "episode").encode("utf-8")).hexdigest()[:12]


def _unique_phrases(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        words = _slug_words(value)
        if not words:
            continue
        phrase = "-".join(words[:4])
        if phrase not in seen:
            seen.add(phrase)
            result.append(phrase)
    return result


def build_episode_names(metadata: EpisodeNamingMetadata) -> EpisodeNames:
    candidates = _unique_phrases([
        *metadata.creators,
        *_proper_phrases(metadata.title),
        metadata.series,
        metadata.channel,
    ])
    if not candidates:
        title_words = _slug_words(metadata.title)
        candidates = ["-".join(title_words[:6]) if title_words else "episode"]

    semantic = "-".join(candidates[:3])
    # Leave room for date, separators, and the identity suffix.
    semantic = semantic[:120].strip("-") or "episode"
    prefix = f"{_date_prefix(metadata)}-{semantic}-{_identity_suffix(metadata.identity)}"
    return names_from_prefix(prefix)


def names_from_prefix(prefix: str) -> EpisodeNames:
    """Derive the artifact name set from an already-decided prefix.

    Retries must reuse the prefix pinned by the first attempt's publish
    marker rather than recomputing it: the semantic part comes from an LLM
    title and the date part rolls over at midnight, so a recomputed prefix
    can disagree with the keys the publisher actually uploads under. Callers
    that hold a pinned slug build their names through this function so the
    in-document URLs and the uploaded keys share one source of truth.
    """
    prefix = (prefix or "").strip().strip("/") or "episode"
    return EpisodeNames(
        prefix=prefix,
        audio_name=f"{prefix}.mp3",
        script_name=f"{prefix}.script.md",
        notes_name=f"{prefix}.notes.md",
        manifest_name=f"{prefix}.manifest.json",
    )
