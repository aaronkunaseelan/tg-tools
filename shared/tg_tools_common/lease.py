"""Attempt-lease heartbeat with fencing.

Renewal runs from mark_running until the finalize response. Explicit
stale-token/lost-lease responses or local expiry without a successful
renewal mark the lease lost; a transient transport error alone does not.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

MIN_INTERVAL_S = 10.0
MAX_INTERVAL_S = 60.0
WORKER_BEAT_MAX_S = 30.0  # controller worker-heartbeat TTL is 120 s; stay well inside it


class LeaseLost(RuntimeError):
    pass


def heartbeat_interval(lease_seconds: int) -> float:
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, lease_seconds / 3.0))


class AttemptHeartbeat:
    """Renews one attempt lease on a background thread.

    ``client.heartbeat`` must raise an exception with ``status``/``error_code``
    attributes on failure (see RuntimeClientError). A 409/410 status or a
    stale/lost error code is explicit loss; other errors retry while the
    locally-known lease deadline (monotonic) has not passed.
    """

    def __init__(
        self,
        *,
        client: Any,
        job_id: str,
        attempt_id: str,
        lease_token: str,
        lease_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] | None = None,
        initial_remaining: float | None = None,
        worker_beat: Callable[[], Any] | None = None,
    ) -> None:
        self._client = client
        self._job_id = job_id
        self._attempt_id = attempt_id
        self._lease_token = lease_token
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._interval = heartbeat_interval(lease_seconds)
        # Optional worker-membership beat carried by this thread while the job
        # runs (the idle loop cannot beat during a job). Ticks fast enough for
        # it; the lease is renewed every `_ratio`-th tick, i.e. still every `_interval`.
        self._worker_beat = worker_beat
        self._ratio = max(1, math.ceil(self._interval / WORKER_BEAT_MAX_S)) if worker_beat else 1
        self._tick = self._interval / self._ratio
        self._deadline = clock() + (initial_remaining if initial_remaining is not None else lease_seconds)
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._sleeper = sleeper or (lambda s: self._stop.wait(s))
        self._thread: threading.Thread | None = None

    # -- state ---------------------------------------------------------------

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def check(self) -> None:
        """Raise LeaseLost if the lease is known lost (call before effects)."""
        if self._lost.is_set() or self._clock() >= self._deadline:
            raise LeaseLost(f"attempt lease lost for job {self._job_id}")

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> AttemptHeartbeat:
        self._thread = threading.Thread(target=self._run, name="attempt-heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def _run(self) -> None:
        ticks = 0
        while not self._stop.is_set():
            self._sleeper(self._tick)
            if self._stop.is_set() or self._lost.is_set():
                return
            ticks += 1
            if ticks % self._ratio == 0:
                self.beat_once()
            if self._worker_beat is not None:
                # After the lease renewal, and never allowed to affect it.
                try:
                    self._worker_beat()
                except Exception as exc:
                    logger.warning("worker heartbeat failed job=%s: %s", self._job_id, exc)
            if self._lost.is_set():
                return

    def beat_once(self) -> None:
        """One renewal attempt; safe to call directly in tests."""
        requested_at = self._clock()
        try:
            self._client.heartbeat(
                job_id=self._job_id,
                attempt_id=self._attempt_id,
                lease_token=self._lease_token,
                lease_duration_sec=self._lease_seconds,
            )
        except Exception as exc:
            status = getattr(exc, "status", getattr(exc, "status_code", None))
            code = str(getattr(exc, "error_code", "") or "").lower()
            explicit = status in (409, 410) or any(
                marker in code for marker in ("stale", "lease_lost", "not_leased"))
            if explicit:
                logger.warning("attempt lease explicitly lost job=%s: %s", self._job_id, exc)
                self._lost.set()
            elif self._clock() >= self._deadline:
                logger.warning("attempt lease expired without renewal job=%s: %s", self._job_id, exc)
                self._lost.set()
            else:
                logger.info("transient heartbeat failure job=%s: %s", self._job_id, exc)
            return
        self._deadline = requested_at + self._lease_seconds

    close = stop

    @property
    def deadline(self):
        return self._deadline

    @deadline.setter
    def deadline(self, value):
        self._deadline = value


AttemptLease = AttemptHeartbeat
