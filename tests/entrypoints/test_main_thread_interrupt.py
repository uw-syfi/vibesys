"""A termination request interrupts the main thread once, even when its wake signal lands early.

#2103: the foreground server's SIGTERM relay woke the main thread with one signal. When
that signal landed just before the main thread blocked in its idle event loop, the
blocking call never ended, the handler never ran, and the run hung until SIGKILL.

The main thread is a simulated thread blocked until a wake signal ends its wait, the way
a system call ends on a signal. A lost wake is one that lands before the main thread
blocks: it ends nothing. A main thread kept busy past ``STUCK_SECONDS`` (a native call
that takes no signals) is reported as stuck, once. The real signals are in ``tests/e2e/test_foreground_run_sigterm.py``.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from entrypoints.main_thread_interrupt import STUCK_SECONDS, MainThreadInterrupt
from vs_sim.api.testing import SimThreads, examples, wait_or_fail


def _interruptions(
    seed: int | None, lost: int, requests: int, busy: float
) -> tuple[int, int, list[float]]:
    """How often the main thread was interrupted, how many wakes were sent, and the stuck reports."""
    threads = SimThreads(schedule_seed=seed)

    def run() -> tuple[int, int, list[float]]:
        woken = threads.event()
        first_interruption = threads.event()
        sent: list[None] = []
        interrupted: list[None] = []
        stuck: list[float] = []

        def wake() -> None:
            sent.append(None)
            if len(sent) > lost:
                woken.set()

        interrupt = MainThreadInterrupt(wake, threads, stuck.append)

        def main_thread() -> None:
            while True:
                wait_or_fail(woken, "a wake signal that ends the main thread's wait")
                woken.clear()
                # The handler runs at the main thread's next bytecode boundary, which a
                # long call can delay past a resend.
                threads.sleep(busy)
                try:
                    interrupt.take()
                except KeyboardInterrupt:
                    interrupted.append(None)
                    first_interruption.set()

        threads.spawn(main_thread, name="main-thread")
        for index in range(requests):
            threads.spawn(interrupt.request, name=f"request-{index}")
            wait_or_fail(first_interruption, "the main thread taking the request")
        # A taken wake keeps resending without raising again during cleanup.
        threads.sleep(1.0)
        interrupt.finish()
        return len(interrupted), len(sent), stuck

    return threads.run(run)


@settings(max_examples=examples(pr=30, nightly=300), deadline=None)
@given(
    seed=st.one_of(st.none(), st.integers(0, 2**32)),
    lost=st.integers(0, 5),
    requests=st.integers(1, 3),
    busy=st.sampled_from([0.0, 0.01, 0.2, STUCK_SECONDS + 1.0]),
)
def test_every_request_ends_with_exactly_one_interruption_however_many_wakes_are_lost(
    seed: int | None, lost: int, requests: int, busy: float
) -> None:
    interrupted, sent, stuck = _interruptions(seed, lost, requests, busy)

    assert interrupted == 1
    assert sent > lost
    # Lost wakes delay the take by at most lost * RESEND_SECONDS, far under STUCK_SECONDS.
    if busy > STUCK_SECONDS:
        assert len(stuck) == 1
        assert stuck[0] >= STUCK_SECONDS
    else:
        assert stuck == []
