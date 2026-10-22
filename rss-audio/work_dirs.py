"""Durable per-job work directories for the RSS runtime worker.

Enrichment makes a publish job long (measured ~12 minutes end to end on a
1h43m episode: ~3 min transcription, ~9 min across two model passes). Running
that inside a TemporaryDirectory means every retry re-pays for transcription
that already succeeded, so job state lives in a durable directory keyed by job
identity instead, and the enrichment pipeline checkpoints into it.

Mirrors the retention approach in ``tts/tts_worker/startup.cleanup_work_dirs``.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_WORK_DIRS = 50
# Count-based eviction must never reclaim a directory a job is still writing
# to. An rss_publish job runs ~12 minutes and its lease allows 7200s, so a
# directory touched within this window is treated as potentially in flight.
DEFAULT_MIN_AGE_SECONDS = 7200
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _mtime(path: Path) -> float | None:
    """Modification time, or None if the entry vanished mid-sweep.

    A concurrent worker can finish a job and rmtree its directory while this
    sweep is walking the root. Unguarded, that raced stat() raised
    FileNotFoundError out of a startup sweep and took the worker down into a
    systemd restart loop.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def job_work_dir(work_root: Path, job_id: str) -> Path:
    """Durable directory for one job. Never escapes ``work_root``."""
    safe = _UNSAFE.sub("-", str(job_id or "job")).strip("-") or "job"
    target = Path(work_root) / safe[:120]
    target.mkdir(parents=True, exist_ok=True)
    return target


def cleanup_work_dirs(
    work_root: Path,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_dirs: int = DEFAULT_MAX_WORK_DIRS,
    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
    exclude: object = (),
    now: float | None = None,
) -> int:
    """Drop work directories older than the window, keeping the newest few.

    Never touches anything outside ``work_root`` and never follows symlinks,
    and never removes a directory that could still be in use:

    * ``exclude`` names directories the caller knows are live.
    * ``min_age_seconds`` shields recently-touched directories from the
      count-based eviction. Age-based expiry still applies past the retention
      window; only the "keep the newest N" rule is age-gated. Without this a
      root holding more than ``max_dirs`` entries would delete the 51st-newest
      even if a second worker sharing the root was mid-enrichment inside it,
      destroying checkpoints and turning a resumable retry into a poison job.

    The sweep is defensive about entries disappearing underneath it, so it is
    safe to run while other workers are finishing jobs.
    """
    root = Path(work_root)
    if not root.is_dir():
        return 0
    moment = now if now is not None else time.time()
    protected = {str(name) for name in (exclude or ())}

    measured: list[tuple[Path, float]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return 0
    for path in children:
        if path.name in protected:
            continue
        try:
            if not path.is_dir() or path.is_symlink():
                continue
        except OSError:
            continue
        mtime = _mtime(path)
        if mtime is None:
            continue
        measured.append((path, mtime))

    measured.sort(key=lambda item: item[1], reverse=True)
    cutoff = moment - retention_days * 86400
    removed = 0
    for index, (entry, mtime) in enumerate(measured):
        expired = mtime < cutoff
        crowded = index >= max_dirs and (moment - mtime) >= min_age_seconds
        if expired or crowded:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("rss-audio work-dir retention removed %d directories", removed)
    return removed
