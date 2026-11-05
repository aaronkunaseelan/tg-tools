"""rss-audio-jobs (jobs_view.py): kind derivation, redaction, summary counts, API failure (fake client only).

| case            | proves                                                                                  |
| kinds           | notes/page/rebuild come from the idempotency key; the rest from the workload            |
| redaction       | no URL, lease token or 28+ character token survives in the list, --job or --json         |
| summary         | counts by kind x status add up to the jobs scanned; drained workers are not rows         |
| workers         | both publish workers are listed with heartbeat and current job; others are left out      |
| api failure     | jobs or one job unreadable: `unknown: <reason>`, exit 1; workers unreadable: exit 1      |
| read only       | the client is only asked to list/get                                                     |
"""
import io
import json
from datetime import UTC, datetime

import jobs_view as jv
import pytest

NOW = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)
TOKEN = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7"     # 34 characters: must never be printed whole (a fake fixture)  gitleaks:allow
URL = "https://edge.example/surveillance/episodes/x.mp3?sig=" + TOKEN
JOB_ID = "cd5f6c95-d3f6-40d6-b123-0123456789ab"


def job(key, workload="rss_publish", status="pending", title="Bloomberg Surveillance TV 11/2/2026", n=0, **extra):
    return {"jobId": f"{n:08x}-d3f6-40d6-b123-0123456789ab", "idempotencyKey": key, "producerRef": key,
            "workloadType": workload, "status": status, "attemptCount": 1, "maxAttempts": 3,
            "createdAt": "2026-11-04T20:00:00Z", "leaseToken": "LEASE-" + TOKEN, "lastErrorClass": None,
            "lastErrorText": None, "inputJson": {"title": title, "originalUrl": URL} if title else {}, **extra}


class Fake:
    def __init__(self, jobs=(), workers=(), fail=None):
        self.jobs, self.workers, self.fail, self.calls = list(jobs), list(workers), fail, []

    def list_jobs(self, **kw):
        self.calls.append(("list_jobs", kw))
        if self.fail == "jobs":
            raise RuntimeError("runtime request failed: [Errno 111] Connection refused")
        return {"items": self.jobs, "count": len(self.jobs)}

    def list_workers(self, **kw):
        self.calls.append(("list_workers", kw))
        if self.fail == "workers":
            raise RuntimeError("boom " + URL)
        return {"items": self.workers, "count": len(self.workers)}

    def get_job(self, *, job_id):
        self.calls.append(("get_job", job_id))
        if self.fail == "job":
            raise RuntimeError("runtime request failed (status=404)")
        return {"job": next(j for j in self.jobs if j["jobId"] == job_id)}


def worker(wid, workloads=("rss_publish",), state="active", fresh=True, phase="busy", job_id=None):
    return {"workerId": wid, "state": state, "supportedWorkloadTypes": list(workloads), "heartbeatFresh": fresh,
            "heartbeatAgeSec": 12, "metadataJson": {"phase": phase, "jobId": job_id}}


JOBS = [
    job("rss-audio:notes:2026-11-02-bloomberg-surveillance-tv-455780e2d6ca:v1", status="retryable", n=1,
        lastErrorClass="notes", lastErrorText="mentions: still invalid\nafter one repair " + "x" * 200),
    job("rss-audio:page:2026-11-03-bloomberg-surveillance-tv-37d48d123b73:v1:l2", status="succeeded", n=2),
    job("rss-audio:rebuild:2026-10-29-bloomberg-surveillance-radio-f984ff4d1541:v2", status="succeeded", n=3),
    job("surveillance-tv:JQZkM3iXIpw", status="succeeded", n=4),
    job("surveillance-tv:AAAAAAAAAAA", status="succeeded", n=5),
    job("tg:rss-audio:-1003844302747:1016", workload="transcribe_audio", status="dead", title=None, n=6,
        lastErrorClass="source_unavailable"),
]
WORKERS = [worker("rss-audio-runtime-worker", job_id=JOB_ID), worker("rss-audio-runtime-worker-2"),
           worker("transcribe-runtime-worker-transcribe", ("transcribe_audio",), phase="idle"),
           worker("tts-worker-1", ("tts_synthesize",)),
           worker("deploy-canary-rss", state="drained")]


def run(client, *argv):
    out = io.StringIO()
    code = jv.main(list(argv), client=client, out=out, now=NOW)
    return code, out.getvalue()


def test_kinds():
    assert [jv.kind_of(j) for j in JOBS] == ["notes", "page", "rebuild", "publish", "publish", "transcribe"]
    assert jv.kind_of({"workloadType": "meeting_summary"}) == "meeting_summary"
    assert jv.episode_of(JOBS[0]) == "Bloomberg Surveillance TV 11/2/2026"
    assert jv.episode_of(JOBS[5]) == "-1003844302747:1016"   # no title: the key's readable part
    assert jv.age(JOBS[0], NOW) == "2h"
    assert jv.redact(TOKEN + " " + "a" * 40 + " " + "deadbeef" * 5) == "A1b2C3d4… aaaaaaaa… deadbeef…"
    assert jv.redact("rss-audio-runtime-worker-2 " + JOB_ID) == "rss-audio-runtime-worker-2 " + JOB_ID


@pytest.mark.parametrize("argv", [(), ("--json",)])
def test_redaction_everywhere(argv):
    code, out = run(Fake(JOBS, WORKERS), *argv)
    assert code == 0
    for text in (out, run(Fake(JOBS, WORKERS), "--job", JOBS[0]["jobId"], *argv)[1]):
        assert TOKEN not in text and "LEASE-" not in text and "edge.example" not in text
        assert not [w for w in text.split() if len(w.strip('",:[]{}')) >= 28 and not jv.keep(w.strip('",:[]{}'))]


def test_job_detail_is_redacted_and_complete():
    code, out = run(Fake(JOBS, WORKERS), "--job", JOBS[0]["jobId"])
    assert code == 0 and "leaseToken" not in out and "attemptCount: 1" in out
    assert "originalUrl" in out and "<url>" in out
    code, out = run(Fake(JOBS, WORKERS), "--job", JOBS[1]["jobId"][:8])    # a prefix resolves
    assert code == 0 and "rss-audio:page:" in out
    code, out = run(Fake(JOBS, WORKERS), "--job", "zz")
    assert code == 1 and out.startswith("unknown:")


def test_list_line_and_error_is_one_line_120():
    code, out = run(Fake(JOBS, WORKERS))
    first = out.splitlines()[1]
    assert first.startswith("00000001  notes") and "retryable" in first and "1/3" in first and "2h" in first
    err = first.split("  ")[-1]
    assert err.startswith("notes: mentions: still invalid after one repair") and len(err) <= 120
    assert len([ln for ln in out.splitlines() if ln[:8].strip() and ln.startswith("0000000")]) == len(JOBS)


def test_summary_counts_add_up():
    data = jv.collect(Fake(JOBS, WORKERS), (), 3)
    assert len(data["jobs"]) == 3 and data["scanned"] == data["total"] == 6
    assert sum(n for c in data["summary"].values() for n in c.values()) == 6
    assert data["summary"]["publish"] == {"succeeded": 2} and data["summary"]["notes"] == {"retryable": 1}
    assert data["summary"]["transcribe"] == {"dead": 1}
    out = run(Fake(JOBS, WORKERS))[1]
    assert "notes" in out and "retryable" in out and "publish" in out


def test_workers():
    out = run(Fake(JOBS, WORKERS))[1]
    assert "rss-audio-runtime-worker  active  heartbeat fresh 12s  busy  job cd5f6c95" in out
    assert "rss-audio-runtime-worker-2" in out and "transcribe-runtime-worker-transcribe" in out
    assert "tts-worker-1" not in out and "deploy-canary-rss" not in out
    assert json.loads(run(Fake(JOBS, WORKERS), "--json")[1])["drained"] == 1
    stale = run(Fake(JOBS, [worker("rss-audio-runtime-worker", fresh=False)]))[1]
    assert "heartbeat STALE" in stale


def test_status_filter_goes_to_the_api_and_is_validated():
    fake = Fake(JOBS, WORKERS)
    run(fake, "--status", "pending,leased,retryable")
    assert fake.calls[0][1]["status"] == ("pending", "leased", "retryable")
    with pytest.raises(SystemExit):
        run(Fake(), "--status", "bogus")


def test_api_failure_is_unknown_exit_1():
    for fail, argv in (("jobs", ()), ("jobs", ("--json",)), ("jobs", ("--job", JOB_ID)), ("job", ("--job", JOB_ID))):
        code, out = run(Fake(JOBS, WORKERS, fail=fail), *argv)
        assert code == 1 and out.startswith("unknown: "), (fail, argv, out)
    code, out = run(Fake([], WORKERS, fail="jobs"))
    assert "Connection refused" in out and "jobs" not in out.splitlines()[0].replace("runtime jobs", "")
    code, out = run(Fake(JOBS, WORKERS, fail="workers"))
    assert code == 1 and "unknown: runtime workers" in out and "edge.example" not in out and TOKEN not in out
    assert "newest first" in out     # the jobs that were read are still shown


def test_empty_is_not_hidden_as_healthy():
    code, out = run(Fake([], []))
    assert code == 0 and "0 of 0" in out and "none registered" in out


def test_read_only():
    fake = Fake(JOBS, WORKERS)
    run(fake)
    run(fake, "--job", JOBS[0]["jobId"])
    assert {name for name, _ in fake.calls} <= {"list_jobs", "list_workers", "get_job"}
