from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

METADATA_FIELDS = ("meeting_date", "title", "attendees")
FIELD_LABELS = {
    "date": "meeting_date",
    "meeting date": "meeting_date",
    "title": "title",
    "attendees": "attendees",
}
NUMBERED_FIELDS = {
    "1": "meeting_date",
    "2": "title",
    "3": "attendees",
}
CANONICAL_LABELS = {
    "meeting_date": "Date",
    "title": "Title",
    "attendees": "Attendees",
}


class MetadataParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCallback:
    token: str
    action: str
    version: int


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    first_newline = stripped.find("\n")
    if first_newline < 0:
        return stripped.strip("`").strip()
    body = stripped[first_newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def normalize_date_text(raw: Any, *, allow_empty: bool = True) -> str:
    text = str(raw or "").strip()
    if not text:
        if allow_empty:
            return "unknown"
        raise MetadataParseError("Date must be YYYY-MM-DD or unknown.")
    if text.casefold() == "unknown":
        return "unknown"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise MetadataParseError("Date must be YYYY-MM-DD or unknown.") from exc


def date_column_value(meeting_date: str | None) -> str | None:
    text = normalize_date_text(meeting_date)
    return None if text == "unknown" else text


def normalize_attendees(value: Any) -> list[str]:
    if value is None:
        values: list[str] = []
    elif isinstance(value, list):
        values = [str(item).strip() for item in value]
    else:
        reader = csv.reader([str(value)], skipinitialspace=True)
        values = [part.strip() for part in next(reader, [])]

    seen: set[str] = set()
    normalized: list[str] = []
    for item in values:
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return normalized


def normalize_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    if not title:
        raise MetadataParseError("Title must be non-empty.")
    return {
        "meeting_date": normalize_date_text(raw.get("meeting_date")),
        "title": title,
        "attendees": normalize_attendees(raw.get("attendees")),
    }


def initial_review_metadata(
    summary: dict[str, Any] | None,
    *,
    fallback_date: datetime,
) -> tuple[dict[str, Any], str]:
    summary_json = summary if isinstance(summary, dict) else {}
    title = str(summary_json.get("title") or "Meeting Summary").strip() or "Meeting Summary"
    date_source = "model"
    try:
        meeting_date = normalize_date_text(summary_json.get("meeting_date"), allow_empty=True)
    except MetadataParseError:
        meeting_date = "unknown"
    if meeting_date == "unknown":
        meeting_date = fallback_date.date().isoformat()
        date_source = "fallback_received_at"
    metadata = normalize_metadata(
        {
            "meeting_date": meeting_date,
            "title": title,
            "attendees": summary_json.get("attendees") or [],
        }
    )
    return metadata, date_source


def apply_metadata_edit(current: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    merged = {
        "meeting_date": current.get("meeting_date") or "unknown",
        "title": current.get("title") or "",
        "attendees": list(current.get("attendees") or []),
    }
    merged.update(edits)
    return normalize_metadata(merged)


def _parse_labeled_line(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([A-Za-z ]+)\s*:\s*(.*)$", line)
    if not match:
        return None
    label = match.group(1).strip().casefold()
    field = FIELD_LABELS.get(label)
    if not field:
        raise MetadataParseError(f"Unknown metadata field: {match.group(1).strip()}.")
    return field, match.group(2).strip()


def _parse_numbered_line(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([123])(?:\s*[:.)]\s*|\s+)(.*)$", line)
    if not match:
        return None
    field = NUMBERED_FIELDS[match.group(1)]
    value = match.group(2).strip()
    labeled = _parse_labeled_line(value)
    if labeled is not None:
        labeled_field, labeled_value = labeled
        if labeled_field != field:
            expected = CANONICAL_LABELS[field]
            actual = CANONICAL_LABELS[labeled_field]
            raise MetadataParseError(f"Line {match.group(1)} is {expected}, not {actual}.")
        return field, labeled_value
    return field, value


def _normalize_field_value(field: str, value: str) -> Any:
    if field == "meeting_date":
        return normalize_date_text(value)
    if field == "title":
        title = value.strip()
        if not title:
            raise MetadataParseError("Title must be non-empty.")
        return title
    if field == "attendees":
        return normalize_attendees(value)
    raise MetadataParseError(f"Unknown metadata field: {field}.")


def parse_metadata_edit_block(text: str) -> dict[str, Any]:
    body = strip_code_fences(text)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        raise MetadataParseError("Send Date, Title, or Attendees lines.")

    parsed: dict[str, Any] = {}
    for line in lines:
        item = _parse_numbered_line(line)
        if item is None:
            item = _parse_labeled_line(line)
        if item is None:
            raise MetadataParseError("Use Date:, Title:, Attendees:, or line numbers 1-3.")
        field, raw_value = item
        value = _normalize_field_value(field, raw_value)
        if field in parsed and parsed[field] != value:
            raise MetadataParseError(f"Conflicting edits for {CANONICAL_LABELS[field]}.")
        parsed[field] = value
    return parsed


def classify_review_reply(text: str) -> Literal["metadata", "append", "invalid_short"]:
    body = strip_code_fences(text)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return "invalid_short"
    first = lines[0]
    if re.match(r"^\s*(date|title|attendees)\s*:", first, re.IGNORECASE):
        return "metadata"
    if re.match(r"^\s*[123](?:\s*[:.)]\s*|\s+)", first):
        return "metadata"
    if len(body.strip()) > 10:
        return "append"
    return "invalid_short"


def render_attendees(attendees: list[str] | tuple[str, ...] | None) -> str:
    values = [str(item).strip() for item in attendees or [] if str(item).strip()]
    return ", ".join(values) if values else "(none)"


def render_metadata_review_text(
    metadata: dict[str, Any],
    *,
    accepted: bool = False,
) -> str:
    heading = "Accepted meeting metadata" if accepted else "Review meeting metadata"
    normalized = normalize_metadata(metadata)
    return "\n".join(
        [
            heading,
            "",
            f"1. Date: {normalized['meeting_date']}",
            f"2. Title: {normalized['title']}",
            f"3. Attendees: {render_attendees(normalized['attendees'])}",
        ]
    )


def build_accept_callback_data(token: str, version: int) -> str:
    data = f"ms:{token}:a:{int(version)}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("callback data exceeds Telegram 64-byte limit")
    return data


def parse_accept_callback_data(data: str) -> ParsedCallback:
    match = re.match(r"^ms:([A-Za-z0-9_-]{6,24}):([a-z]):([0-9]{1,10})$", data or "")
    if not match:
        raise ValueError("invalid callback data")
    return ParsedCallback(token=match.group(1), action=match.group(2), version=int(match.group(3)))


def preview_filename(metadata: dict[str, Any], version: int, *, final: bool = False) -> str:
    normalized = normalize_metadata(metadata)
    date_part = normalized["meeting_date"] if normalized["meeting_date"] != "unknown" else "unknown-date"
    suffix = "final" if final else "preview"
    return f"meeting-summary-{date_part}-v{int(version)}.{suffix}.md"


def _front_matter_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _list_items(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    rendered: list[str] = []
    for item in items:
        if isinstance(item, dict):
            description = str(item.get("description") or item.get("text") or "").strip()
            extras = []
            owner = str(item.get("owner") or "").strip()
            deadline = str(item.get("deadline") or "").strip()
            if owner:
                extras.append(f"owner: {owner}")
            if deadline:
                extras.append(f"deadline: {deadline}")
            if description and extras:
                rendered.append(f"{description} ({'; '.join(extras)})")
            elif description:
                rendered.append(description)
        else:
            text = str(item or "").strip()
            if text:
                rendered.append(text)
    return rendered


def _append_bullets(lines: list[str], heading: str, items: Any) -> None:
    values = _list_items(items)
    if not values:
        return
    lines.extend(["", f"## {heading}", ""])
    lines.extend(f"- {value}" for value in values)


def _fenced_text(text: str) -> str:
    fence = "```"
    if "```" in text:
        fence = "````"
    return f"{fence}\n{text.rstrip()}\n{fence}"


def render_meeting_markdown(
    *,
    metadata: dict[str, Any],
    notes_json: dict[str, Any],
    appends: list[dict[str, Any]] | None = None,
    review_status: str,
    review_version: int,
) -> str:
    normalized = normalize_metadata(metadata)
    lines: list[str] = [
        "---",
        f"meeting_date: {_front_matter_scalar(normalized['meeting_date'])}",
        f"title: {_front_matter_scalar(normalized['title'])}",
        "attendees:",
    ]
    for attendee in normalized["attendees"]:
        lines.append(f"  - {_front_matter_scalar(attendee)}")
    lines.extend(
        [
            f"review_status: {_front_matter_scalar(review_status)}",
            f"review_version: {int(review_version)}",
            "---",
            "",
            f"# {normalized['title']}",
            "",
            "## Metadata",
            "",
            f"- Date: {normalized['meeting_date']}",
            f"- Title: {normalized['title']}",
            f"- Attendees: {render_attendees(normalized['attendees'])}",
        ]
    )

    overview = str(notes_json.get("overview") or "").strip()
    if overview:
        lines.extend(["", "## Overview", "", overview])
    _append_bullets(lines, "Key Points", notes_json.get("key_points"))
    _append_bullets(lines, "Decisions", notes_json.get("decisions"))
    _append_bullets(lines, "Action Items", notes_json.get("action_items"))
    _append_bullets(lines, "Open Questions", notes_json.get("open_questions"))

    append_rows = sorted(appends or [], key=lambda row: int(row.get("append_version") or 0))
    if append_rows:
        lines.extend(["", "## Appended Notes"])
        for row in append_rows:
            created_at = str(row.get("created_at") or "").strip() or "unknown time"
            note_text = str(row.get("note_text") or "").strip()
            lines.extend(["", f"### {created_at}", "", _fenced_text(note_text)])

    lines.extend(["", "## Provenance", "", "- Generated by meeting-summary."])
    return "\n".join(lines).rstrip() + "\n"
