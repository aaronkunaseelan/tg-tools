"""Bounded operational facts; no transport I/O on execution threads."""
from __future__ import annotations
import contextlib
import contextvars
from collections import deque
import logging
import threading
import time
import uuid
from functools import wraps

log = logging.getLogger(__name__)
_current = contextvars.ContextVar("execution_progress", default=None)


class NullProgress:
    def check(self): pass

    def emit(self, stage, kind, *, occurrence=None, **facts):
        return occurrence or str(uuid.uuid4())

    @contextlib.contextmanager
    def stage(self, name, **facts):
        yield facts


_null = NullProgress()


def current_progress():
    return _current.get() or _null


def tracked_stage(name, facts_from_result=None):
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            with current_progress().stage(name) as facts:
                result = fn(*args, **kwargs)
                if facts_from_result:
                    facts.update(facts_from_result(result))
                return result
        return run
    return decorate


@contextlib.contextmanager
def use_progress(sink):
    token = _current.set(sink)
    try: yield sink
    finally: _current.reset(token)


class ProgressReporter(NullProgress):
    def __init__(self, *, send, job_id, attempt_id, lease_token, check=lambda: None, capacity=128):
        self._send, self._check, self._capacity = send, check, capacity
        self._ref = dict(jobId=job_id, attemptId=attempt_id, leaseToken=lease_token)
        self._queue, self._condition = deque(), threading.Condition()
        self._sequence, self._stop, self.dropped = 0, False, 0
        self._sending = False
        self._thread = threading.Thread(target=self._run, name="progress-sender", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def close(self):
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        self._thread.join(timeout=6)

    def flush(self, timeout=3):
        deadline = time.monotonic() + timeout
        with self._condition:
            while (self._queue or self._sending) and time.monotonic() < deadline:
                self._condition.wait(timeout=max(0, deadline - time.monotonic()))

    def check(self): self._check()

    def emit(self, stage, kind, *, occurrence=None, **facts):
        occurrence = occurrence or str(uuid.uuid4())
        with self._condition:
            if self._stop: return occurrence
            self._sequence += 1
            event = dict(schemaVersion=1, **self._ref, eventId=str(uuid.uuid4()),
                         sequence=self._sequence, stage=stage, occurrence=occurrence, kind=kind, facts=facts)
            if len(self._queue) >= self._capacity:
                index = next((i for i, x in enumerate(self._queue) if x["kind"] == "advanced"), 0)
                del self._queue[index]
                self.dropped += 1
                log.warning("progress buffer full job=%s dropped=%s", self._ref["jobId"], self.dropped)
            self._queue.append(event)
            self._condition.notify()
        return occurrence

    @contextlib.contextmanager
    def stage(self, name, **facts):
        self.check()
        started = time.monotonic()
        occurrence = self.emit(name, "started", **facts)
        try:
            yield facts
        except BaseException:
            self.emit(name, "interrupted", occurrence=occurrence)
            raise
        self.check()
        self.emit(name, "completed", occurrence=occurrence,
                  **{**facts, "durationSeconds": round(time.monotonic() - started, 1)})

    def _run(self):
        while True:
            with self._condition:
                while not self._queue and not self._stop: self._condition.wait()
                if self._stop: return
                event = self._queue.popleft()
                self._sending = True
            for attempt in range(3):
                try:
                    self._send(event)
                    break
                except Exception as exc:
                    status = getattr(exc, "status_code", getattr(exc, "status", None))
                    log.warning("progress delivery failed job=%s sequence=%s status=%s", self._ref["jobId"], event["sequence"], status)
                    if status in (400, 404, 409, 410) or self._stop: break
                    with self._condition:
                        self._condition.wait(timeout=min(2 ** attempt, 4))
                        if self._stop: return
            with self._condition:
                self._sending = False
                self._condition.notify_all()
