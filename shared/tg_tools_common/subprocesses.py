"""Supervise subprocess groups while an attempt has execution authority."""
import os
import signal
import subprocess
import time

from .progress import current_progress


def run_with_authority(args, *, timeout=None, cwd=None, env=None):
    sink = current_progress()
    sink.check()
    deadline = time.monotonic() + timeout if timeout is not None else float("inf")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=cwd, env=env, start_new_session=True)
    try:
        while True:
            sink.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise subprocess.TimeoutExpired(args, timeout)
            try:
                stdout, stderr = proc.communicate(timeout=min(5, remaining))
                sink.check()
                return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait()
        if proc.stdout: proc.stdout.close()
        if proc.stderr: proc.stderr.close()
