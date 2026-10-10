"""A SIGTERM taken by any thread of a real foreground run process interrupts the run.

The kernel may hand a process SIGTERM to any thread while CPython runs handlers on the main
thread. This needs a real child process with real threads, so it runs in the real tier.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest
from hypothesis import example, given, settings
from hypothesis.strategies import integers

from vs_sim.api.testing import (
    HANG_GUARD_S,
    TGKILL_SUPPORTED,
    send_to_thread,
)

requires_tgkill = pytest.mark.skipif(
    not TGKILL_SUPPORTED, reason="tgkill syscall number is unknown"
)


@requires_tgkill
@example(transports=1, target=1)
@settings(max_examples=8, deadline=None)
@given(transports=integers(min_value=1, max_value=3), target=integers(min_value=0, max_value=3))
def test_sigterm_taken_by_any_thread_interrupts_the_foreground_run(
    transports: int, target: int
) -> None:
    # The kernel may hand a process SIGTERM to any thread while CPython runs
    # handlers on the main thread, which is blocked driving the run. Aim it at
    # the main thread or at any transport thread to force each interleaving.
    child = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-155502 [S603]; the test runs its own fixed child module.
        # > A real signal needs a real child process.
        [sys.executable, "-m", "tests.e2e.foreground_run_child", str(transports)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        announcement = child.stdout.readline().split()
        assert announcement[0] == "driving"
        # Only threads the run owns: a library's native thread (BLAS workers, ...) may
        # block SIGTERM or exit, and a signal aimed at it is then lost.
        threads = [int(thread) for thread in announcement[1:]]
        assert threads[0] == child.pid
        send_to_thread(child.pid, threads[target % len(threads)], signal.SIGTERM)
        # test-isolation: the deadline only guards a hang; a delivered stop returns at once
        output, _ = child.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate(timeout=HANG_GUARD_S)
        pytest.fail("SIGTERM did not interrupt the foreground run")
    finally:
        child.kill()
    assert "cleanup shutdown=True" in output
    assert child.returncode != 0
