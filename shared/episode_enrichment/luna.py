"""Luna calls for the two enrichment passes.

Thin wrapper over ``pi_harness.PiRunner``, which already provides the
containment this needs: pruned env, ``--no-tools --no-extensions --no-skills
--no-prompt-templates --no-context-files``, process-group kill on timeout, and
capped logs. The transcript is untrusted input, so no tool access is ever
requested.

One bounded repair call is permitted per pass. It receives only the validation
errors and the previous response - never new transcript content - and it is
never retried a second time.
"""
from __future__ import annotations

import json
import logging
import re

from . import prompts
from .validate import GateReport, validate_mentions, validate_topics

logger = logging.getLogger(__name__)

TOPICS_ROLE = "transcript_topics"
MENTIONS_ROLE = "transcript_mentions"
# Calibrated worst case (1h43m episode): pass 1 108s, pass 2 448s. Headroom
# for a slower model day, but bounded so a hung call cannot hold a job lease.
TOPICS_TIMEOUT_S = 1800  # a 3 h Surveillance transcript (~36k tokens) on Sonnet timed out at 600 s (4 Nov)
MENTIONS_TIMEOUT_S = 1800
# Pass 2 runs in batches of at most this many consecutive frozen topics (pipeline._stage_mentions); at or
# below it, one call. Evidence, Haiku on 3 h Surveillance hours (4 Nov, stored manifests): every call with
# 30-33 topics passed on its first try (3 of 3); with 34-51, 5 of 8 needed the repair call and 2 failed
# after it (47 frozen, 48 returned; 50 frozen, 26 returned). 33 is the largest count that never needed one.
MENTIONS_BATCH_TOPICS = 33

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class EnrichmentModelError(RuntimeError):
    """Raised when a pass cannot produce valid JSON within its repair budget."""

    def __init__(self, stage: str, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.stage = stage
        self.errors = errors or []


def extract_json(text: str) -> dict:
    """Parse one JSON object out of a model response.

    The prompts forbid fences and commentary, and live traffic complied every
    time, but a stray fence or trailing sentence must not fail an otherwise
    good episode.
    """
    candidate = _FENCE_RE.sub("", (text or "").strip())
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        raise EnrichmentModelError("parse", "model response contained no JSON object")
    try:
        return json.loads(candidate[start:end + 1])
    except json.JSONDecodeError as exc:
        raise EnrichmentModelError("parse", f"model response was not valid JSON: {exc}") from exc


def _run_pass(runner, prompt: str, *, stage: str, timeout_s: int,
              session_name: str, gate) -> tuple[dict, GateReport, str]:
    """Run one pass, validate, and allow exactly one bounded repair."""
    result = runner.run(prompt, allow_tools=False, timeout_s=timeout_s,
                        session_name=session_name)
    if not result.ok:
        raise EnrichmentModelError(stage, f"{stage} call failed: {result.error}")

    try:
        payload = extract_json(result.text)
        cleaned, report = gate(payload)
    except EnrichmentModelError as exc:
        payload, report = None, GateReport(errors=[str(exc)])
        cleaned = None

    if report.ok:
        return cleaned, report, result.text

    logger.warning("%s failed validation, attempting one repair: %s",
                   stage, "; ".join(report.errors[:5]))
    # Each run is a fresh session, so the repair restates the original task:
    # without it the model cannot restore anything it left out (x-tts job
    # f14962c3: "the source document is not in this context").
    repair = runner.run(
        prompt + "\n\n" + prompts.repair_prompt(report.errors, result.text),
        allow_tools=False, timeout_s=timeout_s,
        session_name=f"{session_name}-repair",
    )
    if not repair.ok:
        raise EnrichmentModelError(
            stage, f"{stage} repair call failed: {repair.error}", report.errors)

    payload = extract_json(repair.text)
    cleaned, repaired_report = gate(payload)
    if not repaired_report.ok:
        raise EnrichmentModelError(
            stage,
            f"{stage} still invalid after one repair: "
            + "; ".join(repaired_report.errors[:5]),
            repaired_report.errors,
        )
    repaired_report.notes.append(f"{stage}: succeeded on the repair call")
    return cleaned, repaired_report, repair.text


def run_topics(stream, *, runner) -> tuple[dict, GateReport, str]:
    """Pass 1: summary and topic spans."""
    def gate(payload):
        report = validate_topics(stream, payload)
        return payload, report

    return _run_pass(
        runner, prompts.topics_prompt(stream),
        stage="topics", timeout_s=TOPICS_TIMEOUT_S,
        session_name="transcript-topics", gate=gate,
    )


def run_mentions(stream, analysis, *, runner, source_kind="audio") -> tuple[dict, GateReport, str]:
    """Pass 2: evidence-bound references inside frozen topic spans."""
    def gate(payload):
        return validate_mentions(stream, analysis, payload)

    prompt = (prompts.reader_mentions_prompt(stream, analysis["topics"])
              if source_kind == "script"
              else prompts.mentions_prompt(stream, analysis["topics"]))
    return _run_pass(
        runner, prompt,
        stage="mentions", timeout_s=MENTIONS_TIMEOUT_S,
        session_name="transcript-mentions", gate=gate,
    )


def mention_batches(topics: list, limit: int = MENTIONS_BATCH_TOPICS) -> list[list]:
    """``topics`` cut into consecutive batches of at most ``limit``, as even as possible
    (47 -> 24 + 23, never 33 + 14); one batch holding all of them at or below ``limit``."""
    count = max(1, -(-len(topics) // limit))
    size, extra = divmod(len(topics), count)
    batches, start = [], 0
    for index in range(count):
        end = start + size + (index < extra)
        batches.append(topics[start:end])
        start = end
    return batches
