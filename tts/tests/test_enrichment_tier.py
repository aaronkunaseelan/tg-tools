"""TTS notes enrichment takes its model tier from the `speech` pipeline of the enrichment tier file.

| tier file                          | job                                                              |
| speech: haiku                      | enrich() gets tier haiku (also after the file changes: next job)  |
| no file / no `speech` / bad tier   | terminal `enrichment_config` at admission, before any acquisition |
"""
import json
from types import SimpleNamespace

import pytest

from test_progress_recovery import setup_execution
from tts_worker import worker
from tts_worker.failure_policy import TERMINAL_CLASSES


def run_until_first_enrichment(tmp_path, monkeypatch):
    tmp_path.mkdir()
    kwargs = setup_execution(tmp_path, monkeypatch, worker.RetryDecision.GENERATE)
    source = tmp_path / "source.md"
    source.write_text("A synthetic paragraph with enough text for extraction. " * 30)
    meta = tmp_path / "source.meta.json"
    meta.write_text(json.dumps({}))
    monkeypatch.setattr(worker, "materialise", lambda *a, **kw: {"markdown_path": source, "meta_path": meta})
    monkeypatch.setattr(worker, "resolve_title", lambda *a, **kw: "Synthetic title")
    seen = []

    def enrich(**kw):
        seen.append(kw)
        raise TimeoutError("stop after the first enrichment")
    monkeypatch.setattr(worker, "enrich", enrich)
    kwargs["settings"].public_base_url = "https://synthetic.test"
    with pytest.raises(worker.TTSJobError):
        worker._execute_job_in_root(**kwargs)
    return seen


def test_notes_run_on_the_speech_tier_and_a_changed_file_applies_to_the_next_job(tmp_path, monkeypatch, enrichment_tiers):
    assert [kw["tier"] for kw in run_until_first_enrichment(tmp_path / "one", monkeypatch)] == ["haiku"]
    enrichment_tiers.write_text(json.dumps({"speech": "sonnet"}))
    assert [kw["tier"] for kw in run_until_first_enrichment(tmp_path / "two", monkeypatch)] == ["sonnet"]


@pytest.mark.parametrize("content", [None, "not json", "[]", "{}", '{"episodes": "haiku"}', '{"speech": "nope"}', '{"speech": 3}'])
def test_unusable_tier_file_fails_the_job_before_any_acquisition(tmp_path, monkeypatch, enrichment_tiers, content):
    if content is None:
        enrichment_tiers.unlink()
    else:
        enrichment_tiers.write_text(content)
    fetched = []
    with pytest.raises(worker.TTSJobError) as error:
        worker.execute_job(SimpleNamespace(work_dir=tmp_path, default_voice="nova", speech_provider="azure"),
                           {"sourceKey": "synthetic"}, attempt_id="attempt",
                           source_fetcher=lambda *a, **kw: fetched.append(1))
    assert (error.value.stage, error.value.error_class) == ("admission", "enrichment_config") and not fetched
    assert "enrichment_config" in TERMINAL_CLASSES
