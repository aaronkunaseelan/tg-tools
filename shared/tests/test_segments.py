"""Segmented enrichment (episode_enrichment.segments): the generic hook every feed shares.

| segmenter                      | result                                                              |
| none (default)                 | one segment, single-pass notes, manifest analysis.segments = [single] |
| two segments                   | one "## <label>" section each, topics flat, reports per segment     |
| second segment's topics fail   | the enrichment fails naming the segment; no notes.md is written     |
| retry after that failure       | the first segment's calls come from cache (no new model call)       |
| a gap or overlap               | refused before any model call                                       |
| preknown topics + two segments | refused (a reader script is enriched in one pass)                   |
"""
import json
import re
from types import SimpleNamespace

import pytest

from episode_enrichment.pipeline import EnrichmentError, NOTES_FILE, enrich_script, ensure_script
from episode_enrichment.segments import Segment, check_segments
from episode_enrichment.paragraphs import build_stream

SCRIPT = "\n\n".join(f"Paragraph {n} names Thomas Jefferson in passing." for n in range(1, 7))


class Source:
    kind, content_origin, topic_origin, summary_origin = "audio", "stt_transcript", "luna_topics", "luna_summary"

    def transcript(self):
        return SCRIPT


class Runner:
    """Answers both passes for whatever paragraphs the prompt carries; fails on demand."""
    def __init__(self, calls, fail_on=None):
        self.calls, self.fail_on = calls, fail_on

    def run(self, prompt, allow_tools=False, timeout_s=None, session_name=""):
        ids = re.findall(r"^\[(p\d{5})\]", prompt, re.M)
        self.calls.append((session_name, ids[0], ids[-1]))
        if self.fail_on and self.fail_on in ids and "topics" in session_name:
            return SimpleNamespace(ok=False, text="", error="timed out after 1800s")
        heading = f"Topic {ids[0]}"
        if "topics" in session_name:
            body = {"summary": f"About {ids[0]}.", "topics": [
                {"heading": heading, "startParagraphId": ids[0], "endParagraphId": ids[-1]}]}
        else:
            body = {"topics": [{"heading": heading, "mentions": []}]}
        return SimpleNamespace(ok=True, text=json.dumps(body), error=None)


def run(tmp_path, segmenter=None, calls=None, fail_on=None):
    calls = [] if calls is None else calls
    script = ensure_script(work_dir=tmp_path, source=Source())
    return enrich_script(script=script, work_dir=tmp_path, title="T", segmenter=segmenter,
                         runner_factory=lambda role: Runner(calls, fail_on)), calls


def halves(stream, timed, context):
    return [Segment("Hour 1", "p00001", "p00003", 0, 3600, "seam"),
            Segment("Hour 2", "p00004", "p00006", 3600, 7200, "fallback")]


def test_default_is_one_segment(tmp_path):
    result, calls = run(tmp_path)
    assert [c[0] for c in calls] == ["transcript-topics", "transcript-mentions"]
    assert "## Summary" in result.notes_markdown and "## Hour" not in result.notes_markdown
    segments = json.loads(result.manifest)["analysis"]["segments"]
    assert segments == [{"label": "", "startParagraphId": "p00001", "endParagraphId": "p00006",
                         "start": None, "end": None, "rule": "single"}]
    assert set(result.reports) >= {"topics", "mentions"} and "segments" not in result.reports


def test_two_segments_give_two_sections(tmp_path):
    result, calls = run(tmp_path, halves)
    assert [(c[1], c[2]) for c in calls if "topics" in c[0]] == [("p00001", "p00003"), ("p00004", "p00006")]
    notes = result.notes_markdown
    assert notes.index("## Hour 1") < notes.index("### Topics") < notes.index("## Hour 2")
    assert [t["heading"] for t in result.analysis["topics"]] == ["Topic p00001", "Topic p00004"]
    assert result.analysis["summary"] == "Hour 1: About p00001.\n\nHour 2: About p00004."
    manifest = json.loads(result.manifest)
    assert [s["rule"] for s in manifest["analysis"]["segments"]] == ["seam", "fallback"]
    assert [r["label"] for r in result.reports["segments"]] == ["Hour 1", "Hour 2"]


def test_a_failing_segment_fails_by_name_and_a_retry_reuses_the_rest(tmp_path):
    with pytest.raises(EnrichmentError) as error:
        run(tmp_path, halves, fail_on="p00004")
    assert "Hour 2" in str(error.value) and error.value.stage.startswith("topics")
    assert not (tmp_path / NOTES_FILE).exists()
    result, calls = run(tmp_path, halves)
    assert [(c[0], c[1]) for c in calls] == [("transcript-topics", "p00004"), ("transcript-mentions", "p00004")]
    assert "## Hour 2" in result.notes_markdown


@pytest.mark.parametrize("segments, error", [
    ([Segment("a", "p00001", "p00002"), Segment("b", "p00004", "p00006")], "starts at p00004"),
    ([Segment("a", "p00001", "p00004"), Segment("b", "p00004", "p00006")], "starts at p00004"),
    ([Segment("a", "p00001", "p00005")], "stop after 5 of 6"),
])
def test_segments_must_cover_the_stream_once(tmp_path, segments, error):
    assert any(error in e for e in check_segments(build_stream(SCRIPT), segments))
    with pytest.raises(EnrichmentError) as raised:
        run(tmp_path, lambda stream, timed, context: segments)
    assert raised.value.stage == "segments"


def test_preknown_topics_stay_one_segment(tmp_path):
    class Reader(Source):
        has_preknown_topics = True
    script = ensure_script(work_dir=tmp_path, source=Reader())
    with pytest.raises(EnrichmentError, match="one segment"):
        enrich_script(script=script, work_dir=tmp_path, title="T", segmenter=halves, topics=[{"heading": "x"}],
                      runner_factory=lambda role: Runner([]))
