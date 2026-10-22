"""Persistent per-source work roots protected by cooperative file locks."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class WorkRootError(RuntimeError):
    """A retryable failure while preparing or locking a work root."""

    def __init__(
        self,
        error_class: str,
        message: str,
        *,
        backoff_sec: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.backoff_sec = backoff_sec


def root_name(source_key: str) -> str:
    """Return the stable, traversal-safe work-root name for source_key."""
    if not source_key:
        raise ValueError("source key must not be empty")
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    return f"source-{digest}"


def work_root(work_dir: Path, source_key: str) -> Path:
    return Path(work_dir) / root_name(source_key)


def _lock_error(exc: OSError) -> WorkRootError:
    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
        return WorkRootError(
            "work_root_busy",
            "work root lock is busy",
            backoff_sec=60,
        )
    return WorkRootError("work_root_lock_failed", f"work root lock failed: {exc}")


def ensure_work_state(work_dir: Path) -> None:
    """Create the persistent work and lock directories privately."""
    try:
        Path(work_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        (Path(work_dir) / ".locks").mkdir(mode=0o700, exist_ok=True)
        Path(work_dir).chmod(0o700)
        (Path(work_dir) / ".locks").chmod(0o700)
    except OSError as exc:
        raise _lock_error(exc) from exc


@contextmanager
def acquire_work_root(work_dir: Path, source_key: str) -> Iterator[Path]:
    """Yield source_key's work root while holding its exclusive lock."""
    name = root_name(source_key)
    work_dir = Path(work_dir)
    locks_dir = work_dir / ".locks"
    lock_path = locks_dir / f"{name}.lock"

    try:
        work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        locks_dir.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
    except OSError as exc:
        raise _lock_error(exc) from exc

    try:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WorkRootError(
                    "work_root_lock_failed",
                    "work root lock is not a regular file",
                )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise _lock_error(exc) from exc

        job_root = work_dir / name
        try:
            job_root.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise _lock_error(exc) from exc
        if job_root.is_symlink() or not job_root.is_dir():
            raise WorkRootError(
                "work_root_lock_failed",
                "work root is not a real directory",
            )
        yield job_root
    finally:
        os.close(fd)


work_root_name = root_name
locked_work_root = acquire_work_root
