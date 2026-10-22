"""Production adapter: runs reader v4 and shapes its result for the worker."""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

from .errors import ExplainError
from .v4 import run_reader


class ExplainResult:
    def __init__(self, *, title: str, script_markdown: str, speech_markdown: str,
                 model_output: dict[str, Any], process_meta: dict[str, Any],
                 usage: dict[str, Any], hangups: list[str]):
        self.title = title
        self.script_markdown = script_markdown
        self.speech_markdown = speech_markdown
        self.model_output = model_output
        self.process_meta = process_meta
        self.usage = usage
        self.hangups = hangups


_HEADING = re.compile(r"^## [^\r\n]+$")
_URL = re.compile(r"https?://\S+")
_FENCE = re.compile(r"(?ms)^\s*```.*?^\s*```\s*$")
_TABLE = re.compile(r"(?m)^\s*\|.+\|\s*$")
_BULLET = re.compile(r"(?m)^(\s*)[-*+]\s+")
_CITATION = re.compile(r"\[\d+\]")


def render_explain_speech(script_markdown: str) -> str:
    """Remove only structural section headings and neutralize speech hazards."""
    lines = [line for line in script_markdown.splitlines()
             if not _HEADING.fullmatch(line)]
    text = "\n".join(lines)
    text = _FENCE.sub("\ncode omitted\n", text)
    text = _TABLE.sub("table omitted", text)
    text = _URL.sub("link omitted", text)
    text = _BULLET.sub(r"\1", text)
    text = _CITATION.sub("citation omitted", text)
    return text.rstrip() + "\n"


def compile_explained_artifact_pi(
    source_md_path: Path,
    meta: dict[str, Any],
    out_path: Path,
    *,
    input_json: dict[str, Any],
    work_dir: Path,
    cancel: Any | None = None,
) -> ExplainResult:
    """Reader v4: write, review, and at most one revision (see v4.py)."""
    source_md = source_md_path.read_text(encoding="utf-8")
    if not source_md.strip():
        raise ExplainError("extract_metadata_only", "empty extracted source")
    max_source_chars = int(os.environ.get("TTS_X_MAX_SOURCE_CHARS", "300000"))
    if len(source_md) > max_source_chars:
        raise ExplainError("source_too_large_for_reader",
                           f"source is {len(source_md)} chars over the reader cap")

    started = time.monotonic()
    result = run_reader(source_md, work_dir, cancel)
    script_markdown = result.script.rstrip() + "\n"
    speech_markdown = render_explain_speech(script_markdown)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(speech_markdown, encoding="utf-8")

    note_hangups = [str(w) for w in (meta.get("warnings") or [])]
    report = dict(result.report)
    report["elapsedSeconds"] = round(time.monotonic() - started, 3)
    process_meta = {
        "schemaVersion": 4,
        "sourceKey": input_json.get("sourceKey"),
        "telemetry": report,
        "repairs": int(report["revised"]),
        "evidenceDigests": {"sourceMarkdown": hashlib.sha256(source_md.encode("utf-8")).hexdigest()},
        "reviewFlags": result.review_flags,
        "terminalState": result.terminal_state,
        "hangups": note_hangups + result.review_flags,
    }
    model_output = {
        "title": result.title,
        "source_summary": result.synopsis,
        "authors": [],
        "process_hangups": note_hangups,
        "script_markdown": script_markdown,
        "source_claims": [],
        "open_questions": [],
        "quality_flags": result.review_flags,
        "terminal_state": result.terminal_state,
        "rulings": report["rulings"],
    }
    return ExplainResult(title=result.title, script_markdown=script_markdown,
                         speech_markdown=speech_markdown, model_output=model_output,
                         process_meta=process_meta, usage=report["usage"],
                         hangups=note_hangups + result.review_flags)
