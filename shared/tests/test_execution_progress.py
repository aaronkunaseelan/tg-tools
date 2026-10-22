"""Execution authority: progress reporting, attempt lease, supervised subprocess.

progress (pure, injected send)
| send behaviour            | outcome                                                      |
| first send times out      | same eventId retried; stage that raised reports interrupted  |
| 20 events, capacity 3     | 3 queued, 17 dropped; tracked work still returns its value   |
lease (injected heartbeat)
| heartbeat answers         | outcome                                                      |
| 409                       | LeaseLost at the next check                                  |
| timeout                   | tolerated until the lease deadline passes, then LeaseLost   |
run_with_authority: a real `sleep 60` child is stopped within seconds once authority is lost
"""
import time

import pytest

from tg_tools_common.lease import AttemptLease, LeaseLost
from tg_tools_common.progress import ProgressReporter, tracked_stage, use_progress
from tg_tools_common.subprocesses import run_with_authority


def reporter(send, **kw):
    return ProgressReporter(send=send, job_id="job", attempt_id="attempt", lease_token="token", **kw)


def test_progress():
    calls = []

    def send(event):
        calls.append(event)
        if len(calls) == 1:
            raise TimeoutError()
    sink = reporter(send).start()
    try:
        with use_progress(sink), pytest.raises(ValueError), sink.stage("download_audio"):
            raise ValueError("not completed")
        sink.flush(timeout=4)
    finally:
        sink.close()
    assert calls[0]["eventId"] == calls[1]["eventId"]
    assert [c["kind"] for c in calls] == ["started", "started", "interrupted"]

    bounded = reporter(lambda event: None, capacity=3)
    for i in range(20):
        bounded.emit("chunks", "advanced", completed=i)
    assert (len(bounded._queue), bounded.dropped) == (3, 17)
    with use_progress(bounded):
        assert tracked_stage("analysis")(lambda: 42)() == 42


class Refused(Exception):
    status_code = 409


def lease(error):
    def heartbeat(**kwargs):
        raise error
    client = type("Client", (), {"heartbeat": staticmethod(heartbeat)})()
    return AttemptLease(client=client, job_id="j", attempt_id="a", lease_token="t", lease_seconds=120)


def test_lease():
    refused = lease(Refused())
    refused.beat_once()
    with pytest.raises(LeaseLost):
        refused.check()
    transient = lease(TimeoutError())
    transient.beat_once()
    transient.check()
    transient.deadline = time.monotonic() - 1
    with pytest.raises(LeaseLost):
        transient.check()


def test_supervised_process_stops_on_authority_loss():
    checks = []

    def check():
        checks.append(True)
        if len(checks) > 1:
            raise LeaseLost("lost")
    started = time.monotonic()
    with use_progress(reporter(lambda event: None, check=check)), pytest.raises(LeaseLost):
        run_with_authority(["/bin/sh", "-c", "sleep 60"], timeout=120)
    assert time.monotonic() - started < 7
