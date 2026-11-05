"""F-145: the worker keeps its controller heartbeat fresh while a long job runs.

The idle loop cannot beat during a job, and the controller marks a worker stale
after ~120 s, so the lease thread carries a "busy" beat for the job's duration.
"""
import asyncio
import time
from functools import partial
from types import SimpleNamespace

import runtime_worker
from tg_tools_common import lease as lease_module


def test_busy_heartbeats_while_a_job_runs_and_stop_after(monkeypatch):
    beats, renewals = [], []
    client = SimpleNamespace(
        heartbeat=lambda **kw: renewals.append(kw) or {},
        heartbeat_worker=lambda **kw: beats.append(kw) or {})

    async def long_job(**kwargs):
        await asyncio.sleep(0.4)

    monkeypatch.setattr(runtime_worker, "_process_claim_inner", long_job)
    # 7200 s lease -> 60 s lease interval -> 30 s worker beat; compress time, keep the structure.
    monkeypatch.setattr(runtime_worker, "AttemptLease",
                        partial(lease_module.AttemptHeartbeat, sleeper=lambda s: time.sleep(0.02)))
    claim = {"job": {"jobId": "job-1"}, "attempt": {"attemptId": "a", "leaseToken": "t"}}
    asyncio.run(runtime_worker.process_claim(client=client, settings=None, bot=None, claim=claim, worker_id="rss-worker"))

    assert len(beats) >= 5, "worker heartbeat must repeat for the whole job"
    assert all(b == {"worker_id": "rss-worker", "metadata_json": {"phase": "busy", "jobId": "job-1"}} for b in beats)
    assert len(renewals) == len(beats) // 2, "lease still renews at its own (every second tick) cadence"
    settled = len(beats)
    time.sleep(0.1)
    assert len(beats) == settled, "no busy beats once the job is over; the idle loop takes over"


def test_worker_beat_failure_never_costs_the_lease():
    renewals = []
    client = SimpleNamespace(heartbeat=lambda **kw: renewals.append(kw) or {})

    def refused():
        raise TimeoutError("controller slow")
    beat = lease_module.AttemptHeartbeat(client=client, job_id="j", attempt_id="a", lease_token="t", lease_seconds=7200,
                                         worker_beat=refused, sleeper=lambda s: time.sleep(0.01)).start()
    time.sleep(0.15)
    beat.stop()
    assert renewals and not beat.lost


def test_transcribe_worker_renews_its_lease_and_beats_for_a_job_longer_than_the_lease(monkeypatch):
    """The transcribe worker never renewed its 1800 s lease nor beat mid-job (same F-145 gap)."""
    import transcribe_runtime_worker as tw

    beats, renewals = [], []
    client = SimpleNamespace(
        heartbeat=lambda **kw: renewals.append(kw) or {},
        heartbeat_worker=lambda **kw: beats.append(kw) or {})

    async def long_job(**kwargs):
        await asyncio.sleep(0.4)  # stands for a job that outlives TRANSCRIBE_LEASE_SECONDS

    monkeypatch.setattr(tw, "_execute_claim", long_job)
    monkeypatch.setattr(tw, "AttemptLease", partial(lease_module.AttemptHeartbeat, sleeper=lambda s: time.sleep(0.02)))
    claim = {"job": {"jobId": "job-2"}, "attempt": {"attemptId": "a", "leaseToken": "t"}}
    asyncio.run(tw.process_claim(client=client, settings=None, bot=None, claim=claim, worker_id="transcribe-worker"))

    assert len(beats) >= 5
    assert all(b == {"worker_id": "transcribe-worker", "metadata_json": {"phase": "busy", "jobId": "job-2"}} for b in beats)
    assert len(renewals) == len(beats) // 2 >= 2, "lease keeps renewing across the whole job"
    assert all(r["lease_duration_sec"] == tw.TRANSCRIBE_LEASE_SECONDS and r["lease_token"] == "t" for r in renewals)
    settled = len(beats)
    time.sleep(0.1)
    assert len(beats) == settled
