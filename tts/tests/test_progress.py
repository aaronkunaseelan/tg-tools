"""TTS uses the shared progress transport without changing execution authority."""
import asyncio
from types import SimpleNamespace

import pytest

from tg_tools_common.progress import ProgressReporter, current_progress, use_progress
from tts_worker import worker, speech_gemini
from tg_tools_common.lease import LeaseLost
from tts_reader import v4


class Heartbeat:
    lost = False
    def start(self): pass
    def stop(self): pass
    def check(self):
        if self.lost:
            raise LeaseLost("lost")


def reporter(events):
    return ProgressReporter(send=events.append, job_id="job", attempt_id="attempt", lease_token="token").start()


@pytest.mark.parametrize("enabled,outcome", [(True, "success"), (True, "failure"), (True, "lost"), (False, "success")])
def test_claim_progress_context_and_finalization(monkeypatch, enabled, outcome):
    monkeypatch.setenv("TG_TOOLS_PROGRESS_ENABLED", "1" if enabled else "0")
    events, finalizations = [], []
    heartbeat = Heartbeat()

    def execute(*args, **kwargs):
        with current_progress().stage("write_explanation"):
            if outcome == "failure":
                raise worker.TTSJobError("model", "pipeline_failure", "bad")
            if outcome == "lost":
                heartbeat.lost = True
                heartbeat.check()
        return {"ok": True}

    def finalize(payload):
        # Facts have been flushed while the lease still authorizes writes.
        if enabled:
            assert events[-1]["kind"] == ("interrupted" if outcome == "failure" else "completed")
        finalizations.append(payload)

    client = SimpleNamespace(mark_running=lambda **kw: None, finalize=finalize)
    if enabled:
        client.append_progress = events.append
    monkeypatch.setattr(worker, "execute_job", execute)
    claim = {"job": {"jobId": "job", "workloadType": "x_tts_explain"},
             "attempt": {"attemptId": "attempt", "leaseToken": "token"}}
    asyncio.run(worker.process_claim(client=client, settings=SimpleNamespace(), claim=claim,
                                     hooks={"heartbeat": heartbeat}))
    assert len(finalizations) == (0 if outcome == "lost" else 1)
    if not enabled:
        assert not events
    assert current_progress().__class__.__name__ == "NullProgress"


def test_progress_transport_failure_does_not_fail_claim(monkeypatch):
    monkeypatch.setenv("TG_TOOLS_PROGRESS_ENABLED", "1")
    def unavailable(payload):
        # Permanent rejection exercises failure isolation without retry waits.
        from tts_worker.runtime_client import RuntimeClientError
        raise RuntimeClientError("unavailable", status=404)
    monkeypatch.setattr(worker, "execute_job", lambda *a, **k: (
        current_progress().emit("write_explanation", "started") and {"ok": True}))
    finalized = []
    client = SimpleNamespace(mark_running=lambda **kw: None, append_progress=unavailable, finalize=finalized.append)
    claim = {"job": {"jobId": "job"}, "attempt": {"attemptId": "attempt", "leaseToken": "token"}}
    asyncio.run(worker.process_claim(client=client, settings=SimpleNamespace(), claim=claim,
                                     hooks={"heartbeat": Heartbeat()}))
    assert finalized[0]["outcome"] == "succeeded"


@pytest.mark.parametrize("findings,steps", [(0, ["write_explanation", "review_explanation"]),
                                          (1, ["write_explanation", "review_explanation", "revise_explanation"])])
def test_reader_stages(tmp_path, monkeypatch, findings, steps):
    class Thread:
        session = "session"
        def __init__(self, *a, **k): pass
        def send(self, step, content):
            text = (f"=== FINDINGS: {findings} ===" if step == "review" else
                    "=== TITLE ===\nTitle\n=== SYNOPSIS ===\nSummary\n=== SCRIPT ===\n## Start\nHello there.\n")
            if step == "revise":
                text += "=== RULINGS ===\nAccepted.\n"
            return v4.Turn(step, "role", text, {}, "model", "high", 1, None, "session")
    monkeypatch.setattr(v4, "ModelThread", Thread)
    events = []
    sink = reporter(events)
    try:
        with use_progress(sink):
            v4.run_reader("source", tmp_path)
        sink.flush()
    finally:
        sink.close()
    assert [e["stage"] for e in events if e["kind"] == "started"] == steps
    assert [e["stage"] for e in events if e["kind"] == "completed"] == steps


@pytest.mark.parametrize("fail", [False, True])
def test_speech_counts_and_interruption(tmp_path, monkeypatch, fail):
    def synthesize(text, out, *, report, **kw):
        report(0, 2)
        report(1, 2)
        if fail:
            raise speech_gemini.SpeechError("model_transient", "bad")
        report(2, 2)
        return speech_gemini.SpeechResult(tmp_path / "audio.mp3", [{}, {}], 10)
    monkeypatch.setattr(speech_gemini, "_synthesize_speech", synthesize)
    events = []
    sink = reporter(events)
    try:
        with use_progress(sink):
            if fail:
                with pytest.raises(speech_gemini.SpeechError):
                    speech_gemini.synthesize_speech("text", tmp_path, provider=None)
            else:
                speech_gemini.synthesize_speech("text", tmp_path, provider=None)
        sink.flush()
    finally:
        sink.close()
    assert len({e["occurrence"] for e in events}) == 1
    assert events[-1]["kind"] == ("interrupted" if fail else "completed")
    assert [e["facts"]["completed"] for e in events if e["kind"] == "advanced"] == ([0, 1] if fail else [0, 1, 2])


def test_progress_request_has_short_timeout(monkeypatch):
    from tts_worker.runtime_client import RuntimeClient
    calls = []
    client = RuntimeClient("http://controller")
    monkeypatch.setattr(client, "_request", lambda *args, **kw: calls.append((args, kw)))
    client.append_progress({"jobId": "job"})
    assert calls[0][0] == ("POST", "/v1/jobs/job/progress")
    assert calls[0][1]["timeout"] == 5
