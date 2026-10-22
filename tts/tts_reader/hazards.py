"""Deterministic speech hazards for generated Markdown."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

WORD_CAP = int(os.environ.get("TTS_X_WORD_CAP", "5000"))


@dataclass(frozen=True)
class SpeechHazard:
    kind: str
    quote: str
    reason: str


URL_RE = re.compile(r"https?://\S+")
FENCE_RE = re.compile(r"(?m)^\s*```")
TABLE_RE = re.compile(r"(?m)^\s*\|.+\|\s*$")
BULLET_RE = re.compile(r"(?m)^\s*[-*+]\s+\S.*$")
CITATION_RE = re.compile(r"\[\d+\]")


def _words(text: str) -> int:
    return len(re.findall(r"\b\w+(?:[-\u2019']\w+)*\b", text))


def speech_hazards(candidate: str, *, word_cap: int | None = None) -> tuple[SpeechHazard, ...]:
    hazards: list[SpeechHazard] = []
    patterns = (
        ("url", URL_RE, "raw URL would be spoken"),
        ("code_fence", FENCE_RE, "code fence would be spoken"),
        ("markdown_table", TABLE_RE, "Markdown table row would be spoken"),
        ("bullet", BULLET_RE, "bullet marker would be spoken"),
        ("citation_bracket", CITATION_RE, "citation bracket would be spoken"),
    )
    for kind, pattern, reason in patterns:
        seen: set[str] = set()
        for match in pattern.finditer(candidate):
            quote = match.group(0)
            if quote not in seen:
                hazards.append(SpeechHazard(kind, quote, reason))
                seen.add(quote)
    cap = WORD_CAP if word_cap is None else word_cap
    words = _words(candidate)
    if words > cap:
        hazards.append(SpeechHazard("word_cap", str(words),
                                    "script has %d words; cap is %d" % (words, cap)))
    return tuple(hazards)
