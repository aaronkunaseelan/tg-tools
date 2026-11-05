"""rss-audio-jobs: the rss-audio jobs and workers on the runtime controller. Read only.

One line per job (newest first): short id, kind, episode, status, attempts, age, last error. Then counts by
kind x status and the workers that run rss-audio work. ``--job ID`` shows one job in full, ``--json`` the same
data as one object. Only GET requests are made, with the settings the ``rss-audio-runtime-worker`` unit runs
with. A failed read prints ``unknown: <reason>`` and exits 1, never an empty table. Nothing printed holds a URL,
a lease token or a token-like string of 28+ characters (it is cut to its first 8 characters and ``…``).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any

STATUSES = ("pending", "leased", "retryable", "succeeded", "dead", "cancelled")
WORKLOADS = {"rss_publish", "transcribe_audio"}       # what the rss-audio workers claim
KEY = re.compile(r"^rss-audio:([a-z_]+):(.+)$")        # notes / page / rebuild jobs carry their kind in the key
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
SLUG = re.compile(r"^(?=.*[a-z]{4,})[a-z0-9]+(?:[-:_][a-z0-9]+){2,}$")   # worker ids, slugs, job keys: words, not secrets
URL = re.compile(r"https?://\S+")
TOKEN = re.compile(r"\S{28,}")
SECRET_FIELDS = {"leaseToken"}
SCAN = 500                                              # jobs read for the summary (the list shows --limit of them)


def redact(text: Any) -> str:
    """No URL, no lease token and no 28+ character token survives. Kept: a job id (UUID, the handle) and a lowercase
    slug (a-z, 0-9 and - : _) with a real word in it (worker ids, episode slugs), which random keys and hashes are not."""
    text = URL.sub("<url>", str(text))
    return TOKEN.sub(lambda m: m.group(0) if keep(m.group(0)) else m.group(0)[:8] + "…", text)


def keep(token: str) -> bool:
    return bool(UUID.match(token) or SLUG.match(token))


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k not in SECRET_FIELDS}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return redact(value) if isinstance(value, str) else value


def oneline(text: Any, limit: int) -> str:
    text = " ".join(redact(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def kind_of(job: dict) -> str:
    match = KEY.match(job.get("idempotencyKey") or "")
    if match:
        return match.group(1)
    return {"transcribe_audio": "transcribe", "rss_publish": "publish"}.get(job.get("workloadType"),
                                                                             str(job.get("workloadType") or "?"))


def episode_of(job: dict) -> str:
    payload = job.get("inputJson") if isinstance(job.get("inputJson"), dict) else {}
    title = payload.get("title") or payload.get("slug")
    if title:
        return str(title)
    ref = job.get("producerRef") or job.get("idempotencyKey") or ""
    match = KEY.match(ref) or re.match(r"^tg:()rss-audio:(.+)$", ref)
    ref = match.group(2) if match else ref
    published = str(payload.get("publishedAt") or "")[:10]
    return f"{ref} {published}".strip()


def when(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def age(job: dict, now: datetime) -> str:
    made = when(job.get("createdAt"))
    if not made:
        return "?"
    seconds = max(0, int((now - made).total_seconds()))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def last_error(job: dict) -> str:
    cls, text = job.get("lastErrorClass"), job.get("lastErrorText")
    return oneline(f"{cls}: {text}" if cls and text else cls or text or "", 120)


def line(job: dict, now: datetime) -> str:
    return (f"{str(job.get('jobId'))[:8]}  {kind_of(job):<10} {oneline(episode_of(job), 48):<48} "
            f"{job.get('status') or '?':<9} {job.get('attemptCount', '?')}/{job.get('maxAttempts', '?')}  "
            f"{age(job, now):>4}  {last_error(job)}").rstrip()


def tally(jobs: list[dict]) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter] = {}
    for job in jobs:
        counts.setdefault(kind_of(job), Counter())[job.get("status") or "?"] += 1
    return {kind: dict(c) for kind, c in sorted(counts.items())}


def rss_workers(workers: list[dict]) -> list[dict]:
    """The workers that claim rss-audio work and are not drained (drained ones are a count, not a row)."""
    return [w for w in workers if WORKLOADS & set(w.get("supportedWorkloadTypes") or [])
            and w.get("state") != "drained"]


def worker_row(w: dict) -> dict:
    meta = w.get("metadataJson") if isinstance(w.get("metadataJson"), dict) else {}
    return {"workerId": w.get("workerId"), "state": w.get("state"), "heartbeatFresh": w.get("heartbeatFresh"),
            "heartbeatAgeSec": w.get("heartbeatAgeSec"), "phase": meta.get("phase"),
            "jobId": meta.get("jobId") or meta.get("job_id"), "workloads": w.get("supportedWorkloadTypes")}


class Unknown(Exception):
    pass


def read(what: str, fn):
    try:
        return fn()
    except Exception as exc:   # a failed read is reported, never shown as empty
        raise Unknown(f"{what}: {oneline(exc, 120) or type(exc).__name__}") from None


def live_client():
    try:
        import config
        from runtime_client import RuntimeClient
        settings, _env = config.worker_settings()
        return RuntimeClient(settings)
    except Exception as exc:   # the unit's environment holds credentials: the type only
        raise Unknown(f"rss-audio-runtime-worker settings unreadable ({type(exc).__name__})") from None


def collect(client, statuses: tuple[str, ...], limit: int) -> dict:
    jobs = read("runtime jobs", lambda: client.list_jobs(status=statuses, limit=max(SCAN, limit)))
    items = jobs["items"]
    data = {"total": jobs.get("count", len(items)), "scanned": len(items), "jobs": items[:limit],
            "summary": tally(items), "workers": None, "workersUnknown": None}
    try:
        everything = read("runtime workers", lambda: client.list_workers())["items"]
        data["workers"] = [worker_row(w) for w in rss_workers(everything)]
        data["drained"] = sum(1 for w in everything if w.get("state") == "drained" and WORKLOADS & set(
            w.get("supportedWorkloadTypes") or []))
    except Unknown as exc:
        data["workersUnknown"] = str(exc)
    return data


def render(data: dict, now: datetime, out) -> None:
    jobs = data["jobs"]
    print(f"rss-audio jobs: {len(jobs)} of {data['total']} (newest first)", file=out)
    for job in jobs:
        print(line(job, now), file=out)
    statuses = [s for s in STATUSES if any(s in c for c in data["summary"].values())] + sorted(
        {s for c in data["summary"].values() for s in c} - set(STATUSES))
    print(f"\nby kind x status, newest {data['scanned']} jobs:", file=out)
    print(f"  {'kind':<11}" + "".join(f"{s:>10}" for s in statuses), file=out)
    for kind, counts in data["summary"].items():
        print(f"  {kind:<11}" + "".join(f"{counts.get(s, 0):>10}" for s in statuses), file=out)
    print("\nworkers:", file=out)
    if data["workersUnknown"]:
        print(f"  unknown: {data['workersUnknown']}", file=out)
    for w in data["workers"] or []:
        beat = "fresh" if w["heartbeatFresh"] else "STALE" if w["heartbeatFresh"] is False else "unknown"
        job = str(w["jobId"])[:8] if w["jobId"] else "-"
        print(f"  {w['workerId']}  {w['state']}  heartbeat {beat} {w['heartbeatAgeSec']}s  "
              f"{w['phase'] or '?'}  job {job}", file=out)
    if data["workers"] == [] and not data["workersUnknown"]:
        print("  none registered", file=out)


def render_job(job: dict, out) -> None:
    for key, value in clean(job).items():
        text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value
        print(f"{key}: {text}", file=out)


def find(client, wanted: str) -> dict:
    wanted = wanted.strip()
    if UUID.match(wanted):
        return read("runtime job", lambda: client.get_job(job_id=wanted))["job"]
    rows = read("runtime jobs", lambda: client.list_jobs(limit=2000))["items"]
    hits = [j for j in rows if str(j.get("jobId")).startswith(wanted)] if len(wanted) >= 4 else []
    if len(hits) != 1:
        raise Unknown(f"job {oneline(wanted, 40)}: {'no match' if not hits else f'{len(hits)} matches'} "
                      "(give 4+ leading characters of the id)")
    return hits[0]


def main(argv: list[str] | None = None, client=None, out=None, now: datetime | None = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="The rss-audio jobs and workers on the runtime controller (read only).")
    parser.add_argument("--limit", type=int, default=40, help="jobs to list (default 40)")
    parser.add_argument("--status", default="", help="comma list: " + ",".join(STATUSES))
    parser.add_argument("--job", help="one job in full (its id or 4+ leading characters)")
    parser.add_argument("--json", action="store_true", help="the same data as one JSON object")
    args = parser.parse_args(argv)
    now = now or datetime.now(UTC)
    statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
    bad = [s for s in statuses if s not in STATUSES]
    if bad or args.limit < 1:
        parser.error(f"invalid --status {','.join(bad)}" if bad else "--limit must be at least 1")
    try:
        client = client or live_client()
        if args.job:
            job = find(client, args.job)
            if args.json:
                print(json.dumps({"job": clean(job)}, ensure_ascii=False, indent=1), file=out)
            else:
                render_job(job, out)
            return 0
        data = collect(client, statuses, args.limit)
    except Unknown as exc:
        print(f"unknown: {exc}", file=out)
        return 1
    if args.json:
        print(json.dumps(clean(data), ensure_ascii=False, indent=1), file=out)
    else:
        render(data, now, out)
    return 1 if data["workersUnknown"] else 0


if __name__ == "__main__":
    sys.exit(main())
