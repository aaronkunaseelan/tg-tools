"""Deterministic input classifier for TTS materialisation."""

from __future__ import annotations

import re
from typing import Any, Iterable

CHATGPT_SHARE_RE = re.compile(r"https?://chatgpt\.com/share/[0-9a-fA-F-]+")
URL_RE = re.compile(r"https?://\S+")


def _item_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("url") or item.get("path") or item.get("text") or item.get("source") or "")
    return str(item)


def _is_file(item: Any) -> bool:
    if isinstance(item, dict):
        if item.get("kind") == "file" or item.get("type") == "file":
            return True
        return bool(item.get("path") or item.get("file_path") or item.get("filename"))
    s = _item_text(item)
    return bool(s and not URL_RE.match(s) and ("/" in s or "." in s) and len(s) < 512)


def _part(item: Any, index: int) -> dict[str, Any]:
    text = _item_text(item)
    kind = "url" if URL_RE.search(text) else "file" if _is_file(item) else "text"
    if CHATGPT_SHARE_RE.search(text):
        kind = "chatgpt_share"
    return {"index": index, "kind": kind, "source": text, "item": item}


def classify_input(items: Iterable[Any]) -> dict[str, Any]:
    """Classify input as single_piece, conversation, or multipart_bundle."""
    seq = list(items if not isinstance(items, (str, bytes)) else [items])
    parts = [_part(item, i) for i, item in enumerate(seq)]
    joined = "\n".join(_item_text(i) for i in seq)
    if CHATGPT_SHARE_RE.search(joined):
        return {"input_class": "conversation", "parts": parts}
    url_count = sum(len(URL_RE.findall(_item_text(i))) for i in seq)
    file_count = sum(1 for i in seq if _is_file(i))
    if len(seq) > 1 or url_count > 1 or file_count > 1 or (url_count and file_count):
        return {"input_class": "multipart_bundle", "parts": parts}
    return {"input_class": "single_piece", "parts": parts}
