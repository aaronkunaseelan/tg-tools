"""Five-minute, read-only product health. Readers: supervisor watch and mech-d."""
from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from .contract_validate import problems

ROOT = Path(__file__).resolve().parents[1]
SUMMARY_CONTRACT = json.loads((Path(__file__).parent / "contracts/runtime.health-summary-1.json").read_text())
WORD = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")


def _config(product):
    path = ROOT / ("tts/tts_worker/config.py" if product == "tts" else f"{product}/config.py")
    name = "_telemetry_" + product.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def unit_environment(unit):
    """Read effective config, including EnvironmentFile overrides and PID defaults.

    Never display the environment or exception text: it contains credentials.
    No sessions, providers or product startup functions are invoked.
    """
    pid = int(subprocess.check_output(
        ["systemctl", "--user", "show", unit, "--property=MainPID", "--value"], timeout=3))
    if pid <= 0:
        raise ValueError("unit not running")
    env = dict(part.decode().split("=", 1) for part in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in part)
    return env, pid


def configuration(read_env=unit_environment):
    workers, bounds = [], {}
    for unit, product in (("tts-worker", "tts"), ("rss-audio-runtime-worker", "rss-audio"),
                          ("rss-audio-transcribe-runtime-worker", "rss-audio"),
                          ("meeting-summary-runtime-worker", "meeting-summary")):
        try:
            env, pid = read_env(unit)
            module = _config(product)
            settings = module.load_settings(env) if product == "tts" else module.load_settings(env, pid=pid)
            if product == "tts":
                worker_id = settings.worker_id
                bounds.update({w: settings.lease_seconds for w in settings.workload_types})
            elif product == "rss-audio":
                transcribe = "transcribe" in unit
                worker_id = module.transcribe_worker_id(settings.runtime_worker_id) if transcribe else settings.runtime_worker_id
                bounds["transcribe_audio" if transcribe else "rss_publish"] = (
                    module.TRANSCRIBE_LEASE_SECONDS if transcribe else module.PUBLISH_LEASE_SECONDS)
            else:
                worker_id = settings.runtime_worker_id
                # Generous processing-age alarm, not the renewable five-minute lease.
                bound = settings.max_audio_duration_sec + settings.summary_timeout_sec
                bounds.update({w: bound for w in settings.runtime_claim_workload_types})
                bounds["meeting_note"] = bound  # gateway's admitted workload, even if misconfigured worker claims another
            if not WORD.fullmatch(worker_id):
                raise ValueError("invalid worker id")
            workers.append((unit, worker_id))
        except Exception:
            workers.append((unit, None))
    routes = [route for path in (ROOT / "services").glob("*.json")
              for route in json.loads(path.read_text())["routes"]]
    owners = sorted({r.get("ownerSystem", json.loads(path.read_text())["ownerSystem"])
                     for path in (ROOT / "services").glob("*.json")
                     for r in json.loads(path.read_text())["routes"]})
    workloads = sorted(set(bounds) | {r["workloadType"] for r in routes})
    return workers, bounds, workloads, owners


def handoff_health(read_env=unit_environment):
    import psycopg
    from tg_gateway.settings import DEFAULT_DATABASE_URL
    databases = {read_env(unit)[0].get("TG_GATEWAY_DATABASE_URL") or DEFAULT_DATABASE_URL
                 for unit in ("tg-gateway-rss", "tg-gateway-ms")}
    stuck, newest = False, None
    for dsn in databases:
        with psycopg.connect(dsn, connect_timeout=3, options="-c default_transaction_read_only=on -c statement_timeout=1000") as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT EXISTS (SELECT 1 FROM tg_gateway.source_handoffs
                    WHERE state IN ('pending','staged') AND created_at < now() - interval '15 minutes'),
                    (SELECT EXTRACT(EPOCH FROM now() - created_at)::float8 FROM tg_gateway.source_handoffs
                     ORDER BY created_at DESC LIMIT 1)""")
                bad, age = cur.fetchone()
                stuck |= bad
                if age is not None:
                    newest = age if newest is None else min(newest, age)
    return stuck, newest


def collect(client, workers, bounds, workloads, owners, handoffs=handoff_health, *, now=None):
    now = time.time() if now is None else now
    checks, metrics = [], []

    def check(name, ok, reason=""):
        checks.append({"name": name, "ok": bool(ok), "reason": reason[:160]})

    def metric(name, value, unit, labels=None):
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid metric")
        metrics.append({"name": name, "labels": labels or {}, "value": value, "unit": unit})

    try:
        health = client.health()
        if health["ok"] is not True:
            raise ValueError("health failed")
        check("controller", True)
    except Exception:
        health = None
        check("controller", False, "unknown: controller health unavailable or invalid")
    try:
        heartbeats = health["runtime"]["workerHeartbeats"]
        if not isinstance(heartbeats, list):
            raise ValueError("missing heartbeats")
        if any(not isinstance(w, dict) or not isinstance(w.get("workerId"), str) or
               type(w.get("heartbeatFresh")) is not bool for w in heartbeats):
            raise ValueError("invalid heartbeat")
        by_id = {w["workerId"]: w for w in heartbeats}
        missing = [unit for unit, identity in workers if identity is None or identity not in by_id]
        stale = [unit for unit, identity in workers if identity in by_id and by_id[identity]["heartbeatFresh"] is not True]
        check("workers", not missing and not stale,
              "unknown: worker config or heartbeat missing: " + ",".join(missing) if missing else
              "stale heartbeat: " + ",".join(stale) if stale else "")
    except Exception:
        check("workers", False, "unknown: controller worker heartbeats unavailable or invalid")
    try:
        summary = client.health_summary(workloads=workloads, owners=owners)
        if problems(SUMMARY_CONTRACT, summary) or abs(now - summary["at"]) > 300:  # the summary is read after `now`
            raise ValueError("invalid summary")
        rows = {r["workloadType"]: r for r in summary["workloads"]}
        if len(rows) != len(workloads) or set(rows) != set(workloads) or summary["windowSec"] != 21600:
            raise ValueError("incomplete summary")
        bad, unknown = [], []
        for workload, row in rows.items():
            if not WORD.fullmatch(workload):
                raise ValueError("invalid workload")
            if not row["activeComplete"] or workload not in bounds:
                unknown.append(workload)
            if row["pastDeadline"] or row["expiredLease"] or (row["oldestActiveAgeSec"] is not None and
                    workload in bounds and row["oldestActiveAgeSec"] > bounds[workload]):
                bad.append(workload)
            if row["newestJobAt"] is not None:
                from datetime import datetime
                metric("newest_job_age", max(0, now - datetime.fromisoformat(row["newestJobAt"].replace("Z", "+00:00")).timestamp()), "s", {"workload": workload})
            if row["countsComplete"]:
                for status, count in row["jobsByStatus"].items():
                    if not WORD.fullmatch(status):
                        raise ValueError("invalid status")
                    metric("jobs", count, "count", {"workload": workload, "status": status})
            else:
                unknown.append(workload + ":counts_capped")
        check("stuck_jobs", not bad and not unknown,
              "overdue or lease/runtime bound exceeded: " + ",".join(bad) if bad else
              "unknown: incomplete job reading: " + ",".join(unknown) if unknown else "")
        failures = summary["failures"]
        for failure in failures:
            if not WORD.fullmatch(failure["workloadType"]) or not WORD.fullmatch(failure["errorClass"]):
                raise ValueError("invalid failure")
        check("recent_failures", not failures and summary["failuresComplete"],
              "unknown: failure window capped" if not summary["failuresComplete"] else
              "; ".join(f"{r['workloadType']} {r['errorClass']} ({r['kind']})" for r in failures))
    except Exception:
        # Remove any partial metrics/checks from an invalid payload.
        checks[:] = [c for c in checks if c["name"] not in ("stuck_jobs", "recent_failures")]
        metrics.clear()
        check("stuck_jobs", False, "unknown: controller summary unavailable or invalid")
        check("recent_failures", False, "unknown: controller summary unavailable or invalid")
    try:
        stuck, age = handoffs()
        if age is not None:
            metric("newest_handoff_age", max(0, age), "s")
        check("handoffs", not stuck, "pending/staged handoff older than 900s" if stuck else "")
    except Exception:
        check("handoffs", False, "unknown: gateway handoff reading unavailable or invalid")
    return {"schema": "telemetry/1", "system": "tg-tools", "at": now,
            "state": "red" if any(not c["ok"] for c in checks) else "green", "checks": checks, "metrics": metrics}


def write_atomic(payload, path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, allow_nan=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def run(args):
    sys.path.insert(0, str(ROOT / "tg-gateway"))
    from tg_gateway.runtime_client import RuntimeClient
    client = RuntimeClient(base_url=os.environ.get("RUNTIME_CONTROLLER_API_URL", "http://127.0.0.1:8787"),
                           bearer_token=os.environ.get("RUNTIME_CONTROLLER_BEARER_TOKEN", ""), timeout=3)
    payload = collect(client, *configuration())
    if args.dry_run:
        print(json.dumps(payload, indent=2, allow_nan=False))
    else:
        write_atomic(payload, Path.home() / ".local/state/telemetry/tg-tools.json")
