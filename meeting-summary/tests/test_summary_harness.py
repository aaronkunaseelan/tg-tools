"""The meeting summary is one contained harness call on the `meetings` pipeline's tier (X-104.3).

| tier file / reply                     | outcome                                                        |
| meetings: deepseek                    | `pi -p` on opencode-go-min/deepseek-v4.1-flash at thinking xhigh |
| strict JSON reply                      | the summary object                                              |
| fenced JSON                            | parsed                                                          |
| run failed (timeout, non-zero exit)    | SummaryError model_transient -> retryable job                    |
| reply not JSON / not an object         | SummaryError model_schema_invalid -> retryable job               |
| empty transcript                       | SummaryError input_empty -> terminal job                         |
| no `meetings` entry in the tier file    | EnrichmentConfigError -> terminal job, before any audio work      |
"""
import json
import os
from types import SimpleNamespace as NS

import pytest

from pipeline import execute_pipeline
from runtime_worker import classify_worker_error
from summarizer import SUMMARY_ROLE, SummaryError, summarize_transcript_chunks

FINAL = {"title": "Launch timing review", "meeting_date": "", "overview": "The launch moves.",
         "attendees": ["Ana"], "key_points": [], "decisions": [], "action_items": [], "open_questions": []}
CHUNK = {"title_hint": "Launch timing", "meeting_date_hint": "", "overview": "The launch moves.",
         "attendees": ["Ana"], "key_points": [], "decisions": [], "action_items": [], "open_questions": []}


def fake_pi(tmp_path, monkeypatch, *, replies=None, exit_code=0, stderr=""):
    """A `pi` stand-in that logs each call and answers in order; PiRunner is the real one."""
    log = tmp_path / "pi.jsonl"
    script = tmp_path / "bin" / "pi"
    script.parent.mkdir(exist_ok=True)
    texts = list(replies or [json.dumps(CHUNK), json.dumps(FINAL)])
    script.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        f"log, texts, code, err = {str(log)!r}, {json.dumps(texts)}, {exit_code}, {stderr!r}\n"
        "call = {'argv': sys.argv[1:], 'stdin': sys.stdin.read()}\n"
        "open(log, 'a').write(json.dumps(call) + '\\n')\n"
        "text = texts[min(len(open(log).read().splitlines()) - 1, len(texts) - 1)]\n"
        "sys.stderr.write(err)\n"
        "print(text)\nsys.exit(code)\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{script.parent}:{os.environ['PATH']}")
    return log


def deepseek_meetings(enrichment_tiers):
    enrichment_tiers.write_text(json.dumps({"meetings": "deepseek"}))
    return enrichment_tiers


def runner_for(tmp_path, pipeline="meetings"):
    from pi_harness import PiRunner, enrichment_tier
    work = tmp_path / "summary"
    work.mkdir(exist_ok=True)
    return PiRunner(SUMMARY_ROLE, cwd=str(work), tier=enrichment_tier(pipeline))


def test_the_summary_runs_on_the_meetings_tier_through_pi(tmp_path, monkeypatch, enrichment_tiers):
    deepseek_meetings(enrichment_tiers)
    log = fake_pi(tmp_path, monkeypatch)
    chunks, final = summarize_transcript_chunks(
        settings=NS(summary_timeout_sec=30), chunk_texts=["The launch moves to November."],
        runner=runner_for(tmp_path))
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    chunk_call, final_call = calls
    for call in calls:
        argv = call["argv"]
        assert argv[argv.index("--provider") + 1] == "opencode-go-min"
        assert argv[argv.index("--model") + 1] == "deepseek-v4.1-flash"
        assert argv[argv.index("--thinking") + 1] == "xhigh"
        assert {"--no-tools", "--no-extensions", "--no-skills", "--no-context-files"} <= set(argv)
        assert "--system-prompt" in argv
    assert "Summarize this meeting transcript chunk" in chunk_call["stdin"]
    assert "The launch moves to November." in chunk_call["stdin"]
    assert "Produce one final meeting summary" in final_call["stdin"]
    assert (chunks, final) == ([CHUNK], FINAL)


@pytest.mark.parametrize("reply", ['```json\n{"title": "x"}\n```', ' {"title": "x"} '])
def test_a_json_reply_is_parsed_through_fences_and_space(tmp_path, monkeypatch, enrichment_tiers, reply):
    deepseek_meetings(enrichment_tiers)
    fake_pi(tmp_path, monkeypatch, replies=[reply, json.dumps(FINAL)])
    chunks, final = summarize_transcript_chunks(
        settings=NS(summary_timeout_sec=30), chunk_texts=["t"], runner=runner_for(tmp_path))
    assert chunks == [{"title": "x"}] and final == FINAL


def test_a_failed_call_is_a_named_job_error(tmp_path, monkeypatch, enrichment_tiers):
    deepseek_meetings(enrichment_tiers)
    fake_pi(tmp_path, monkeypatch, exit_code=3, stderr="pi: provider error: quota")
    with pytest.raises(SummaryError) as error:
        summarize_transcript_chunks(settings=NS(summary_timeout_sec=30), chunk_texts=["t"],
                                    runner=runner_for(tmp_path))
    assert error.value.error_class == "model_transient"
    assert classify_worker_error(error.value) == ("retryable_error", "model_transient", str(error.value))


@pytest.mark.parametrize("reply", ["not json at all", "[]", '"a string"'])
def test_an_unusable_reply_is_a_named_job_error(tmp_path, monkeypatch, enrichment_tiers, reply):
    deepseek_meetings(enrichment_tiers)
    fake_pi(tmp_path, monkeypatch, replies=[reply])
    with pytest.raises(SummaryError) as error:
        summarize_transcript_chunks(settings=NS(summary_timeout_sec=30), chunk_texts=["t"],
                                    runner=runner_for(tmp_path))
    assert error.value.error_class == "model_schema_invalid"
    assert classify_worker_error(error.value)[0] == "retryable_error"


def test_an_empty_transcript_is_terminal(tmp_path, monkeypatch, enrichment_tiers):
    deepseek_meetings(enrichment_tiers)
    with pytest.raises(SummaryError) as error:
        summarize_transcript_chunks(settings=NS(summary_timeout_sec=30), chunk_texts=[],
                                    runner=runner_for(tmp_path))
    assert (error.value.error_class, classify_worker_error(error.value)[0]) == ("input_empty", "terminal_error")


def test_a_missing_meetings_tier_fails_the_job_before_the_audio(tmp_path, enrichment_tiers):
    """The tier is read before any audio work: the job fails on the file, terminal, not on the input."""
    enrichment_tiers.write_text(json.dumps({"episodes": "haiku"}))
    job = {"inputJson": {"sourceKind": "telegram_audio", "sourceKey": "k",
                         "localWorkDir": str(tmp_path / "work"), "localSourcePath": str(tmp_path / "missing.ogg")}}
    with pytest.raises(Exception) as error:
        execute_pipeline(NS(), job)
    assert (error.value.error_class, classify_worker_error(error.value)[:2]) == (
        "enrichment_config", ("terminal_error", "enrichment_config"))
    assert "meetings" in str(error.value)
