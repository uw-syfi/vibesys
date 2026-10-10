"""The stop policy of ``wait_stoppable`` on a simulated clock.

``FakeStoppableProcess`` follows a script (how long it works, how it answers ``SIGTERM``)
on ``SimThreads``' virtual clock, so a 30 s grace period costs nothing and every schedule
seed explores another interleaving of the process, the caller's cancel and the timeout.
The same contract the real ``PopenProcess`` passes (``tests/e2e``) holds for the Fake.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sandbox.api.testing import (
    FakeStoppableProcess,
    ProcessHarness,
    StoppableProcessContract,
    StoppableScript,
)

# test-isolation: the stop policy is internal to the command runners; this is the one place that drives it directly.
from vs_sandbox.process_execution import (
    KILL,
    TERMINATE,
    ProcessStop,
    StopSignal,
    wait_stoppable,
)
from vs_sim.api.testing import SimThreads

SEEDS = st.one_of(st.none(), st.integers(0, 2**32))
_NATURAL_STATUS = 5
_TERM_STATUS = 128 + TERMINATE
_KILL_STATUS = 128 + KILL
# Instants are chosen at least 0.25 s apart across the three event kinds, so which one comes
# first never depends on the 0.05 s cancel poll or on float rounding.
_EXIT_AT = st.one_of(st.none(), st.integers(0, 30).map(lambda n: n + 0.5))
_TIMEOUT = st.integers(1, 20)
_CANCEL_AT = st.one_of(st.none(), st.integers(0, 25).map(lambda n: n + 0.75))
_TERM_DELAY = st.sampled_from([None, 0.0, 2.0])
_GRACE = st.sampled_from([1.0, 5.0])
_POLL_SLACK = 0.06


class TestFakeStoppableProcess(StoppableProcessContract):
    def harness(self) -> ProcessHarness:
        threads = SimThreads()

        def start(script: StoppableScript) -> FakeStoppableProcess:
            return FakeStoppableProcess(threads, script)

        return ProcessHarness(
            run=threads.run,
            exits_with=lambda out, err, code: start(
                StoppableScript(stdout=out, stderr=err, returncode=code)
            ),
            runs_until_signalled=lambda: start(StoppableScript(runs_for=None)),
            ignores_term=lambda: start(StoppableScript(runs_for=None, term_delay=None)),
        )


@dataclass(frozen=True)
class _Scenario:
    exit_at: float | None
    timeout: int
    cancel_at: float | None
    term_delay: float | None
    grace: float


@dataclass(frozen=True)
class _Observed:
    outcome_stop: ProcessStop | None
    returncode: int
    stdout: str
    group_signals: list[tuple[StopSignal, float]]
    """Each signal the process received, with the seconds since the scenario began."""
    remote_signals: list[StopSignal]
    ended_at: float


def _observe(scenario: _Scenario, seed: int | None, *, with_remote: bool) -> _Observed:
    threads = SimThreads(schedule_seed=seed)
    origin = threads.now()
    process = FakeStoppableProcess(
        threads,
        StoppableScript(
            stdout="out",
            returncode=_NATURAL_STATUS,
            runs_for=scenario.exit_at,
            term_delay=scenario.term_delay,
        ),
    )
    cancel = threads.event()
    remote: list[StopSignal] = []

    def main() -> _Observed:
        if scenario.cancel_at is not None:
            threads.spawn(
                lambda: (threads.sleep(scenario.cancel_at or 0.0), cancel.set()), name="cancel"
            )
        outcome = wait_stoppable(
            process,
            timeout=scenario.timeout,
            cancel=cancel if scenario.cancel_at is not None else None,
            grace_seconds=scenario.grace,
            signal_remote=remote.append if with_remote else None,
            threads=threads,
        )
        return _Observed(
            outcome.stopped,
            outcome.returncode,
            outcome.stdout,
            [(number, at - origin) for number, at in process.signals],
            remote,
            threads.now() - origin,
        )

    return threads.run(main)


def _model(scenario: _Scenario) -> tuple[ProcessStop | None, float]:
    """The stop reason and the instant the first stop signal is sent."""
    events: list[tuple[float, ProcessStop | None]] = [
        (float(scenario.timeout), ProcessStop.TIMEOUT)
    ]
    if scenario.cancel_at is not None:
        events.append((scenario.cancel_at, ProcessStop.CANCELLED))
    if scenario.exit_at is not None:
        events.append((scenario.exit_at, None))
    return min(events, key=lambda event: event[0])[::-1]


_SCENARIOS = st.builds(
    _Scenario,
    exit_at=_EXIT_AT,
    timeout=_TIMEOUT,
    cancel_at=_CANCEL_AT,
    term_delay=_TERM_DELAY,
    grace=_GRACE,
)


@settings(max_examples=150, deadline=None)
@given(seed=SEEDS, scenario=_SCENARIOS, with_remote=st.booleans())
def test_a_process_is_stopped_by_whichever_comes_first_and_killed_only_after_the_grace(
    seed: int | None,
    scenario: _Scenario,
    with_remote: bool,  # noqa: FBT001  # lint-waiver: LW-731016 [FBT001]; a hypothesis-drawn flag, not a call-site boolean.
) -> None:
    exit_at, term_delay, grace = scenario.exit_at, scenario.term_delay, scenario.grace
    seen = _observe(scenario, seed, with_remote=with_remote)
    reason, first_event = _model(scenario)

    assert seen.stdout == "out"
    assert seen.outcome_stop is reason
    if reason is None:
        # It finished on its own before any stop: nobody signalled it.
        assert seen.returncode == _NATURAL_STATUS
        assert seen.group_signals == []
        assert seen.remote_signals == []
        assert seen.ended_at == pytest.approx(exit_at)
        return

    stop_at = seen.group_signals[0][1]
    assert seen.group_signals[0][0] is TERMINATE
    assert abs(stop_at - first_event) <= _POLL_SLACK
    own_exit = exit_at if exit_at is not None else float("inf")
    term_exit = float("inf") if term_delay is None else stop_at + term_delay
    ends_in_grace = min(own_exit, term_exit) <= stop_at + grace
    if ends_in_grace:
        assert [number for number, _ in seen.group_signals] == [TERMINATE]
        assert seen.returncode == (_TERM_STATUS if term_exit < own_exit else _NATURAL_STATUS)
        assert seen.ended_at == pytest.approx(min(own_exit, term_exit))
        expected_remote = [TERMINATE, TERMINATE]
    else:
        assert [number for number, _ in seen.group_signals] == [TERMINATE, KILL]
        assert seen.group_signals[1][1] == pytest.approx(stop_at + grace)
        assert seen.returncode == _KILL_STATUS
        assert seen.ended_at == pytest.approx(stop_at + grace)
        expected_remote = [TERMINATE, KILL]
    assert seen.remote_signals == (expected_remote if with_remote else [])


class _InterruptedCancel:
    """A cancel event whose check raises, as ``KeyboardInterrupt`` does in a waiting thread."""

    def is_set(self) -> bool:
        raise KeyboardInterrupt


def test_an_interrupted_wait_kills_the_process_before_the_interruption_propagates() -> None:
    threads = SimThreads()
    process = FakeStoppableProcess(threads, StoppableScript(runs_for=None, term_delay=None))
    remote: list[StopSignal] = []

    def main() -> None:
        with pytest.raises(KeyboardInterrupt):
            wait_stoppable(
                process,
                timeout=60,
                cancel=_InterruptedCancel(),
                signal_remote=remote.append,
                threads=threads,
            )

    threads.run(main)

    assert [number for number, _ in process.signals] == [KILL]
    assert remote == [KILL]
