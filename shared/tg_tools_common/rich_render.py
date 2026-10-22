from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Literal

MAX_RICH_UTF8_CHARS = 32768
MAX_RICH_BLOCKS = 500
MAX_RICH_NESTING = 16
MAX_RICH_MEDIA = 50
MAX_RICH_TABLE_COLUMNS = 20
HTML_CHUNK_LIMIT = 3400

MEDIA_RE = re.compile(r"!\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")


class RichLimitError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RichCapability:
    final_rich_supported: bool | None = None
    draft_rich_supported: bool | None = None


@dataclass(frozen=True, slots=True)
class RenderedDelivery:
    delivery_kind: Literal["telegram_rich_message", "telegram_text", "telegram_document"]
    rich_message: dict[str, Any] | None = None
    html_chunks: tuple[str, ...] = ()
    document_text: str | None = None
    fallback_reason: str | None = None


def render_rich_text(text: str, capability: RichCapability) -> RenderedDelivery:
    body = text if text.strip() else "(no output)"
    if capability.final_rich_supported:
        try:
            rich_message = build_rich_markdown_message(body)
        except RichLimitError as exc:
            return _fallback(body, reason=str(exc))
        return RenderedDelivery(delivery_kind="telegram_rich_message", rich_message=rich_message)
    return _fallback(body, reason="rich_not_supported")


def build_rich_markdown_message(text: str) -> dict[str, Any]:
    validate_rich_markdown(text)
    return {"markdown": text, "skip_entity_detection": False}


def validate_rich_markdown(text: str) -> None:
    if len(text.encode("utf-8")) > MAX_RICH_UTF8_CHARS:
        raise RichLimitError("rich_utf8_limit")
    if _estimate_blocks(text) > MAX_RICH_BLOCKS:
        raise RichLimitError("rich_block_limit")
    if _estimate_nesting(text) > MAX_RICH_NESTING:
        raise RichLimitError("rich_nesting_limit")
    media_targets = [match.group("target") for match in MEDIA_RE.finditer(text)]
    if len(media_targets) > MAX_RICH_MEDIA:
        raise RichLimitError("rich_media_limit")
    if any(not target.startswith(("http://", "https://")) for target in media_targets):
        raise RichLimitError("rich_media_requires_http")
    if _max_table_columns(text) > MAX_RICH_TABLE_COLUMNS:
        raise RichLimitError("rich_table_column_limit")


def _estimate_blocks(text: str) -> int:
    count = 0
    in_paragraph = False
    in_code = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            count += 1
            in_code = not in_code
            in_paragraph = False
            continue
        if in_code:
            continue
        if not stripped:
            in_paragraph = False
            continue
        if stripped.startswith(("#", "-", "*", "+", ">", "|", "1.", "1)")):
            count += 1
            in_paragraph = False
        elif not in_paragraph:
            count += 1
            in_paragraph = True
    return max(1, count)


def _estimate_nesting(text: str) -> int:
    depth = 0
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        leading_spaces = len(line) - len(stripped)
        list_depth = leading_spaces // 2
        quote_depth = len(stripped) - len(stripped.lstrip(">"))
        depth = max(depth, list_depth + quote_depth)
    return depth


def _max_table_columns(text: str) -> int:
    max_columns = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell for cell in stripped.strip("|").split("|")]
        max_columns = max(max_columns, len(cells))
    return max_columns


def _fallback(text: str, *, reason: str) -> RenderedDelivery:
    chunks = _html_chunks(text)
    if len(chunks) > 8 or reason == "rich_utf8_limit":
        return RenderedDelivery(
            delivery_kind="telegram_document",
            document_text=text,
            fallback_reason=reason,
        )
    return RenderedDelivery(
        delivery_kind="telegram_text",
        html_chunks=chunks,
        fallback_reason=reason,
    )


def _html_chunks(text: str) -> tuple[str, ...]:
    escaped = html.escape(text)
    if len(escaped) <= HTML_CHUNK_LIMIT:
        return (escaped,)
    chunks: list[str] = []
    start = 0
    while start < len(escaped):
        end = min(len(escaped), start + HTML_CHUNK_LIMIT)
        split_at = escaped.rfind("\n", start, end)
        if split_at > start + HTML_CHUNK_LIMIT // 2:
            end = split_at + 1
        chunks.append(escaped[start:end])
        start = end
    return tuple(chunks)
