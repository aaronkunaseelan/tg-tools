from __future__ import annotations

import json
import os
import textwrap
import urllib.error
import urllib.request
from html import escape
from typing import Any

from config import Settings
from openai_auth import OpenAICodexAuth, OpenAIAuthError, load_openai_codex_auth

CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"


class SummaryError(RuntimeError):
    pass


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


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[: max_chars - 3].rstrip() + "...", True


def strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def read_sse_output_text(response) -> str:
    parts: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "response.output_text.delta" and event.get("delta"):
            parts.append(str(event["delta"]))
    text = "".join(parts).strip()
    if not text:
        raise SummaryError("empty openai-codex response")
    return text


def call_codex_json(
    auth: OpenAICodexAuth,
    *,
    model: str,
    instructions: str,
    prompt: str,
    timeout_sec: int,
) -> dict[str, Any]:
    body = json.dumps(
        {
            "model": model,
            "store": False,
            "stream": True,
            "instructions": instructions,
            "input": [{"role": "user", "content": prompt}],
            "text": {"verbosity": "medium"},
            "tool_choice": "none",
            "reasoning": {"effort": "medium", "summary": "auto"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        # The env override points summaries at a local responses endpoint (chain tests).
        os.environ.get("MEETING_SUMMARY_CODEX_RESPONSES_URL") or CODEX_RESPONSES_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {auth.token}",
            "chatgpt-account-id": auth.account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "meeting-summary",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            text = read_sse_output_text(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        raise SummaryError(f"openai-codex HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SummaryError(f"openai-codex request failed: {exc}") from exc
    cleaned = strip_json_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SummaryError(f"summary JSON parse failed: {cleaned[:500]}") from exc


def build_chunk_summary_prompt(chunk_index: int, total_chunks: int, transcript_text: str) -> str:
    content, truncated = truncate_text(transcript_text, 16000)
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
        Truncated: {str(truncated).lower()}
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
    settings: Settings,
    chunk_texts: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        auth = load_openai_codex_auth(settings.pi_auth_path)
    except OpenAIAuthError as exc:
        raise SummaryError(str(exc)) from exc
    chunk_summaries: list[dict[str, Any]] = []
    for index, chunk_text in enumerate(chunk_texts):
        chunk_summary = call_codex_json(
            auth,
            model=settings.summary_model,
            instructions="You summarize one meeting transcript chunk. Return strict JSON only.",
            prompt=build_chunk_summary_prompt(index, len(chunk_texts), chunk_text),
            timeout_sec=settings.summary_timeout_sec,
        )
        chunk_summaries.append(chunk_summary)

    final_summary = call_codex_json(
        auth,
        model=settings.summary_model,
        instructions="You synthesize one final meeting summary. Return strict JSON only.",
        prompt=build_final_summary_prompt(chunk_summaries),
        timeout_sec=settings.summary_timeout_sec,
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
