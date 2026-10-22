"""Progress describes checkpoint recovery, never changes its execution path."""
from types import SimpleNamespace

import pytest

from tg_tools_common.progress import ProgressReporter, use_progress
from tts_worker import worker
from tg_tools_common.lease import LeaseLost


def setup_execution(tmp_path, monkeypatch, decision):
    acquired = SimpleNamespace(source_hash="hash", source_ref="source", path=tmp_path / "source",
                               mime_type="text/plain")
    checkpoint = SimpleNamespace(
        identity="identity", source={"hash": "hash", "ext": "txt"},
        publication={"title": "Title", "description": "Description", "route": "tts",
                     "episodeKind": "faithful_read", "durationSeconds": 10},
        artifacts={key: SimpleNamespace(path=tmp_path / key) for key in (
            "sourceOriginal", "sourceMarkdown", "sourceMeta", "artifactMarkdown",
            "speechText", "audioMp3", "manifestJson")},
    )
    monkeypatch.setattr(worker, "acquire_source", lambda *a, **kw: acquired)
    monkeypatch.setattr(worker, "execution_fingerprint", lambda **kw: "identity")
    monkeypatch.setattr(worker, "decide_retry", lambda *a: SimpleNamespace(
        decision=decision, checkpoint=checkpoint, episode="installed"))
    monkeypatch.setattr(worker, "result_from_checkpoint", lambda c, ep, **kw: {"episode": ep})
    settings = SimpleNamespace(default_voice="nova", speech_provider="azure", azure_tts_model="model")
    return dict(settings=settings, input_json={"workloadType": "tts_synthesize"}, work=tmp_path,
                source_key="source", attempt_id="attempt", source_fetcher=None, gemini_post=None,
                openai_post=None, pi_runner_factory=None, rclone_runner=None, media_runner=None,
                episode_post=None, episode_get=None, cancel_check=None)


@pytest.mark.parametrize("decision,stage,publish", [
    (worker.RetryDecision.INSTALLED, "publication_checkpoint", False),
    (worker.RetryDecision.PUBLISH_FROM_G, "generation_checkpoint", True),
])
def test_recovery_only_reports_work_actually_performed(tmp_path, monkeypatch, decision, stage, publish):
    kwargs = setup_execution(tmp_path, monkeypatch, decision)
    calls = []
    monkeypatch.setattr(worker, "publish_episode", lambda **kw: calls.append(kw) or "published")
    monkeypatch.setattr(worker, "materialise", lambda *a, **kw: pytest.fail("recovery regenerated source"))
    events = []
    sink = ProgressReporter(send=events.append, job_id="job", attempt_id="attempt", lease_token="token").start()
    try:
        with use_progress(sink):
            worker._execute_job_in_root(**kwargs)
        sink.flush()
    finally:
        sink.close()
    assert bool(calls) == publish
    assert [(e["stage"], e["kind"]) for e in events] == [(stage, "reused")]


def test_extraction_lease_loss_is_not_mapped_to_pipeline_failure(tmp_path, monkeypatch):
    kwargs = setup_execution(tmp_path, monkeypatch, worker.RetryDecision.GENERATE)
    def lost(*a, **kw):
        raise LeaseLost("expired")
    monkeypatch.setattr(worker, "materialise", lost)
    with pytest.raises(LeaseLost):
        worker._execute_job_in_root(**kwargs)


def test_gateway_renders_tts_stages_and_chunk_counts():
    from tg_gateway.job_progress import render_status
    snapshot = {
        "serverTime": "2026-10-11T13:00:00+00:00",
        "job": {"status": "leased", "createdAt": "2026-10-11T12:50:00+00:00"},
        "attempt": {"createdAt": "2026-10-11T12:50:00+00:00",
                    "heartbeatAt": "2026-10-11T13:00:00+00:00", "leaseExpiresAt": "2026-10-11T13:02:00+00:00"},
        "stages": [
            {"stage": "review_explanation", "kind": "completed", "facts": {}},
            {"stage": "synthesize_speech", "kind": "advanced", "startedAt": "2026-10-11T12:59:00+00:00",
             "facts": {"completed": 2, "total": 5, "units": "chunks"}},
        ],
    }
    text = render_status(snapshot)
    assert "Reviewing explanation" in text
    assert "Working: Generating audio" in text
    assert "2/5 chunks complete" in text
    snapshot["job"]["status"] = "succeeded"
    text = render_status(snapshot)
    assert "Processing complete" in text and "Working:" not in text
