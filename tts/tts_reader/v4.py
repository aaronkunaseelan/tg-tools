"""Reader v4: one writer thread, one whole-script review, at most one revision.

The writer sees the whole source and plans in its own thinking. A second
reader checks the complete script against the source. If it reports anything,
or the script is over budget, the writer's own conversation is resumed with
those notes: it judges them and returns the complete revised script. Code only
measures length, parses the reply format and keeps receipts; meaning is left
to the models.

Each role names its model in pi_harness/roles.py (or a TTS_PI_ROLES_FILE
override). Provider "claude" runs `claude -p` on the subscription login; any
other provider runs `pi -p`. Either way the writer is one stored conversation,
so the revision turn reads brief + source from the provider's prompt cache.
On claude the writer's brief + source carry one explicit 1-hour cache marker.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tg_tools_common.progress import current_progress
from pi_harness.roles import resolve_role
from pi_harness.runner import (LOG_CAP_BYTES, _read_capped, claude_cmd, pi_cmd, pi_env,
                               pruned_env, run_contained)

from .errors import ExplainError
from .hazards import WORD_CAP, _words, speech_hazards

WORD_TARGET = 4800
WORDS_PER_MINUTE = 160
CALL_TIMEOUT_S = int(os.environ.get("TTS_X_V4_CALL_TIMEOUT_S", "2400"))
MAX_OUTPUT_TOKENS = os.environ.get("TTS_X_V4_MAX_OUTPUT_TOKENS", "64000")

WRITER_BRIEF = f"""You're turning a written source into a spoken explanation for one listener. They're commuting or walking, hearing it once at about {WORDS_PER_MINUTE} words a minute, and they can't scroll back. They're smart and curious but not a specialist in this field. What they should finish with is a working model of the source's argument - enough to explain the main idea to a friend, and to know where the authors themselves are unsure. A list of findings doesn't do that; an account of what was asked, how it was found out, what came out and what it means does. Early on, tell the listener what they're hearing about: what kind of source it is, where it appeared and when, as the source itself states them. A 2020 journal paper, a preprint and last week's blog post are weighed differently, and the listener has no cover or URL to check. One sentence does it. If the source doesn't say, leave it out rather than guess.

Take your time with the source before writing. Work out its central idea - what the listener should come away holding - and put it in concrete terms: what someone actually does or sees, not the name of a school of thought. The listener builds their understanding on whatever they hear first and hangs everything after onto it; a point explained only at the end arrives too late to organise what came before, and they can't go back. A paper's own order was written for a reader who has seen the abstract and can skim ahead, so it is often the wrong order for a listener. After the sentence about what the source is, give the central idea in a sentence or two they could repeat to a friend - stating it, not announcing it - then build the rest as support for it: the evidence, how it was found out, where it stops. Background comes after that, and only as much as the next point needs. Introduce each idea once, at the point where it's first needed, and trust the listener to remember it: repeating a definition costs them time and suggests you think they weren't listening. The same holds inside a sentence: a listener holds each unfinished piece until it's resolved. Get the subject and its verb out early and close together, and don't park a qualification between them - give it its own sentence, moving it rather than dropping it. Let the new information come at the end of a sentence, where it lands; the aim is fewer open pieces, not fewer words. A listener can't see letters, symbols or equations, so say what a quantity is and what a calculation does, in words. One or two numbers per point is plenty; a run of numbers turns into noise when heard. Say the thing rather than announcing it: "here's the sequence" or "two other points are easy to miss" spend the listener's time promising content instead of giving it. A pointer earns its place only when it sets up something that pays off later - "hold on to those two, they come back" helps a listener who can't scroll back. Name the authors or institutions only where knowing who said something changes how to take it.

Fidelity is the part that has gone wrong before, and the errors are small and easy to miss. "Impaired" became "lost". "Many complex models" became "complex models". A patient was given a gender the source never stated. Each of these changes what the listener believes. Compressing and paraphrasing is the job, but keep every hedge, scope and condition that changes what a claim means, and don't add details the source doesn't give. Where the source is uncertain, or its authors flag a limitation, the listener should hear that. Background that isn't in the source is fine only where the listener needs it to follow, and it should sound like background, not like a finding.

Aim for about 30 minutes - roughly {WORD_TARGET} words, never more than {WORD_CAP}. This is a limit on the listener's time, not a quota: if the material is fully explained in less, stop.

The script is read aloud exactly as written, so write only what should be heard: plain paragraphs, no bullet points, tables, links, citations or stage directions. Divide it into a handful of chapters, each starting with a line "## " plus a short chapter title; the titles become show-note chapters and are not spoken.

Reply in exactly this form:

=== TITLE ===
An episode title, in plain words.
=== SYNOPSIS ===
Two sentences saying what the listener will learn.
=== SCRIPT ===
## First chapter title
The script..."""

REVIEW_BRIEF = """You're a second reader. Below is a source, and a script that explains it for a listener who hears it once. Your job is to catch the places where that listener would come away believing something the source doesn't say: a dropped hedge, condition or scope; a changed quantity or direction; a wrong attribution; a detail the source doesn't give; a limitation the authors stated that the script presents as settled. Also catch places where the listener would be lost because an idea is used before it's explained, where they'd go minutes without knowing what the material is building toward, or where the same explanation is given twice.

Before you flag anything, read the sentences around it: a condition stated nearby counts as stated. Spoken paraphrase and simplification are expected; a different wording that leaves the meaning intact is not a finding, and neither is a stylistic preference. Background the script clearly presents as background is fine.

For each finding, quote the script, quote the source where it applies, and say in one line what the listener would wrongly believe or where they'd get lost. A good review is short; most scripts have between zero and eight real findings, and an empty review is a perfectly good answer.

Number your findings, then end with exactly one line:
=== FINDINGS: N ===
where N is how many you reported (0 if none)."""

REVISE_TURN = """A second reader checked your script against the source. Their notes are below. They're careful but sometimes wrong - for example, flagging a condition you state in the next sentence. You're the judge: fix what's real, leave what isn't.

{length_note}

Return the complete revised script in the same form (=== TITLE ===, === SYNOPSIS ===, === SCRIPT ===), followed by:
=== RULINGS ===
one line per note: its number, accepted or rejected, and why.

Second reader's notes:
{findings}"""

LENGTH_OVER = ("Separately, the script measures {words} words, about {minutes} minutes, against the "
               "roughly 30-minute budget ({target} words, never more than {cap}). Cut where it costs "
               "the listener least - repeated explanations, runs of numbers, detail that doesn't serve "
               "the argument.")
LENGTH_OK = "The script measures {words} words, about {minutes} minutes, which is within budget."

FORMAT_RETRY = ("Your reply couldn't be read: {error}. Please send the complete reply again in exactly "
                "the requested form, starting with === TITLE ===.")

# Any edit to the prompts above is a new reader identity (fingerprint.py).
PROMPT_VERSION = "reader-v4-" + hashlib.sha256("\0".join(
    (WRITER_BRIEF, REVIEW_BRIEF, REVISE_TURN, LENGTH_OVER, LENGTH_OK, FORMAT_RETRY)
).encode("utf-8")).hexdigest()[:12]


@dataclass
class Turn:
    step: str
    role: str
    text: str
    usage: dict[str, int]
    model: str
    effort: str
    wall_seconds: float
    cost_usd: float | None
    session: str | None


@dataclass
class V4Result:
    title: str
    synopsis: str
    script: str
    terminal_state: str
    review_flags: list[str]
    report: dict[str, Any]
    turns: list[Turn] = field(default_factory=list)


_MARK = re.compile(r"^=== ([A-Z]+)(?:: (\d+))? ===\s*$", re.MULTILINE)


def parse_reply(text: str, *, rulings: bool = False) -> dict[str, str]:
    marks = list(_MARK.finditer(text))
    parts = {m.group(1): text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))].strip()
             for i, m in enumerate(marks)}
    for name in ("TITLE", "SYNOPSIS", "SCRIPT") + (("RULINGS",) if rulings else ()):
        if not parts.get(name):
            raise ValueError(f"missing or empty === {name} === section")
    if not parts["SCRIPT"].startswith("## "):
        raise ValueError("the script must start with a '## ' chapter title line")
    return parts


def finding_count(review: str) -> int:
    counts = [int(m.group(2)) for m in _MARK.finditer(review) if m.group(1) == "FINDINGS" and m.group(2)]
    if not counts:
        raise ValueError("review has no === FINDINGS: N === line")
    return counts[-1]


def spoken_words(script: str) -> int:
    return _words("\n".join(line for line in script.splitlines() if not line.startswith("## ")))


@dataclass
class Reply:
    text: str
    usage: dict[str, int]
    raw_usage: dict[str, Any]
    cost_usd: float | None


def read_claude(stdout: str) -> Reply:
    """The result event of a `claude -p --output-format stream-json` turn."""
    events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None or result.get("is_error"):
        raise ValueError(str((result or {}).get("result") or "no result event"))
    raw = result.get("usage") or {}
    return Reply(str(result.get("result") or "").strip(), {
        "input_tokens": int(raw.get("input_tokens") or 0),
        "cache_creation_input_tokens": int(raw.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(raw.get("cache_read_input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "thinking_tokens": int((raw.get("output_tokens_details") or {}).get("thinking_tokens") or 0),
    }, raw, result.get("total_cost_usd"))


def read_pi(stdout: str) -> Reply:
    """The final assistant message of a `pi -p --mode json` turn.

    pi exits 0 even when the provider refuses (usage limit, auth), so success
    is read from the message: only stopReason "stop" is a complete reply.
    """
    ends = [e for e in (json.loads(line) for line in stdout.splitlines() if line.startswith("{"))
            if e.get("type") == "agent_end"]
    message = next((m for m in reversed(ends[-1]["messages"]) if m.get("role") == "assistant"),
                   None) if ends else None
    if message is None:
        raise ValueError("no assistant message")
    if message.get("stopReason") != "stop":
        raise ValueError(f"stopReason {message.get('stopReason')}: {message.get('errorMessage') or ''}")
    raw = message.get("usage") or {}
    text = "".join(c.get("text") or "" for c in message.get("content") or [] if c.get("type") == "text")
    return Reply(text.strip(), {
        "input_tokens": int(raw.get("input") or 0),
        "cache_creation_input_tokens": int(raw.get("cacheWrite") or 0),
        "cache_read_input_tokens": int(raw.get("cacheRead") or 0),
        "output_tokens": int(raw.get("output") or 0),
        "thinking_tokens": 0,  # pi reports reasoning inside output
    }, raw, (raw.get("cost") or {}).get("total"))


class ModelThread:
    """Closed turns with the model a role names: no tools, settings, skills or MCP.

    With ``persist`` every turn after the first continues the same stored
    conversation, so the earlier turns are the cached prefix of the next one.
    """

    def __init__(self, role: str, evidence: Path, *, persist: bool):
        cfg = resolve_role(role)
        self.role, self.provider, self.model = role, cfg["provider"], cfg["model"]
        self.effort, self.system_prompt = cfg["thinking"], cfg.get("system_prompt")
        self.flags = list(cfg.get("flags") or [])
        self.evidence = evidence
        # One stable cwd per thread: claude keys stored sessions by cwd.
        self.cwd = evidence / f"thread-{role}"
        self.session = None
        if persist:
            self.session = (str(uuid.uuid4()) if self.provider == "claude"
                            else str(self.cwd / "session.jsonl"))
        self.turns = 0

    def _command(self, content: list[dict]) -> tuple[list[str], str, dict, Callable[[str], Reply]]:
        if self.provider == "claude":
            cmd = claude_cmd(self.model, self.effort, self.system_prompt, self.flags, stream=True,
                             session_id=self.session, resume=self.turns > 0)
            env = pruned_env("claude")
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = MAX_OUTPUT_TOKENS
            message = json.dumps({"type": "user", "message": {"role": "user", "content": content}},
                                 ensure_ascii=False) + "\n"
            return cmd, message, env, read_claude
        cmd = pi_cmd(self.provider, self.model, self.effort, self.system_prompt, self.flags,
                     session_file=self.session, json_mode=True)
        return cmd, "\n\n".join(block["text"] for block in content), pi_env(self.provider), read_pi

    def send(self, step: str, content: list[dict]) -> Turn:
        cmd, stdin, env, read = self._command(content)
        (self.evidence / "requests").mkdir(parents=True, exist_ok=True)
        (self.evidence / "requests" / f"{step}.json").write_text(
            json.dumps({"cmd": cmd, "content": content}, ensure_ascii=False, indent=1), encoding="utf-8")
        started = time.monotonic()
        rc, timed_out, out_p, err_p = run_contained(cmd, stdin, str(self.cwd), env, CALL_TIMEOUT_S, tag=step)
        wall = round(time.monotonic() - started, 1)
        out_p = str(Path(out_p).replace(self.cwd / f"{step}.stdout.log"))
        err_p = str(Path(err_p).replace(self.cwd / f"{step}.stderr.log"))
        if timed_out:
            raise ExplainError("model_timeout", f"{step} timed out after {CALL_TIMEOUT_S}s")
        try:
            if rc != 0:
                raise ValueError(f"exit {rc}")
            reply = read(_read_capped(out_p, LOG_CAP_BYTES))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            detail = f"{exc} {_read_capped(err_p, LOG_CAP_BYTES)[-1500:]}".strip()
            raise ExplainError("model_call_failed", f"{step} ({self.provider}/{self.model}): {detail}") from exc
        self.turns += 1
        turn = Turn(step, self.role, reply.text, reply.usage, self.model, self.effort, wall,
                    reply.cost_usd, self.session)
        (self.evidence / "replies").mkdir(exist_ok=True)
        (self.evidence / "replies" / f"{step}.txt").write_text(turn.text, encoding="utf-8")
        (self.evidence / "receipts").mkdir(exist_ok=True)
        (self.evidence / "receipts" / f"{step}.json").write_text(json.dumps({
            "step": step, "role": self.role, "provider": self.provider, "model": self.model,
            "effort": self.effort, "session": self.session, "wallSeconds": wall,
            "usage": reply.usage, "rawUsage": reply.raw_usage, "costUSD": reply.cost_usd},
            indent=1), encoding="utf-8")
        return turn


def _evidence_blocks(brief: str, source: str, *, cache: bool) -> list[dict]:
    """Brief and source as one block; only a thread that will be resumed pays for the 1-hour
    cache write. The marker is claude's; pi providers cache the prefix on their own."""
    block = {"type": "text", "text": brief + "\n\n<source>\n" + source + "\n</source>"}
    if cache:
        block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return [block]


def _write_until_parsed(thread: ModelThread, step: str, content: list[dict], *,
                        rulings: bool = False) -> tuple[dict[str, str], list[Turn]]:
    """One corrective turn in the same thread if the reply format can't be read."""
    turn = thread.send(step, content)
    try:
        return parse_reply(turn.text, rulings=rulings), [turn]
    except ValueError as exc:
        retry = thread.send(step + "-format", [{"type": "text", "text": FORMAT_RETRY.format(error=exc)}])
        try:
            return parse_reply(retry.text, rulings=rulings), [turn, retry]
        except ValueError as again:
            raise ExplainError("reader_contract_invalid", f"{step}: {again}") from again


def _usage_totals(turns: list[Turn]) -> dict[str, Any]:
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens",
            "thinking_tokens")
    by_model: dict[str, dict[str, int]] = {}
    for turn in turns:
        row = by_model.setdefault(turn.model, {k: 0 for k in keys + ("calls",)})
        row["calls"] += 1
        for key in keys:
            row[key] += int(turn.usage.get(key) or 0)
    total = {k: sum(row[k] for row in by_model.values()) for k in keys}
    return {"inputTokens": total["input_tokens"] + total["cache_creation_input_tokens"] + total["cache_read_input_tokens"],
            "uncachedInputTokens": total["input_tokens"],
            "cacheCreationInputTokens": total["cache_creation_input_tokens"],
            "cachedInputTokens": total["cache_read_input_tokens"],
            "outputTokens": total["output_tokens"], "reasoningOutputTokens": total["thinking_tokens"],
            "totalTokens": sum(total[k] for k in keys[:4]), "modelCalls": len(turns),
            "usageEstimated": False, "usageIncomplete": False, "byModel": by_model}


def run_reader(source_md: str, work_dir: Path, cancel: Callable[[], Any] | None = None) -> V4Result:
    evidence = work_dir / "explain-v4"
    evidence.mkdir(parents=True, exist_ok=True)
    check_cancel = cancel or (lambda: None)
    writer = ModelThread("write", evidence, persist=True)
    reviewer = ModelThread("review", evidence, persist=False)
    turns: list[Turn] = []

    check_cancel()
    with current_progress().stage("write_explanation"):
        draft, sent = _write_until_parsed(writer, "write", _evidence_blocks(WRITER_BRIEF, source_md, cache=True) + [
            {"type": "text", "text": "Write the episode."}])
    turns += sent
    first_words = spoken_words(draft["SCRIPT"])

    check_cancel()
    with current_progress().stage("review_explanation"):
        review = reviewer.send("review", _evidence_blocks(REVIEW_BRIEF, source_md, cache=False) + [
            {"type": "text", "text": "<script>\n" + draft["SCRIPT"] + "\n</script>"}])
    turns.append(review)
    try:
        findings = finding_count(review.text)
    except ValueError:
        findings = None  # unreadable count: let the writer judge the notes as written

    final, rulings = draft, ""
    over = first_words > WORD_TARGET
    if findings != 0 or over:
        check_cancel()
        length = (LENGTH_OVER if over else LENGTH_OK).format(
            words=first_words, minutes=round(first_words / WORDS_PER_MINUTE), target=WORD_TARGET, cap=WORD_CAP)
        notes = review.text if findings != 0 else "(none - the second reader found nothing to change.)"
        with current_progress().stage("revise_explanation"):
            final, sent = _write_until_parsed(writer, "revise", [{"type": "text", "text": REVISE_TURN.format(
                length_note=length, findings=notes)}], rulings=True)
        turns += sent
        rulings = final["RULINGS"]

    words = spoken_words(final["SCRIPT"])
    flags = [f"speech_hazard:{h.kind}:{h.quote}" for h in speech_hazards(final["SCRIPT"])]
    report = {
        "prompt_version": PROMPT_VERSION, "terminal_state": "requires_review" if flags else "complete",
        "first_draft_words": first_words, "words": words, "word_target": WORD_TARGET, "word_cap": WORD_CAP,
        "review_findings": findings, "revised": bool(rulings), "rulings": rulings,
        "writer_session": writer.session,
        "turns": [{"step": t.step, "model": t.model, "effort": t.effort, "wallSeconds": t.wall_seconds,
                   "usage": t.usage, "costUSD": t.cost_usd} for t in turns],
        "usage": _usage_totals(turns),
    }
    (evidence / "run-report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    (evidence / "script.md").write_text(final["SCRIPT"] + "\n", encoding="utf-8")
    return V4Result(final["TITLE"], final["SYNOPSIS"], final["SCRIPT"], report["terminal_state"],
                    flags, report, turns)
