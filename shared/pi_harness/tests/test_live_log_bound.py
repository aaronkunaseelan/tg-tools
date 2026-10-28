"""The configured log cap applies while the child is running."""
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pi_harness.runner import run_contained

import pytest
from pi_harness.runner import LOG_OVERFLOW_EXIT


@pytest.mark.parametrize("fd", [1, 2])
def test_overflow_stops_a_running_child_with_named_outcome(tmp_path, fd):
    script = f'import os,time; os.write({fd}, b"x" * 65536); time.sleep(30)'
    start = time.monotonic()
    rc, timed_out, out, err = run_contained(
        [sys.executable, '-c', script], '', str(tmp_path),
        {'PATH': '/usr/bin:/bin'}, timeout_s=10, cap_bytes=1024, grace_s=.1)
    assert (rc, timed_out) == (LOG_OVERFLOW_EXIT, False)
    assert time.monotonic() - start < 5
    assert Path(out).stat().st_size <= 1024
    assert Path(err).stat().st_size <= 1024
    assert 'log_overflow' in Path(err).read_text()


def test_exact_limit_and_large_stdin_do_not_deadlock(tmp_path):
    prompt = 'x' * 100000
    rc, timeout, out, err = run_contained(
        [sys.executable, '-c', 'import sys; print(len(sys.stdin.read()), end="")'],
        prompt, str(tmp_path), {'PATH': '/usr/bin:/bin'}, timeout_s=5, cap_bytes=6)
    assert (rc, timeout, Path(out).read_text()) == (0, False, '100000')


class LiveLogBound(unittest.TestCase):
    def test_log_cap_must_apply_before_the_child_finishes(self):
        cap = 1024
        observed = []
        with tempfile.TemporaryDirectory() as tmp:
            import pi_harness.runner as runner
            original_hit = runner._hit
            def hit(point, tag):
                if point == 'attempt.after-start':
                    output = Path(tmp) / 'stdout.log'
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and output.stat().st_size < 65536:
                        time.sleep(0.01)
                    observed.append(output.stat().st_size)
            # Finite, synthetic subprocess; the observer sees the log while alive.
            script = 'import os,time; os.write(1,b"x"*65536); time.sleep(.2)'
            with patch.object(runner, '_hit', side_effect=hit):
                run_contained([sys.executable, '-c', script], '', tmp,
                              {'PATH': '/usr/bin:/bin'}, timeout_s=5, cap_bytes=cap)
        self.assertLessEqual(observed[0], cap, 'log is capped only after process exit')


