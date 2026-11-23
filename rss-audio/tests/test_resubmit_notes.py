"""resubmit_notes.py: a released unit re-submits a dead notes job (F-144).

| case                       | outcome                                                                    |
| dead notes job, :r2        | the same job submitted with `:r2` on idempotencyKey and effectKey; one line  |
| a short job id (8 hex)     | found by its leading characters, the same submit                            |
| a job that is not dead     | exit 3, nothing submitted                                                   |
| a job that is not notes    | exit 3, nothing submitted                                                   |
| bad argument (abc, ...)    | exit 2, nothing read, nothing submitted                                     |
| the unit                   | oneshot, the worker's WorkingDirectory/Environment/EnvironmentFile, %I       |
``notes job <id> <action> <key>`` is the whole of what is printed: no environment value, no token.
"""
import io
import re
from pathlib import Path

import pytest

import resubmit_notes as rn

REPO = Path(__file__).resolve().parents[2]
JOB_ID = "21cb2f8e-7d31-4a0e-9c1f-6d2b3a4c5e6f"          # the PANW notes job's id, dead since 21 Nov
NEW_ID = "60024008-1b2c-4d3e-8f40-5a6b7c8d9e0f"
SLUG = "2026-11-19-panw-ignite-2026-9d2c4a1f0b7e"
KEY = f"rss-audio:notes:{SLUG}:v1"
EFFECT = f"rss-audio:feed-notes:{SLUG}:v1"


def job(**over):
    base = {
        "jobId": JOB_ID, "status": "dead", "producerSystem": "rss-audio",
        "producerRef": f"{SLUG}:notes", "resultOwner": "rss-audio",
        "resultRoute": "telegram.rss-audio.confirmation.v1", "workloadType": "rss_publish",
        "queueClass": "foreground", "executionProfile": "cpu_media", "executionShape": "local_bounded",
        "priority": 50, "inputItemCount": 1, "maxAttempts": 3,
        "idempotencyKey": KEY, "effectKey": EFFECT,
        "inputJson": {"kind": "rss", "inputKind": "notes", "feed": "audio", "slug": SLUG, "version": 1},
        "outputContractJson": {"version": 1, "result": "episode notes and manifest, no delivery"},
        "lastErrorClass": "notes", "lastErrorText": "mentions: still invalid",
    }
    return {**base, **over}


class Controller:
    """The two reads and the one write, with no network: get_job, list_jobs (a short id), submit_job."""

    def __init__(self, record=None):
        self.record = record if record is not None else job()
        self.calls, self.submitted = [], []

    def get_job(self, *, job_id):
        self.calls.append(("get_job", job_id))
        assert job_id == JOB_ID
        return {"job": self.record}

    def list_jobs(self, *, limit=100, **_kw):
        self.calls.append(("list_jobs", limit))
        return {"items": [self.record], "count": 1}

    def submit_job(self, payload):
        self.calls.append(("submit_job", payload["idempotencyKey"]))
        self.submitted.append(payload)
        return {"action": "created", "job": {"jobId": NEW_ID, "status": "pending"}}


def run(controller, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = rn.main(list(argv), settings=object(), client=controller, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_the_keys_get_the_suffix_and_nothing_else_changes():
    controller = Controller()
    code, out, err = run(controller, f"{JOB_ID}:r2")
    assert code == 0 and err == ""
    (payload,) = controller.submitted
    assert payload["idempotencyKey"] == f"{KEY}:r2" and payload["effectKey"] == f"{EFFECT}:r2"
    original = job()
    assert {k: v for k, v in payload.items() if k not in {"idempotencyKey", "effectKey"}} == {
        k: v for k, v in original.items()
        if k in {"producerSystem", "producerRef", "resultOwner", "resultRoute", "workloadType", "queueClass",
                 "executionProfile", "executionShape", "priority", "inputItemCount", "inputJson",
                 "outputContractJson", "maxAttempts"}}
    assert out == f"notes job {NEW_ID} created {KEY}:r2\n"


def test_a_short_job_id_is_found_by_its_leading_characters():
    controller = Controller()
    code, out, _ = run(controller, "21cb2f8e:r1")
    assert code == 0 and controller.submitted[0]["idempotencyKey"] == f"{KEY}:r1"
    assert [name for name, *_ in controller.calls] == ["list_jobs", "submit_job"]


def test_a_job_that_is_not_dead_is_refused_and_nothing_is_submitted():
    for status in ("retryable", "succeeded", "leased", "pending", ""):
        controller = Controller(job(status=status))
        code, out, err = run(controller, f"{JOB_ID}:r2")
        assert (code, out, controller.submitted) == (3, "", [])
        assert "is not dead" in err


def test_a_job_that_is_not_notes_is_refused():
    controller = Controller(job(idempotencyKey=f"rss-audio:rebuild:{SLUG}:v1",
                                effectKey=f"rss-audio:feed-rebuild:{SLUG}:v1"))
    code, out, err = run(controller, f"{JOB_ID}:r2")
    assert (code, out, controller.submitted) == (3, "", [])
    assert "not a notes job" in err


@pytest.mark.parametrize("argv", [
    ("abc",),                 # no suffix at all
    ("60024008:x",),          # a suffix that is not rN
    ("zz:r2",),               # a job id that is not hex
    ("21cb2f8e:r",),          # a suffix with no number
    ("21cb2f8e:r2", "x"),     # two arguments
    (),                       # none
])
def test_a_bad_argument_exits_2_before_any_request(argv):
    controller = Controller()
    code, out, err = run(controller, *argv)
    assert (code, out) == (2, "")
    assert err.startswith("resubmit_notes: ")          # named, never a traceback or a value
    assert controller.calls == [] and controller.submitted == []


def test_nothing_token_like_is_printed():
    """The environment holds the bearer token and the B2 keys: the line names the job and its key only."""
    controller = Controller()
    code, out, _ = run(controller, f"{JOB_ID}:r2")
    assert code == 0 and re.fullmatch(r"notes job \S+ \S+ \S+\n", out)
    assert out.count(" ") == 4 and len(out.splitlines()) == 1


def directives(unit: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in unit.read_text().splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        if sep:
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def sections(unit: Path) -> list[str]:
    return [line.strip()[1:-1] for line in unit.read_text().splitlines()
            if line.strip().startswith("[") and line.strip().endswith("]")]


def test_the_unit_is_a_oneshot_with_the_workers_environment():
    unit = REPO / "systemd/rss-audio-notes-resubmit@.service"
    worker = directives(REPO / "systemd/rss-audio-runtime-worker.service")
    d = directives(unit)
    assert sections(unit) == ["Unit", "Service"]          # no [Install]: nothing enables it at boot
    assert d["Type"] == ["oneshot"] and "Restart" not in d and "RestartSec" not in d
    assert d["WorkingDirectory"] == worker["WorkingDirectory"]
    assert d["Environment"] == worker["Environment"]      # no PYTHONPATH of its own (a stale release)
    assert d["EnvironmentFile"] == worker["EnvironmentFile"] and len(d["EnvironmentFile"]) == 3
    assert d["ExecStart"] == ["%h/.local/lib/tg-tools/runtime/bin/python resubmit_notes.py %I"]
    assert d["Description"] and "notes" in d["Description"][0]
