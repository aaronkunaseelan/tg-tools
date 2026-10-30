"""Unexpected failures finalize promptly without copying private exception text."""
import asyncio
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from tts_worker import worker, speech_gemini
from tts_worker.result_projector import render_notification
from test_progress import Heartbeat


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("lost", [False, True])
def test_unexpected_claim_failure_finalizes(monkeypatch, enabled, lost):
    monkeypatch.setenv("TG_TOOLS_PROGRESS_ENABLED", "1" if enabled else "0")
    finalized = []
    heartbeat = Heartbeat()
    stopped = []
    heartbeat.stop = lambda: stopped.append(True)

    def execute(*args, **kwargs):
        heartbeat.lost = lost
        raise TimeoutError("PRIVATE https://example.test/?secret=hidden")

    def finalize(payload):
        assert not stopped  # renewal remains active through finalize
        finalized.append(payload)

    monkeypatch.setattr(worker, "execute_job", execute)
    client = SimpleNamespace(mark_running=lambda **kw: None,
                             append_progress=lambda payload: None, finalize=finalize)
    claim = {"job": {"jobId": "job"},
             "attempt": {"attemptId": "attempt", "leaseToken": "token"}}
    asyncio.run(worker.process_claim(client=client, settings=SimpleNamespace(), claim=claim,
                                     hooks={"heartbeat": heartbeat}))
    assert stopped == [True]
    assert len(finalized) == (0 if lost else 1)
    if not lost:
        payload = finalized[0]
        assert payload["outcome"] == "terminal_error"
        assert payload["resultJson"]["stage"] == "worker"
        assert payload["errorText"] == "TimeoutError"
        reply = render_notification("dead", payload["resultJson"])
        assert "worker" in reply and "TimeoutError" in reply
        assert "PRIVATE" not in str(payload) and "https://" not in reply


@pytest.mark.parametrize("provider,seconds", [("azure", 300), ("gemini", 120)])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("during_read", [False, True])
def test_provider_timeout_is_typed_safe_and_not_retried(monkeypatch, provider, seconds, wrapped, during_read):
    calls = []
    def urlopen(*args, **kwargs):
        calls.append(kwargs["timeout"])
        exc = TimeoutError("PRIVATE https://example.test/?secret=hidden")
        if not during_read:
            raise URLError(exc) if wrapped else exc
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args):
                raise URLError(exc) if wrapped else exc
        return Response()
    monkeypatch.setattr(speech_gemini.urllib_request, "urlopen", urlopen)
    obj = (speech_gemini.AzureSpeechProvider(base_url="https://synthetic.test", api_key="synthetic")
           if provider == "azure" else speech_gemini.GeminiSpeechProvider(api_key="synthetic"))
    with pytest.raises(speech_gemini.SpeechError) as error:
        obj.synthesize_pcm("synthetic text")
    assert error.value.error_class == "speech_timeout"
    assert str(error.value) == f"TimeoutError: socket read timed out after {seconds} s from {provider}"
    assert calls == [seconds]


def test_unexpected_execution_stage(monkeypatch, tmp_path):
    # Admission exceptions previously escaped all product failure handlers.
    def unexpected(*args):
        raise OSError("PRIVATE")
    monkeypatch.setattr(worker, "validate_voice", unexpected)
    with pytest.raises(worker.TTSJobError) as error:
        worker.execute_job(SimpleNamespace(work_dir=tmp_path, default_voice="nova", speech_provider="azure"),
                           {"sourceKey": "synthetic"}, attempt_id="attempt")
    assert error.value.stage == "admission"
    assert str(error.value) == "OSError"


@pytest.mark.parametrize("failure_stage", ["speech", "enrichment"])
def test_execution_failure_reaches_reply_with_real_stage(monkeypatch, tmp_path, failure_stage):
    import json
    from test_progress_recovery import setup_execution
    kwargs = setup_execution(tmp_path, monkeypatch, worker.RetryDecision.GENERATE)
    kwargs["settings"].public_base_url = "https://synthetic.test"
    for key, value in {"azure_tts_base_url": "https://synthetic.test", "azure_tts_api_key": "synthetic",
                       "azure_tts_key_cmd": "", "ffmpeg_bin": "ffmpeg", "ffprobe_bin": "ffprobe"}.items():
        setattr(kwargs["settings"], key, value)
    source = tmp_path / "source.md"
    source.write_text("A synthetic paragraph with enough text for extraction. " * 30)
    meta = tmp_path / "source.meta.json"
    meta.write_text(json.dumps({}))
    monkeypatch.setattr(worker, "materialise", lambda *a, **kw: {"markdown_path": source, "meta_path": meta})
    monkeypatch.setattr(worker, "resolve_title", lambda *a, **kw: "Synthetic title")
    def enrich(**kw):
        if failure_stage == "enrichment":
            raise TimeoutError("PRIVATE")
    monkeypatch.setattr(worker, "enrich", enrich)
    def synthesize(*a, **kw):
        raise OSError("PRIVATE https://synthetic.test/?secret=hidden")
    monkeypatch.setattr(worker, "synthesize_with_fallback", synthesize)
    with pytest.raises(worker.TTSJobError) as error:
        worker._execute_job_in_root(**kwargs)
    assert error.value.stage == failure_stage
    finalized = []
    asyncio.run(worker._finalize_failure(client=SimpleNamespace(finalize=finalized.append),
        exc=error.value, job_id="job", attempt_id="attempt", lease_token="token"))
    reply = render_notification("dead", finalized[0]["resultJson"])
    assert f"at {failure_stage}:" in reply
    assert ("TimeoutError" if failure_stage == "enrichment" else "OSError") in reply
    assert "PRIVATE" not in str(finalized) and "https://" not in reply


def test_missing_legacy_failure_is_explicit():
    assert render_notification("dead", {}) == (
        "TTS failed at runtime: failure_details_unavailable — Stage and reason were not recorded")
