"""Time-dependent properties of the dynamic run, on a virtual clock with real-run durations.

When every fake answers at once, a lapsed lease, a stop that waits for a turn, or turns
that never overlap are invisible, and each of them blocked a real run. Here agent turns
last 15 to 45 s and evaluation jobs go through the Fake Slurm cluster's queue, run and
COMPLETING phases (``tests/support/timed_dynamic_run.py``), on one virtual clock, so a
run of ten virtual minutes takes seconds of host time and the same inputs always give the
same timeline.

The scenario is the production shell and loop (``CoreRuntime`` under ``drive_core``) over
the dynamic strategy, not the CLI host: the host builds its own wall run clock and runs
the cluster as a subprocess, neither of which a virtual clock can drive.

Each property names the fix it guards and the commit before which it fails:

- the lease heartbeat and the renew floor (#1370): the lease and renewal tests;
- stopping a turn from the loop (#1370): the stop-bound test;
- concurrent dispatch (#1374, #1375): the overlap and wall-time tests.
"""

from __future__ import annotations

from functools import cache

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.support.timed_dynamic_run import (
    IMPLEMENTERS,
    OBSERVE_INTERVAL_S,
    TimedRun,
    TimingProfile,
    run_timed,
)

from vibesys.run.core_run import LEASE_SECONDS
from vs_core.api import RunStatus
from vs_runtime.api.infrastructure import RunStopped
from vs_slurm.api import SecondsRange, SlurmTimingProfile

#: A stop during a turn is acted on within this many virtual seconds (the old bound was
#: the host's 60 s grace period).
STOP_BOUND_S = 5.0
#: Core polls a running job every ``observe_interval`` and the loop adds at most its
#: minimum sleep, so noticing that a job ended costs up to one interval.
_POLL_SLACK_S = OBSERVE_INTERVAL_S + 1.0

_SETTINGS = settings(max_examples=8, deadline=None, derandomize=True)


EXACT = TimingProfile(
    turn_s=SecondsRange.exactly(40.0),
    slurm=SlurmTimingProfile(
        queue_wait_s=SecondsRange.exactly(30.0),
        run_s=SecondsRange.exactly(150.0),
        completing_s=SecondsRange.exactly(35.0),
    ),
)
#: One agent turn longer than the 60 s lease, whatever else happens.
LONG_TURN = TimingProfile(turn_s=SecondsRange.exactly(LEASE_SECONDS + 5))


#: Wide ranges around the measured run: short and long turns, short and long evaluations.
_SHORT = TimingProfile(
    turn_s=SecondsRange(low=5.0, high=12.0),
    slurm=SlurmTimingProfile(
        queue_wait_s=SecondsRange(low=0.0, high=10.0),
        run_s=SecondsRange(low=20.0, high=60.0),
        completing_s=SecondsRange(low=0.0, high=5.0),
    ),
    seed=1,
)
_SLOW = TimingProfile(
    turn_s=SecondsRange(low=20.0, high=110.0),
    slurm=SlurmTimingProfile(
        queue_wait_s=SecondsRange(low=20.0, high=190.0),
        run_s=SecondsRange(low=100.0, high=300.0),
        completing_s=SecondsRange(low=30.0, high=45.0),
    ),
    seed=2,
)
#: The profiles the properties range over. A run costs about a second of host time, so the
#: properties draw from this spread (one run serves every property that draws it).
PROFILES = st.sampled_from([TimingProfile(), EXACT, LONG_TURN, _SHORT, _SLOW])


@cache
def _finished(profile: TimingProfile) -> TimedRun:
    """The run with no stop: a pure function of the profile, so cached across properties."""
    return run_timed(profile)


def _ended_normally(run: TimedRun) -> None:
    assert run.error is None, run.error
    assert run.outcome is not None
    assert run.outcome.status is RunStatus.TERMINAL


@_SETTINGS
@given(PROFILES)
def test_the_state_lease_never_lapses_while_the_run_is_alive(profile: TimingProfile) -> None:
    """Guards the heartbeat (#1370): a drain over 60 s (agent turns) used to lose the lease."""
    run = _finished(profile)
    _ended_normally(run)
    times = [event.at for event in run.lease_events]
    assert times[0] == run.started_at
    # No stretch of the run lasts a full lease without a renewal that extended it.
    for earlier, later in zip(times, [*times[1:], run.ended_at], strict=True):
        assert later - earlier < LEASE_SECONDS, (earlier, later)


@_SETTINGS
@given(PROFILES)
def test_every_lease_renewal_is_accepted(profile: TimingProfile) -> None:
    """Guards the renew floor (#1370): a renewal stamped with a turn's own, older request
    time (an agent tool call) was rejected once anything had advanced the store's time.
    """
    run = _finished(profile)
    _ended_normally(run)
    renewals = [event for event in run.lease_events if event.kind == "renew"]
    assert all(event.accepted for event in run.lease_events)
    # Every turn renews once mid-turn, besides the loop's own heartbeat.
    assert len(renewals) >= len(run.turns)


def _turn_windows(run: TimedRun) -> list[tuple[float, float]]:
    return [(span.start, span.end) for span in run.turns if span.end - span.start > 1.0]


@_SETTINGS
@example(profile=TimingProfile(), turn=0, position=0.4)
@example(profile=LONG_TURN, turn=1, position=0.9)
@given(
    profile=PROFILES,
    turn=st.integers(min_value=0, max_value=20),
    position=st.floats(min_value=0.05, max_value=0.95),
)
def test_a_stop_during_a_turn_is_acted_on_within_the_bound(
    profile: TimingProfile, turn: int, position: float
) -> None:
    """Guards stopping a turn from the loop (#1370): a stop used to wait for the turn to
    end, and for a turn that never ends, the 60 s grace period.
    """
    windows = _turn_windows(_finished(profile))
    start, end = windows[turn % len(windows)]
    stop_after = start + position * (end - start) - _finished(profile).started_at
    run = run_timed(profile, stop_after=stop_after)
    assert run.stopped_at is not None
    # Core cancels the running turns and ends the run terminal; only a request core cannot
    # cancel (a submission, a poll) in flight makes the loop give up with RunStopped.
    assert run.error is None or isinstance(run.error, RunStopped), run.error
    assert run.ended_at - run.stopped_at <= STOP_BOUND_S
    assert run.turns[-1].cancelled
    # Nothing new started after the stop.
    assert all(span.start <= run.stopped_at for span in run.turns)


def test_a_stop_during_a_turn_cancels_it_through_core_and_ends_the_run_terminal() -> None:
    reference = _finished(EXACT)
    start, end = _turn_windows(reference)[0]
    run = run_timed(EXACT, stop_after=(start + end) / 2 - reference.started_at)
    if run.error is not None:
        raise run.error
    assert run.outcome is not None
    assert run.outcome.status is RunStatus.TERMINAL
    assert run.ended_at - (run.stopped_at or run.ended_at) <= STOP_BOUND_S


@_SETTINGS
@given(PROFILES)
def test_two_implementer_turns_overlap_with_two_in_flight(profile: TimingProfile) -> None:
    """Guards concurrent dispatch (#1374, #1375): turns used to run strictly one at a time."""
    run = _finished(profile)
    _ended_normally(run)
    implementers = [span for span in run.turns if span.role == "implementer"]
    assert len(implementers) == IMPLEMENTERS
    assert run.max_overlap("implementer") == IMPLEMENTERS


@_SETTINGS
@given(PROFILES)
def test_a_finished_turn_is_followed_up_while_its_peer_turn_still_runs(
    profile: TimingProfile,
) -> None:
    """Guards deciding while requests run (#1375): a finished turn's follow-up (the review,
    then the evaluation submit) used to wait for every other running turn to end.
    """
    run = _finished(profile)
    _ended_normally(run)
    implementers = sorted((s for s in run.turns if s.role == "implementer"), key=lambda s: s.end)
    judges = sorted((s for s in run.turns if s.role == "judge"), key=lambda s: s.end)
    # Each review starts the moment its implementer's turn ends, not when the slowest ends.
    for implementer in implementers:
        assert any(abs(j.start - implementer.end) < 1e-6 for j in judges), implementer
    # The first review's evaluation is submitted when that review ends, while the other
    # review is still running (the submits are the jobs after the baseline's).
    submits = sorted(at for _, at in run.jobs)[1:]
    assert any(abs(at - judges[0].end) < 1e-6 for at in submits), (judges[0], submits)


@_SETTINGS
@given(PROFILES)
def test_a_one_round_two_workstream_run_takes_the_critical_path(profile: TimingProfile) -> None:
    """Guards concurrent dispatch (#1374, #1375): with two turns in flight the run is the
    baseline evaluation, then planner, implementers, reviews (each stage as long as its
    longest turn) and the candidates' evaluations; a serial shell adds a turn per stage.
    """
    run = _finished(profile)
    _ended_normally(run)
    slurm = profile.slurm
    evaluation = (
        slurm.queue_wait_s.high + slurm.run_s.high + slurm.completing_s.high + _POLL_SLACK_S
    )
    stages = 3  # planner, implementers, reviews
    bound = 2 * evaluation + stages * profile.turn_s.high
    assert run.wall_s <= bound, (run.wall_s, bound)
