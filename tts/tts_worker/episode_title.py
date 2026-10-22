"""Episode title resolution for faithful reads.

The explain path gets a model-authored title; faithful reads historically fell
back to raw source metadata or the first line of the markdown, which produced
feed items like "---", "Process note. I converted the source ..." and
"<page> - Wikipedia". This module cleans those up: a small chat-model call
names the episode when TTS_TITLE_MODEL is set, and a hardened heuristic covers
the disabled/failure case.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib import request as urllib_request

from tts_worker.config import Settings
from .key_command import KeyCommandError, resolve_key_command

MAX_TITLE_CHARS = 120
_SITE_SUFFIX_RE = re.compile(r"\s*[-–—|]\s*(wikipedia|wikipedia, the free encyclopedia)\s*$", re.IGNORECASE)
_BOILERPLATE_PREFIXES = ("process note", "table of contents", "skip to content", "source:", "converted the source")
_BOILERPLATE_EXACT = {"figure", "concept paper", "mdpi", "entropy", "check for updates"}
_MARKUP_RE = re.compile(r"<!--|-->|<\/?[A-Za-z][^>]*>")
_EXCERPT_CHARS = 6000

_TITLE_PROMPT = (
    "You name podcast episodes. The episode is a faithful read-aloud of the "
    "source text below. Reply with JSON only: {\"title\": \"<title>\"}. The "
    "title must be a specific, human-readable episode name under 90 "
    "characters — no site names, no quotes, no 'Episode'/'Podcast' filler."
)


def _extract_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, str):
            return content
    return str(payload.get("output_text") or "")


def _strip_code_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and "\n" in value:
        value = value.split("\n", 1)[1]
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    return value.strip()


def _resolve_api_key(api_key: str, key_cmd: str) -> str:
    if api_key.strip():
        return api_key.strip()
    if not key_cmd.strip():
        raise RuntimeError("no title chat API key or key command configured")
    try:
        return resolve_key_command(key_cmd)
    except KeyCommandError as exc:
        raise RuntimeError(f"title chat key command failed: {exc}") from exc


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


def llm_title(source_text: str, hint: str, *, settings: Settings, http_post: Any | None = None) -> str:
    """Best-effort model-authored title; returns "" on any failure."""
    if not settings.title_model.strip():
        return ""
    excerpt = source_text[:_EXCERPT_CHARS]
    user = f"TITLE HINT (may be junk): {hint}\n\nSOURCE TEXT:\n{excerpt}"
    body = {
        "model": settings.title_model,
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": _TITLE_PROMPT},
            {"role": "user", "content": user},
        ],
    }
    try:
        if http_post is not None:
            payload = http_post(body, None)
        else:
            key = _resolve_api_key(settings.azure_chat_api_key, settings.azure_chat_key_cmd)
            req = urllib_request.Request(
                settings.azure_chat_base_url.rstrip("/") + "/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "api-key": key, "User-Agent": "tts-worker/1.0"},
                method="POST",
            )
            with urllib_request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        obj = json.loads(_strip_code_fence(_extract_text(payload)))
        return clean_title(obj.get("title") if isinstance(obj, dict) else "")
    except Exception:
        return ""


def resolve_title(source_md: Path, meta: dict[str, Any], *, settings: Settings, title_hint: Any = None, http_post: Any | None = None) -> str:
    explicit = clean_title(title_hint)
    if explicit:
        return explicit
    fallback = heuristic_title(source_md, meta)
    hint = str(meta.get("title") or "").strip() or fallback
    title = llm_title(source_md.read_text(encoding="utf-8"), hint, settings=settings, http_post=http_post)
    return title or fallback
