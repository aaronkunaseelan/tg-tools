"""Pass 2 in batches of consecutive topics (luna.mention_batches, pipeline._stage_mentions).

| frozen topics                  | result                                                                  |
| 33 (MENTIONS_BATCH_TOPICS)     | one call, the prompt byte for byte as before, the report as before       |
| 34                             | two calls of 17, each carrying only its topics' paragraphs; joined in    |
|                                | order, one report whose counts add up; each batch checkpointed           |
| 34, batch 2 stays invalid      | the pass fails naming "batch 2/2 (topics 18-34)"; no notes.md is written |
| retry after that failure       | batch 1 comes from its checkpoint: one new call, for batch 2             |
| a new script                   | every mentions-batch-*.json checkpoint is cleared                        |
| 34                             | gates.json beside the job holds the batches  ; one INFO line   |
|                                | per batch, and one naming the batch that fails                           |
| 34, a later stage raises       | the report file is written with what ran, then the failure is raised     |
"""
import json
import logging
import re
from types import SimpleNamespace

import pytest

from episode_enrichment import pipeline, prompts
from episode_enrichment.luna import MENTIONS_BATCH_TOPICS, mention_batches
from episode_enrichment.paragraphs import build_stream
from episode_enrichment.pipeline import (NOTES_FILE, REPORT_FILE, EnrichmentError, _invalidate_dependents,
                                          enrich_script, ensure_script)

FROZEN_RE = re.compile(r'^- "(.+)" covers (p\d{5})\.\.(p\d{5})$', re.M)


def script(paragraphs):
    return "\n\n".join(f"Paragraph {n} says Thomas Jefferson wrote it." for n in range(1, paragraphs + 1))


class Source:
    kind, content_origin, topic_origin, summary_origin = "audio", "stt_transcript", "luna_topics", "luna_summary"

    def __init__(self, paragraphs):
        self.text = script(paragraphs)

    def transcript(self):
        return self.text


class Runner:
    """One topic per paragraph; each frozen topic gets one evidence-bound mention. ``drop_on`` names a
    paragraph whose mentions batch always answers one topic short."""
    def __init__(self, calls, drop_on=None):
        self.calls, self.drop_on = calls, drop_on

    def run(self, prompt, allow_tools=False, timeout_s=None, session_name=""):
        ids = re.findall(r"^\[(p\d{5})\]", prompt, re.M)
        self.calls.append(SimpleNamespace(session=session_name, ids=ids, prompt=prompt))
        if "topics" in session_name:
            body = {"summary": "About the paragraphs.", "topics": [
                {"heading": f"Topic {pid}", "startParagraphId": pid, "endParagraphId": pid} for pid in ids]}
        else:
            frozen = FROZEN_RE.findall(prompt.split("FROZEN TOPIC SEGMENTS", 1)[1].split("TRANSCRIPT", 1)[0])
            body = {"topics": [{"heading": heading, "mentions": [{
                "type": "person", "displayText": "Thomas Jefferson", "referencedAs": "Wrote it.",
                "paragraphId": start, "evidenceQuote": "says Thomas Jefferson wrote it"}]}
                for heading, start, _ in frozen]}
            if self.drop_on and self.drop_on in ids:
                body["topics"].pop()
        return SimpleNamespace(ok=True, text=json.dumps(body), error=None)


def run(tmp_path, paragraphs, calls, drop_on=None):
    prepared = ensure_script(work_dir=tmp_path, source=Source(paragraphs))
    return enrich_script(script=prepared, work_dir=tmp_path, title="T",
                         runner_factory=lambda role: Runner(calls, drop_on))


def mention_calls(calls):
    return [c for c in calls if c.session.startswith("transcript-mentions")]


@pytest.mark.parametrize("count, sizes", [
    (1, [1]), (MENTIONS_BATCH_TOPICS, [MENTIONS_BATCH_TOPICS]), (34, [17, 17]),
    (47, [24, 23]), (50, [25, 25]), (67, [23, 22, 22]), (99, [33, 33, 33]), (100, [25, 25, 25, 25]),
])
def test_batches_are_consecutive_even_and_at_most_the_limit(count, sizes):
    topics = list(range(count))
    batches = mention_batches(topics)
    assert [len(b) for b in batches] == sizes
    assert [t for b in batches for t in b] == topics


def test_at_the_limit_one_call_and_the_prompt_unchanged(tmp_path):
    calls = []
    result = run(tmp_path, MENTIONS_BATCH_TOPICS, calls)
    [call] = mention_calls(calls)
    stream = build_stream(script(MENTIONS_BATCH_TOPICS))
    assert call.prompt == prompts.mentions_prompt(stream, result.analysis["topics"])
    assert "batches" not in result.reports["mentions"]
    assert result.reports["mentions"]["stats"]["mentionsKept"] == MENTIONS_BATCH_TOPICS
    assert not list(tmp_path.glob("mentions-batch-*.json"))


def test_above_the_limit_batches_join_in_order_under_one_report(tmp_path):
    calls = []
    result = run(tmp_path, 34, calls)
    first, second = mention_calls(calls)
    assert (first.ids[0], first.ids[-1], len(first.ids)) == ("p00001", "p00017", 17)
    assert (second.ids[0], second.ids[-1], len(second.ids)) == ("p00018", "p00034", 17)
    assert len(FROZEN_RE.findall(first.prompt)) == 17 and "[p00018]" not in first.prompt
    headings = [t["heading"] for t in result.analysis["topics"]]
    assert [t["heading"] for t in result.mentions["topics"]] == headings
    assert [m["paragraphId"] for t in result.mentions["topics"] for m in t["mentions"]] == \
        [f"p{n:05d}" for n in range(1, 35)]
    report = result.reports["mentions"]
    assert report["ok"] and report["stats"] == {"mentionsReturned": 34, "mentionsKept": 34,
                                                "mentionsDropped": 0, "keepRate": 1.0}
    assert [(b["batch"], b["topics"], b["cached"]) for b in report["batches"]] == [
        ("batch 1/2 (topics 1-17)", 17, False), ("batch 2/2 (topics 18-34)", 17, False)]
    assert result.mention_count == 34 and len(list(tmp_path.glob("mentions-batch-*.json"))) == 2


def test_a_failing_batch_fails_by_name_and_a_retry_reuses_the_rest(tmp_path, caplog):
    calls = []
    with caplog.at_level(logging.ERROR, logger="episode_enrichment.pipeline"), pytest.raises(EnrichmentError) as error:
        run(tmp_path, 34, calls, drop_on="p00018")
    assert [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("mentions batch 2/2 (topics 18-34) failed")]
    assert error.value.stage == "mentions:mentions"
    assert "batch 2/2 (topics 18-34)" in str(error.value) and "17 frozen, 16 returned" in str(error.value)
    assert [c.session for c in mention_calls(calls)] == [
        "transcript-mentions", "transcript-mentions", "transcript-mentions-repair"]
    assert not (tmp_path / NOTES_FILE).exists()
    retry = []
    result = run(tmp_path, 34, retry)
    [call] = mention_calls(retry)
    assert call.ids[0] == "p00018" and [c.session for c in retry] == ["transcript-mentions"]
    assert [b["cached"] for b in result.reports["mentions"]["batches"]] == [True, False]
    assert result.mention_count == 34


def test_a_new_script_clears_the_batch_checkpoints(tmp_path):
    run(tmp_path, 34, [])
    assert list(tmp_path.glob("mentions-batch-*.json"))
    _invalidate_dependents(tmp_path)
    assert not list(tmp_path.glob("mentions-*.json")) and not list(tmp_path.glob("topics-*.json"))


def test_reports_file_written_with_batches(tmp_path):
    result = run(tmp_path, 34, [])
    written = json.loads((tmp_path / REPORT_FILE).read_text(encoding="utf-8"))
    assert written == result.reports
    assert written["script"]["ok"] is True
    batches = written["mentions"]["batches"]
    assert [b["batch"] for b in batches] == ["batch 1/2 (topics 1-17)", "batch 2/2 (topics 18-34)"]
    assert [b["topics"] for b in batches] == [17, 17]
    assert written["mentions"]["stats"]["mentionsKept"] == 34


def test_batch_lines_logged(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="episode_enrichment.pipeline"):
        run(tmp_path, 34, [])
    lines = [r.getMessage() for r in caplog.records if r.name == "episode_enrichment.pipeline"]
    batch_lines = [line for line in lines if line.startswith("mentions batch ")]
    assert len(batch_lines) == 2
    assert batch_lines[0].startswith("mentions batch 1/2 (topics 1-17): 17 mentions, ")
    assert batch_lines[1].startswith("mentions batch 2/2 (topics 18-34): 17 mentions, ")
    assert all(line.endswith(" s") for line in batch_lines)


def test_reports_file_written_on_later_failure(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("the notes renderer failed")

    monkeypatch.setattr(pipeline.render, "render_notes_markdown", boom)
    with pytest.raises(RuntimeError, match="the notes renderer failed"):
        run(tmp_path, 34, [])
    written = json.loads((tmp_path / REPORT_FILE).read_text(encoding="utf-8"))
    assert [b["batch"] for b in written["mentions"]["batches"]] == [
        "batch 1/2 (topics 1-17)", "batch 2/2 (topics 18-34)"]
    assert not (tmp_path / NOTES_FILE).exists()
