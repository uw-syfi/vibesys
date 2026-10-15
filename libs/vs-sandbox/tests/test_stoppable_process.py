"""The stop policy of ``wait_stoppable`` on a simulated clock.

``FakeStoppableProcess`` follows a script (how long it works, how it answers ``SIGTERM``)
on ``SimThreads``' virtual clock, so a 30 s grace period costs nothing and every schedule
seed explores another interleaving of the process, the caller's cancel and the timeout.
The same contract the real ``PipedStoppableProcess`` passes (``tests/e2e``) holds for the Fake.
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

# test-isolation: the result contract for a lost client is internal to the command runners.
from vs_sandbox.command_execution import execute_command

# test-isolation: the stop policy is internal to the command runners; this is the one place that drives it directly.
from vs_sandbox.process_execution import (
    KILL,
    TERMINATE,
    ProcessOutcome,
    ProcessStop,
    RemoteProcesses,
    StopSignal,
    wait_stoppable,
)
from vs_sim.api.testing import SimThreads, examples

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


class _Remote:
    """A command's remote side: records each signal and answers how many processes remain.

    ``answers`` are given in order, one per signal; once they run out, the remote side
    is empty.
    """

    def __init__(self, *answers: int | None) -> None:
        self.signals: list[StopSignal] = []
        self._answers = list(answers)

    def __call__(self, number: StopSignal) -> RemoteProcesses:
        self.signals.append(number)
        return RemoteProcesses(self._answers.pop(0) if self._answers else 0)


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
    returncode: int | None
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
    remote = _Remote()

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
            signal_remote=remote if with_remote else None,
            threads=threads,
        )
        return _Observed(
            outcome.stopped,
            outcome.returncode,
            outcome.stdout,
            [(number, at - origin) for number, at in process.signals],
            remote.signals,
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


@settings(max_examples=examples(pr=5, nightly=150), deadline=None)
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
        expected_remote = [TERMINATE, KILL, KILL]
    assert seen.remote_signals == (expected_remote if with_remote else [])


class _InterruptedCancel:
    """A cancel event whose check raises, as ``KeyboardInterrupt`` does in a waiting thread."""

    def is_set(self) -> bool:
        raise KeyboardInterrupt


def test_an_interrupted_wait_kills_the_process_before_the_interruption_propagates() -> None:
    threads = SimThreads()
    process = FakeStoppableProcess(threads, StoppableScript(runs_for=None, term_delay=None))
    remote = _Remote()

    def main() -> None:
        with pytest.raises(KeyboardInterrupt):
            wait_stoppable(
                process,
                timeout=60,
                cancel=_InterruptedCancel(),
                signal_remote=remote,
                threads=threads,
            )

    threads.run(main)

    assert [number for number, _ in process.signals] == [KILL]
    assert remote.signals == [KILL]


def _stop_with_remote(
    remote: _Remote, *, grace: float = 5.0
) -> tuple[object, list[StopSignal], float]:
    """Time out a local client that dies on ``SIGTERM`` whose remote side answers as *remote*."""
    threads = SimThreads()
    origin = threads.now()
    process = FakeStoppableProcess(threads, StoppableScript(runs_for=None, term_delay=0.0))

    def main() -> tuple[object, list[StopSignal], float]:
        outcome = wait_stoppable(
            process,
            timeout=1,
            cancel=None,
            grace_seconds=grace,
            signal_remote=remote,
            threads=threads,
        )
        assert outcome.stopped is ProcessStop.TIMEOUT
        return outcome.unconfirmed, remote.signals, threads.now() - origin

    return threads.run(main)


def test_a_remote_side_that_outlives_its_killed_client_gets_sigkill_after_the_grace() -> None:
    """The local client dies at once; the remote command still gets the grace, then SIGKILL."""
    unconfirmed, signals, ended_at = _stop_with_remote(_Remote(1, 1, 0), grace=5.0)

    assert signals == [TERMINATE, TERMINATE, KILL]
    assert ended_at == pytest.approx(1 + 5.0)
    assert unconfirmed is None


@given(survivors=st.one_of(st.none(), st.integers(1, 5)))
def test_a_stop_that_cannot_show_the_remote_side_ended_is_unconfirmed(
    survivors: int | None,
) -> None:
    unconfirmed, signals, _ = _stop_with_remote(_Remote(survivors, survivors, survivors))

    assert signals == [TERMINATE, TERMINATE, KILL]
    assert unconfirmed == RemoteProcesses(survivors)


def test_a_remote_side_shown_empty_after_the_client_died_is_not_waited_for() -> None:
    unconfirmed, signals, ended_at = _stop_with_remote(_Remote(1, 0))

    assert signals == [TERMINATE, TERMINATE]
    assert ended_at == pytest.approx(1)
    assert unconfirmed is None


def _client_dies(
    signal_number: StopSignal, remote: _Remote | None, *, grace: float = 5.0
) -> tuple[ProcessOutcome, list[StopSignal], float]:
    """Run a client that a signal nobody here sent ends after 2 s; return its outcome."""
    threads = SimThreads()
    origin = threads.now()
    process = FakeStoppableProcess(
        threads, StoppableScript(stdout="partial", returncode=-signal_number, runs_for=2.0)
    )

    def main() -> tuple[ProcessOutcome, list[StopSignal], float]:
        outcome = wait_stoppable(
            process,
            timeout=60,
            cancel=None,
            grace_seconds=grace,
            signal_remote=remote,
            threads=threads,
        )
        assert outcome.stdout == "partial"
        assert process.signals == []
        return outcome, [] if remote is None else remote.signals, threads.now() - origin

    return threads.run(main)


@given(signal_number=st.sampled_from([TERMINATE, KILL]), survivors=st.integers(0, 3))
def test_a_relaying_client_killed_from_outside_reports_no_status_and_stops_the_command(
    signal_number: StopSignal, survivors: int
) -> None:
    """Regression: a ``docker exec`` client's own death read as the command's ``128 + N``.

    The client relays the command's status as an exit code, so its death by a
    signal says nothing about the command, which is stopped where it runs.
    """
    outcome, signals, ended_at = _client_dies(signal_number, _Remote(survivors, survivors))

    assert outcome.stopped is ProcessStop.CLIENT_LOST
    assert outcome.returncode is None
    if survivors:
        assert signals == [TERMINATE, KILL]
        assert ended_at == pytest.approx(2 + 5.0)
    else:
        assert signals == [TERMINATE]
        assert ended_at == pytest.approx(2)
    assert outcome.unconfirmed == (RemoteProcesses(survivors) if survivors else None)


@given(signal_number=st.sampled_from([TERMINATE, KILL]))
def test_a_local_command_killed_from_outside_reports_the_shell_status(
    signal_number: StopSignal,
) -> None:
    outcome, _, _ = _client_dies(signal_number, None)

    assert outcome.stopped is None
    assert outcome.returncode == 128 + signal_number


def test_a_lost_client_gives_a_result_with_no_exit_code_and_a_notice() -> None:
    threads = SimThreads()
    process = FakeStoppableProcess(threads, StoppableScript(returncode=-KILL, runs_for=1.0))

    result = threads.run(
        lambda: execute_command(
            "work",
            timeout=60,
            default_timeout=60,
            cancel=None,
            max_output_chars=1000,
            launch=lambda: process,
            signal_remote=_Remote(),
            threads=threads,
        )
    )

    assert result.exit_code is None
    assert not result.cancelled
    assert "exit status is unknown" in result.stderr
