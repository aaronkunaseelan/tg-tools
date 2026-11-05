"""`"delivery": "none"` jobs (scheduled sources): no Telegram confirmation, failures still visible.

| job input                  | worker                                         | result projector                    |
| delivery none, success     | INFO log, no append_delivery, no ERROR         | result consumed, nothing appended   |
| delivery none, terminal    | finalize terminal_error with errorClass, no    | result consumed, nothing appended   |
|                            | append_delivery                                |                                     |
| no messageRef, delivery on | ERROR log (as today)                           | raises, result left unconsumed      |
| Telegram job               | confirmation appended exactly as before        | confirmation appended, consumed     |
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

import runtime_worker
from pipeline import PipelineError
from result_projector import RSSResultProjector

REF = {"chat_id": 1, "message_id": 2, "message_thread_id": 3}


class Client:
    def __init__(self, jobs=None, results=()):
        self.deliveries, self.finalized, self.consumed = [], [], []
        self.jobs, self.results = jobs or {}, list(results)

    def append_delivery(self, payload): self.deliveries.append(payload)
    def mark_running(self, **kw): return {}
    def finalize(self, payload):
        self.finalized.append(payload)
        return {"result": {"resultId": "r1"}}
    def list_results(self, **kw): return self.results
    def get_job(self, *, job_id): return {"job": self.jobs[job_id]}
    def consume_result(self, *, result_id, result_owner): self.consumed.append(result_id)


def deliver(client, input_json):
    asyncio.run(runtime_worker._deliver(client, job_id="j", input_json=input_json, finalized={}, html_text="done"))


def test_worker_skips_confirmation_without_an_error(caplog):
    client = Client()
    with caplog.at_level(logging.INFO):
        deliver(client, {"delivery": "none"})
    assert client.deliveries == [] and not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("delivery=none" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)


def test_worker_still_logs_an_error_for_a_job_that_wanted_delivery_and_has_no_target(caplog):
    client = Client()
    with caplog.at_level(logging.INFO):
        deliver(client, {})
    assert client.deliveries == [] and [r for r in caplog.records if r.levelno == logging.ERROR]


def test_telegram_job_confirmation_is_unchanged():
    client = Client()
    deliver(client, {"messageRef": REF, "sourceKey": "tg:rss:1:2"})
    (payload,) = client.deliveries
    assert payload["deliveryKey"] == "rss-audio:telegram-confirmation:tg:rss:1:2" and payload["priority"] == 60


def claim(input_json):
    job = {"jobId": "j1", "inputJson": input_json}
    return {"job": job, "attempt": {"attemptId": "a1", "leaseToken": "t"}}


@pytest.mark.parametrize("retryable, outcome", [(False, "terminal_error"), (True, "retryable_error")])
def test_failure_is_visible_in_the_controller_result_not_in_telegram(monkeypatch, tmp_path, retryable, outcome):
    async def fail(*args, **kwargs):
        raise PipelineError("window_not_show", "opening is not the show", retryable=retryable)
    monkeypatch.setattr(runtime_worker, "execute_runtime_job", fail)
    monkeypatch.setattr(runtime_worker, "job_work_dir", lambda root, job_id: tmp_path)
    client = Client()
    asyncio.run(runtime_worker._process_claim_inner(
        client=client, settings=SimpleNamespace(work_dir=tmp_path), bot=None, claim=claim({"delivery": "none"})))
    (final,) = client.finalized
    assert (final["outcome"], final["errorClass"]) == (outcome, "window_not_show")
    assert final["resultJson"]["ok"] is False and client.deliveries == []


def test_success_finalizes_without_delivery(monkeypatch, tmp_path):
    async def ok(*args, **kwargs):
        return runtime_worker.RuntimeExecutionResult("slug", "Title", "https://h/s.mp3", 5, "<b>done</b>")
    monkeypatch.setattr(runtime_worker, "execute_runtime_job", ok)
    monkeypatch.setattr(runtime_worker, "job_work_dir", lambda root, job_id: tmp_path)
    client = Client()
    asyncio.run(runtime_worker._process_claim_inner(
        client=client, settings=SimpleNamespace(work_dir=tmp_path), bot=None, claim=claim({"delivery": "none"})))
    assert client.finalized[0]["outcome"] == "succeeded" and client.deliveries == []


def result(job_id, kind="succeeded"):
    return {"resultId": f"res-{job_id}", "jobId": job_id, "resultKind": kind,
            "resultJson": {"completionHtml": "Posted" if kind == "succeeded" else "FAILED"}}


def test_projector_consumes_a_no_delivery_result_without_appending():
    jobs = {"s": {"jobId": "s", "resultOwner": "rss-audio", "inputJson": {"delivery": "none"}},
            "t": {"jobId": "t", "resultOwner": "rss-audio", "inputJson": {"messageRef": REF}},
            "d": {"jobId": "d", "resultOwner": "rss-audio", "inputJson": {"delivery": "none"}}}
    client = Client(jobs, [result("s"), result("t"), result("d", "dead")])
    assert RSSResultProjector(client).project_once() == 3
    assert client.consumed == ["res-s", "res-t", "res-d"]
    assert [d["jobId"] for d in client.deliveries] == ["t"]


def test_projector_still_leaves_a_targetless_telegram_result_unconsumed():
    client = Client({"x": {"jobId": "x", "resultOwner": "rss-audio", "inputJson": {}}}, [result("x")])
    assert RSSResultProjector(client).project_once() == 0 and client.consumed == []
