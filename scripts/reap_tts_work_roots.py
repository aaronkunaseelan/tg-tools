#!/usr/bin/env python3
"""Reclaim disk from completed TTS work roots.

Work roots under ``$TTS_WORK_DIR`` are persistent by design: they hold the
generation checkpoint and published artifacts that let a retry resume instead
of regenerating, and ``test_worker_startup_performs_no_automatic_root_deletion``
deliberately forbids the worker from deleting them on its own. Nothing else in
the tree reclaims them either, so without an operator step the directory grows
without bound (roughly 35MB per episode).

This is that operator step, and it is intentionally conservative:

* Only roots whose publication is **complete** are eligible -- a valid
  ``publish.installed.json`` proves the episode is live, so the local copy is
  redundant. Roots mid-generation, mid-publish, quarantined or in any
  ambiguous state are always kept.
* A root whose lock is held by a running worker is skipped, using the same
  ``.locks/<name>.lock`` flock the worker takes.
* ``--min-age-days`` keeps recent roots so a just-finished job can still be
  inspected. Default 14.
* Dry run unless ``--apply`` is passed.

Usage:
    reap_tts_work_roots.py [--work-dir DIR] [--min-age-days N] [--apply]
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import shutil
import sys
import time
from pathlib import Path

DEFAULT_WORK_DIR = "~/.local/share/tg-tools/tts/work"
DEFAULT_MIN_AGE_DAYS = 14
_INSTALLED = "publish.installed.json"


def _is_installed(root: Path) -> bool:
    """True when this root holds a structurally valid installed marker."""
    marker = root / _INSTALLED
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    episode = record.get("episode")
    return bool(record.get("identity")) and isinstance(episode, dict) and bool(
        episode.get("slug"))


def _is_idle(work_dir: Path, name: str) -> bool:
    """True when no worker currently holds this root's lock."""
    lock_path = work_dir / ".locks" / f"{name}.lock"
    if not lock_path.exists():
        return True
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def _directory_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-dir", default=os.environ.get("TTS_WORK_DIR", DEFAULT_WORK_DIR))
    parser.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE_DAYS)
    parser.add_argument("--apply", action="store_true",
                        help="actually delete; otherwise report only")
    args = parser.parse_args(argv)

    work_dir = Path(args.work_dir).expanduser()
    if not work_dir.is_dir():
        print(f"no work dir at {work_dir}", file=sys.stderr)
        return 1

    cutoff = time.time() - args.min_age_days * 86400
    reclaimed = 0
    kept = 0
    for root in sorted(work_dir.glob("source-*")):
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            mtime = root.stat().st_mtime
        except OSError:
            continue

        if mtime > cutoff:
            kept += 1
            print(f"keep  {root.name}  (younger than {args.min_age_days} days)")
            continue
        if not _is_installed(root):
            kept += 1
            print(f"keep  {root.name}  (no completed publication)")
            continue
        if not _is_idle(work_dir, root.name):
            kept += 1
            print(f"keep  {root.name}  (locked by a running worker)")
            continue

        size = _directory_bytes(root)
        reclaimed += size
        action = "REMOVE" if args.apply else "would remove"
        print(f"{action}  {root.name}  ({size / 1_048_576:.1f} MB)")
        if args.apply:
            shutil.rmtree(root, ignore_errors=True)

    verb = "reclaimed" if args.apply else "reclaimable"
    print(f"\n{verb}: {reclaimed / 1_048_576:.1f} MB across the eligible roots; {kept} kept")
    if not args.apply and reclaimed:
        print("re-run with --apply to delete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
