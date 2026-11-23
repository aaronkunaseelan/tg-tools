"""Re-submit a dead notes job under a fresh key: ``resubmit_notes.py DEAD_JOB_ID:SUFFIX``.

A dead notes job keeps its idempotency key, so the controller will never run it again: the episode sits
with a script and no notes, and no retry of the parent touches it either (the parent's key is spent too).
This reads the job back and submits the same job under a new key, ``SUFFIX`` (``rN``) appended to
``idempotencyKey`` and ``effectKey``. Nothing else changes: the notes input (feed, slug, version, script
sha, timing, sections) travels unchanged, so the release that is current when this runs is the one that
executes it, and the job does the work the dead one was submitted for.

    systemctl --user start rss-audio-notes-resubmit@21cb2f8e:r1
    rss-audio-jobs                              # the new job, then the episode

Only a job whose status is ``dead`` is re-submitted (exit 3 otherwise), and only a notes job of this
product (``rss-audio:notes:...``). A malformed argument exits 2 before any request; an unreadable job or
a refused submit exits 1. The job id may be given in full or as a unique leading hex run, as
``rss-audio-jobs`` prints it. The environment comes from the unit's EnvironmentFiles, the way the worker
gets it, never from a value typed by hand; nothing printed here holds an environment value or a token,
only the job id, the controller's action and the job's idempotency key.
"""
from __future__ import annotations

import re
import sys

from config import Settings, load_settings
from runtime_client import RuntimeClient

NOTES_KEY = "rss-audio:notes:"          # what a notes job's idempotencyKey starts with (notes_job.notes_job_payload)
SUFFIX = re.compile(r"^r[0-9]+$")
HEX = re.compile(r"^[0-9a-f]+(?:-[0-9a-f]+)*$")      # a job id, or the leading characters of one
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
PREFIX = 6                                            # leading characters that may name a job
USAGE = "usage: resubmit_notes.py DEAD_JOB_ID:SUFFIX   (SUFFIX is rN, e.g. 21cb2f8e:r1)"


class BadArgument(ValueError):
    """Not ``JOB_ID:rN``: nothing was read or submitted (exit 2)."""


class Refused(RuntimeError):
    """The job named is not one this program may re-submit (exit 3)."""


def parse_spec(text: str) -> tuple[str, str]:
    """``JOB_ID:SUFFIX`` -> (job id, suffix), split on the last ``:``. A dead job id is hex."""
    job_id, sep, suffix = str(text).rpartition(":")
    if not sep:
        raise BadArgument(f"{text!r} is not JOB_ID:SUFFIX")
    if not HEX.match(job_id):
        raise BadArgument(f"{job_id!r} is not a job id (hex)")
    if not SUFFIX.match(suffix):
        raise BadArgument(f"{suffix!r} is not a suffix (rN, N a number)")
    return job_id, suffix


def find_job(client, job_id: str) -> dict:
    """The job, by full id or by a unique leading hex run (``rss-audio-jobs`` prints the first 8)."""
    if UUID.match(job_id):
        return client.get_job(job_id=job_id)["job"]
    if len(job_id) < PREFIX:
        raise Refused(f"job {job_id}: give at least {PREFIX} leading characters of the id")
    rows = client.list_jobs(limit=2000)["items"]
    hits = [job for job in rows if str(job.get("jobId") or "").startswith(job_id)]
    if len(hits) != 1:
        raise Refused(f"job {job_id}: {'no match' if not hits else f'{len(hits)} matches'} "
                      f"(give {PREFIX}+ leading characters of the id)")
    return hits[0]


def resubmit_payload(job: dict, suffix: str) -> dict:
    """The payload the released notes_job.notes_job_payload builds from the dead job (a parent stub read off it,
    its inputJson unchanged), with ``:SUFFIX`` appended to both keys: the D-85 one-off, unchanged."""
    import json

    import notes_job

    key = str(job.get("idempotencyKey") or "")
    if not key.startswith(NOTES_KEY):
        raise Refused(f"job {job.get('jobId')} is not a notes job (idempotencyKey {key or 'missing'}); "
                      "resubmit_notes re-submits notes only")
    raw = job["inputJson"]
    notes = dict(json.loads(raw) if isinstance(raw, str) else raw)
    ref = str(job.get("producerRef") or "")
    parent = {"producerRef": ref[:-len(":notes")] if ref.endswith(":notes") else ref,
              "resultRoute": job.get("resultRoute"),
              "priority": int(job.get("priority") or 0) + notes_job.NOTES_BELOW_PARENT}
    payload = notes_job.notes_job_payload(parent, notes)
    for field in ("idempotencyKey", "effectKey"):
        payload[field] = f"{payload[field]}:{suffix}"
    return payload


def main(argv: list[str] | None = None, *, settings: Settings | None = None, client=None,
         out=None, err=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out, err = out or sys.stdout, err or sys.stderr
    if len(argv) != 1:
        print(f"resubmit_notes: {USAGE}", file=err)
        return 2
    try:
        job_id, suffix = parse_spec(argv[0])
    except BadArgument as exc:
        print(f"resubmit_notes: {exc}", file=err)
        return 2
    if settings is None:                   # the environment holds credentials: name the type only
        try:
            settings = load_settings()
        except Exception as exc:
            print(f"resubmit_notes: settings unreadable ({type(exc).__name__})", file=err)
            return 1
    client = client or RuntimeClient(settings)
    try:
        job = find_job(client, job_id)
        status = str(job.get("status") or "")
        if status != "dead":
            raise Refused(f"job {job.get('jobId') or job_id} is not dead (status={status or 'unknown'})")
        payload = resubmit_payload(job, suffix)
        submitted = client.submit_job(payload) or {}
    except Refused as exc:
        print(f"resubmit_notes: {exc}", file=err)
        return 3
    except Exception as exc:
        print(f"resubmit_notes: {type(exc).__name__}: {exc}", file=err)
        return 1
    new = submitted.get("job") or {}
    print(f"notes job {new.get('jobId')} {submitted.get('action') or 'submitted'} {payload['idempotencyKey']}",
          file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
