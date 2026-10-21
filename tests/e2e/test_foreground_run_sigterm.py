"""A SIGTERM taken by any thread of a real foreground run process interrupts the run.

The kernel may hand a process SIGTERM to any thread while CPython runs handlers on the main
thread. Each thread the run owns takes it once, in a forked child of the test (real
threads, real signals, no interpreter start-up); one case runs the real entrypoint process
to prove the wiring.
"""

from __future__ import annotations

import gc
import os
import signal
import subprocess
import sys
from functools import partial
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.e2e.foreground_run_child import interrupted_run

from entrypoints.main_thread_interrupt import MainThreadInterrupt
from vs_sim.api import OsThreads
from vs_sim.api.testing import (
    HANG_GUARD_S,
    TGKILL_SUPPORTED,
    run_in_child,
    send_to_thread,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from sys import UnraisableHookArgs


requires_tgkill = pytest.mark.skipif(
    not TGKILL_SUPPORTED, reason="tgkill syscall number is unknown"
)


@requires_tgkill
@pytest.mark.parametrize(
    ("transports", "target"),
    [(1, 0), (1, 1), (1, 2), (3, 3)],
    # (3, 3) is the case that hung on main before #2349 (run 38539411846).
    ids=["main", "relay", "transport", "third-of-three-transports"],
)
def test_sigterm_taken_by_any_thread_interrupts_the_foreground_run(
    transports: int, target: int
) -> None:
    # The kernel may hand a process SIGTERM to any thread while CPython runs
    # handlers on the main thread, which is blocked driving the run. Aim it at
    # each thread the run owns. However late the main thread's wake signal
    # lands, the run ends interrupted (the interleavings are in
    # tests/entrypoints/test_main_thread_interrupt.py).
    cleanups, ended = run_in_child(partial(interrupted_run, transports, target))

    assert ended == "interrupted"
    assert cleanups == [True]


@requires_tgkill
def test_sigterm_whose_first_wake_is_discarded_by_gc_still_interrupts_the_run() -> None:
    cleanups, ended = run_in_child(partial(interrupted_run, 1, 0, discard_wake_in_gc=True))

    assert ended == "interrupted"
    assert cleanups == [True]


@requires_tgkill
def test_sigterm_taken_by_a_transport_thread_stops_the_real_server_process() -> None:
    """The one subprocess case: the real entrypoint process, signalled from outside."""
    child = subprocess.Popen(
        [sys.executable, "-m", "tests.e2e.foreground_run_child", "1"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
        send_to_thread(child.pid, threads[1], signal.SIGTERM)
        # test-isolation: the deadline only guards a hang; a delivered stop returns at once
        output, _ = child.communicate(timeout=HANG_GUARD_S)
    except subprocess.TimeoutExpired:
        # SIGABRT makes the child's faulthandler dump every thread's stack.
        child.send_signal(signal.SIGABRT)
        _, stuck_stderr = child.communicate(timeout=HANG_GUARD_S)
        pytest.fail(
            f"SIGTERM to a transport thread did not stop the run; its stacks:\n{stuck_stderr}"
        )
    finally:
        child.kill()
    assert "cleanup shutdown=True" in output
    assert child.returncode != 0


class _Finalizer:
    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback

    def __del__(self) -> None:
        self.callback()


def _discarded_interruptions(discarded: int) -> None:
    """Exercise the scope in a child: hooks and finalizers belong to the process."""
    seen: list[BaseException | None] = []
    previous = sys.unraisablehook
    enabled = gc.isenabled()
    gc.disable()

    def observe(error: UnraisableHookArgs) -> None:
        seen.append(error.exc_value)
        # A reporting hook can take another wake before it returns.
        interrupt.take()

    def finalize(callback: Callable[[], None]) -> None:
        finalizer = _Finalizer(callback)
        del finalizer

    interrupt = MainThreadInterrupt(lambda: None, OsThreads(), lambda _waited: None)

    def unrelated() -> None:
        raise ValueError

    sys.unraisablehook = observe
    try:
        with interrupt.recovering():
            for _ in range(discarded):
                finalize(interrupt.take)
            assert len(seen) == discarded
            assert all(isinstance(error, KeyboardInterrupt) for error in seen)
            with pytest.raises(KeyboardInterrupt):
                interrupt.take()
            # Ordinary propagation protects cleanup; an unrelated unraisable error
            # must be forwarded without rearming this interruption.
            finalize(unrelated)
            assert isinstance(seen[-1], ValueError)
            interrupt.take()
        assert sys.unraisablehook is observe
        interrupt.take()
    finally:
        sys.unraisablehook = previous
        if enabled:
            gc.enable()


@pytest.mark.regression
@example(discarded=1)
@given(discarded=st.integers(0, 5))
def test_only_our_discarded_interruptions_rearm_delivery(discarded: int) -> None:
    run_in_child(partial(_discarded_interruptions, discarded))
