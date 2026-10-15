"""``ThreadsClock`` gives agentshim waits that run on the run's threads, real or simulated."""

from __future__ import annotations

from agentshim import StopSignal
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import STOP_POLL_S, ThreadsClock
from vs_sim.api.testing import SimThreads

_SECONDS = st.floats(min_value=0, max_value=3600, allow_nan=False)


@given(seconds=_SECONDS)
def test_a_wait_without_a_signal_takes_its_time_on_the_threads(seconds: float) -> None:
    threads = SimThreads()
    clock = ThreadsClock(threads)

    def scenario() -> tuple[bool, float]:
        before = clock.monotonic()
        return clock.wait(seconds), clock.monotonic() - before

    stopped, elapsed = threads.run(scenario)

    assert stopped is False
    assert abs(elapsed - seconds) <= 1e-6


@given(seconds=_SECONDS)
def test_a_wait_nobody_stops_takes_its_time_and_reports_not_stopped(seconds: float) -> None:
    threads = SimThreads()
    clock = ThreadsClock(threads)

    def scenario() -> tuple[bool, float]:
        before = clock.monotonic()
        return clock.wait(seconds, StopSignal()), clock.monotonic() - before

    stopped, elapsed = threads.run(scenario)

    assert stopped is False
    assert abs(elapsed - seconds) <= 1e-6


@given(seconds=_SECONDS)
def test_a_wait_on_a_set_signal_returns_at_once(seconds: float) -> None:
    threads = SimThreads()
    clock = ThreadsClock(threads)
    stop = StopSignal()
    stop.set()

    def scenario() -> tuple[bool, float]:
        before = clock.monotonic()
        return clock.wait(seconds, stop), clock.monotonic() - before

    assert threads.run(scenario) == (True, 0.0)


@given(seconds=st.floats(min_value=1, max_value=3600), fraction=st.floats(min_value=0, max_value=1))
def test_a_signal_set_during_a_wait_ends_it_within_one_poll(
    seconds: float, fraction: float
) -> None:
    threads = SimThreads()
    clock = ThreadsClock(threads)
    stop = StopSignal()
    stop_at = seconds * fraction

    def stopper() -> None:
        threads.sleep(stop_at)
        stop.set()

    def scenario() -> tuple[bool, float]:
        threads.spawn(stopper, name="stopper", daemon=True)
        before = clock.monotonic()
        return clock.wait(seconds, stop), clock.monotonic() - before

    stopped, elapsed = threads.run(scenario)

    if stopped:
        assert stop_at <= elapsed <= stop_at + STOP_POLL_S + 1e-6
    else:
        assert stop_at >= seconds - STOP_POLL_S - 1e-6
