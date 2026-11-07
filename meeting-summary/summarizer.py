"""Meeting summary calls: one contained harness turn per chunk, one for the final summary.

The model comes from the enrichment tier file's ``meetings`` pipeline (``pi_harness`` role
``meeting_summary``), read once at the start of a job: there is no second client and no
fallback model. A call that fails, or a reply that is not the strict JSON the prompt asks
for, raises ``SummaryError`` with a named ``error_class`` (``model_transient``,
``model_schema_invalid``) that the worker turns into a job error, so the job fails and
retries instead of quietly producing a note without a summary.
"""

from __future__ import annotations

import json
import textwrap
from html import escape
from typing import Any

from pi_harness import PiRunner

SUMMARY_ROLE = "meeting_summary"


class SummaryError(RuntimeError):
    """A summary call, or the input it was given, failed; ``error_class`` names it for the job."""

    error_class = "model_transient"

    def __init__(self, message: str, error_class: str | None = None) -> None:
        super().__init__(message)
        if error_class:
            self.error_class = error_class


MISSING_INFORMATION_RULES = (
    "Use only the provided transcript text or chunk summaries; do not use outside knowledge, tools, API calls, or guesses.",
    "Do not invent attendees, owners, deadlines, decisions, or action items.",
    "Gracefully handle missing information: if attendees or other fields are not supported by the source text, return an empty array or empty string as appropriate.",
    "If an action item is clear but the owner or deadline is unknown, include the action without fabricating an owner or deadline.",
)


def render_missing_information_rules() -> str:
    return "\n".join(f"- {rule}" for rule in MISSING_INFORMATION_RULES)


def indented_missing_information_rules() -> str:
    return textwrap.indent(render_missing_information_rules(), "        ")


MISSING_INFORMATION_RULE_BLOCK = indented_missing_information_rules()


def _prompt_rule_text() -> str:
    return MISSING_INFORMATION_RULE_BLOCK


def strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def parse_summary_json(text: str) -> dict[str, Any]:
    """One summary reply as an object; anything else is a named failure."""
    cleaned = strip_json_fences(text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SummaryError(f"summary JSON parse failed: {cleaned[:500]}", "model_schema_invalid") from exc
    if not isinstance(payload, dict):
        raise SummaryError(f"summary reply was not a JSON object: {cleaned[:500]}", "model_schema_invalid")
    return payload


def call_summary_json(runner: PiRunner, prompt: str, *, timeout_sec: int, session_name: str) -> dict[str, Any]:
    result = runner.run(prompt, allow_tools=False, timeout_s=timeout_sec, session_name=session_name)
    if not result.ok:
        raise SummaryError(f"summary call failed: {result.error}")
    return parse_summary_json(result.text)


def build_chunk_summary_prompt(chunk_index: int, total_chunks: int, transcript_text: str) -> str:
    content = transcript_text
    missing_information_rules = _prompt_rule_text()
    return textwrap.dedent(
        f"""
        Summarize this meeting transcript chunk as strict JSON with keys:
        title_hint, meeting_date_hint, overview, attendees, key_points, decisions, action_items, open_questions

        Rules:
        - Use only the transcript text below.
        - meeting_date_hint must be YYYY-MM-DD when explicitly supported by the transcript, otherwise an empty string.
        - attendees, key_points, decisions, action_items, open_questions must be arrays of strings.
        - Keep each string concise.
{missing_information_rules}
        - Output JSON only.

        Chunk: {chunk_index + 1}/{total_chunks}
        Transcript:
        {content}
        """
    ).strip()


def build_final_summary_prompt(chunk_summaries: list[dict[str, Any]]) -> str:
    serialized = json.dumps(chunk_summaries, ensure_ascii=True)
    missing_information_rules = _prompt_rule_text()
    return textwrap.dedent(
        f"""
        Produce one final meeting summary as strict JSON with keys:
        title, meeting_date, overview, attendees, key_points, decisions, action_items, open_questions

        Rules:
        - Use only the chunk summaries below.
        - meeting_date must be YYYY-MM-DD when explicitly supported by the chunk summaries, otherwise an empty string.
        - attendees, key_points, decisions, action_items, open_questions must be arrays of strings.
        - title should be short and specific.
        - overview should be 2-4 sentences in one string.
{missing_information_rules}
        - Output JSON only.

        Chunk summaries:
        {serialized}
        """
    ).strip()


def summarize_transcript_chunks(
    *,
    settings: Any,
    chunk_texts: list[str],
    runner: PiRunner,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Partition without discarding any characters; every part gets a summary.
    chunk_texts = [text[start:start + 16000] for text in chunk_texts
                   for start in range(0, len(text), 16000)]
    if not chunk_texts:
        raise SummaryError("empty meeting transcript", "input_empty")
    timeout_sec = settings.summary_timeout_sec
    chunk_summaries: list[dict[str, Any]] = []
    for index, chunk_text in enumerate(chunk_texts):
        chunk_summaries.append(call_summary_json(
            runner,
            build_chunk_summary_prompt(index, len(chunk_texts), chunk_text),
            timeout_sec=timeout_sec,
            session_name=f"meeting-chunk-{index + 1}",
        ))

    final_summary = call_summary_json(
        runner,
        build_final_summary_prompt(chunk_summaries),
        timeout_sec=timeout_sec,
        session_name="meeting-final",
    )
    return chunk_summaries, final_summary


def render_summary_html(summary: dict[str, Any]) -> str:
    title = escape(str(summary.get("title") or "Meeting Summary"))
    overview = escape(str(summary.get("overview") or ""))
    lines = [f"<b>{title}</b>"]
    if overview:
        lines.append(overview)
    sections = [
        ("Attendees", summary.get("attendees") or []),
        ("Key Points", summary.get("key_points") or []),
        ("Decisions", summary.get("decisions") or []),
        ("Action Items", summary.get("action_items") or []),
        ("Open Questions", summary.get("open_questions") or []),
    ]
    for heading, items in sections:
        safe_items = [escape(str(item)) for item in items if str(item).strip()]
        if not safe_items:
            continue
        lines.append(f"<b>{escape(heading)}</b>")
        lines.extend(f"- {item}" for item in safe_items[:8])
    text = "\n".join(lines).strip()
    if len(text) <= 4000:
        return text
    truncated_lines = [lines[0], lines[1] if len(lines) > 1 else ""]
    for heading, items in sections:
        safe_items = [escape(str(item)) for item in items if str(item).strip()]
        if not safe_items:
            continue
        candidate = truncated_lines + [f"<b>{escape(heading)}</b>"] + [f"- {item}" for item in safe_items[:4]]
        if len("\n".join(candidate)) > 4000:
            break
        truncated_lines = candidate
    return "\n".join(line for line in truncated_lines if line).strip()
