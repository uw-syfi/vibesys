"""Paced job observation: polls follow core time, retry unknown polls, and stay issued-only.

A logical clock stands in for the shell. Nothing here sleeps or reads wall time.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_measurements import committed, observation, requested

JOB = core.ResourceId(root="job")
QUARTER = 0.25

intervals = st.integers(min_value=1, max_value=40).map(lambda quarters: quarters * QUARTER)
caps = st.integers(min_value=0, max_value=60).map(lambda quarters: quarters * QUARTER)


def limits(interval: float, extra: float) -> core.Limits:
    """Run limits whose backoff cap is never below the cadence."""
    return core.Limits(observe_interval=interval, observe_backoff_cap=interval + extra)


class World:
    """One submitted job observed through core, with the clock under test control."""

    def __init__(self, run_limits: core.Limits) -> None:
        base = core.initial_state()
        base = base.model_copy(update={"run": base.run.model_copy(update={"limits": run_limits})})
        result = requested(base)
        submit = result.requests[0]
        assert isinstance(submit, core.SubmitMeasurement)
        self.submit: core.SubmitMeasurement = submit
        first = observation(self.submit, 1, observed_at=0.0)
        state = committed(result.state, first)
        state = core.step(state, core.MeasurementSubmissionObserved(observation=first)).state
        after = core.step(state, core.JobObserved(resource_id=JOB, observation=first))
        assert polls(after) == []
        self.state = after.state
        self.clock = 0.0
        self.limits = run_limits
        self.outstanding = False
        self.poll_times: list[float] = []

    @property
    def job(self) -> core.OwnedJob:
        return self.state.evaluation.jobs[0]

    @property
    def due(self) -> float | None:
        return core.project(self.state).next_observe_at

    def tick(self, now: float) -> list[core.ObserveOwnedJob]:
        self.clock = max(self.clock, now)
        result = core.step(self.state, core.ClockAdvanced(now_at=self.clock))
        self.state = result.state
        issued = polls(result)
        if issued:
            self.outstanding = True
            self.poll_times.extend([self.clock] * len(issued))
        self.check()
        return issued

    def answer(
        self,
        status: core.ObservationStatus = core.ObservationStatus.PENDING,
        *,
        accepted: bool = True,
    ) -> core.Transition:
        """Deliver the executor's reply to the outstanding poll, one sequence further."""
        assert self.job.observation is not None
        terminal = status not in (core.ObservationStatus.PENDING, core.ObservationStatus.UNKNOWN)
        reply = observation(
            self.submit,
            self.job.observation.sequence + 1,
            observed_at=self.clock,
            status=status,
            terminal=terminal,
            accepted=accepted,
        )
        result = core.step(self.state, core.JobObserved(resource_id=JOB, observation=reply))
        assert polls(result) == [], "an observation never answers itself with a poll"
        self.state = result.state
        self.outstanding = False
        self.check()
        return result

    def check(self) -> None:
        """Post-step invariant: a live job has a poll outstanding or one due in the future."""
        if self.job.terminal:
            assert self.due is None
            return
        if self.outstanding:
            assert self.due is None
        else:
            assert self.due is not None
            assert self.due > self.state.run.now_at


def polls(result: core.Transition) -> list[core.ObserveOwnedJob]:
    return [r for r in result.requests if isinstance(r, core.ObserveOwnedJob)]


@given(
    interval=intervals,
    extra=caps,
    script=st.lists(st.tuples(st.integers(min_value=0, max_value=400), st.booleans()), max_size=40),
)
def test_polls_never_exceed_one_per_interval_and_unknown_polls_back_off(
    interval: float, extra: float, script: list[tuple[int, bool]]
) -> None:
    world = World(limits(interval, extra))
    cap = world.limits.observe_backoff_cap
    unknown_run = 0
    for quarters, answered_ok in script:
        issued = world.tick(world.clock + quarters * QUARTER)
        assert len(issued) <= 1
        if not issued:
            continue
        if answered_ok:
            unknown_run = 0
            world.answer()
            assert world.due == world.clock + interval
        else:
            unknown_run += 1
            world.answer(core.ObservationStatus.UNKNOWN, accepted=False)
            delay = min(interval * 2 ** (unknown_run - 1), cap)
            assert interval <= delay <= cap
            assert world.due == world.clock + delay
    gaps = [b - a for a, b in zip(world.poll_times, world.poll_times[1:], strict=False)]
    assert all(gap >= interval for gap in gaps)
    assert len(world.poll_times) <= world.clock / interval + 1


@given(interval=intervals, extra=caps, ticks=st.lists(st.integers(0, 400), max_size=30))
def test_an_unanswered_poll_is_never_repeated(
    interval: float, extra: float, ticks: list[int]
) -> None:
    world = World(limits(interval, extra))
    issued = world.tick(interval)
    assert [p.resource_id for p in issued] == [JOB]
    for quarters in ticks:
        assert world.tick(world.clock + quarters * QUARTER) == []
        assert world.due is None
    world.answer()
    assert world.due == world.clock + interval


@given(interval=intervals, extra=caps, retries=st.integers(min_value=1, max_value=12))
def test_an_unknown_poll_is_retried_within_the_backoff_bound(
    interval: float, extra: float, retries: int
) -> None:
    world = World(limits(interval, extra))
    for _ in range(retries):
        assert world.due is not None
        before = world.clock
        assert world.tick(world.due - QUARTER / 2) == []
        assert [p.resource_id for p in world.tick(world.due)] == [JOB]
        assert world.clock - before <= world.limits.observe_backoff_cap
        world.answer(core.ObservationStatus.UNKNOWN, accepted=False)
        assert world.job.status == core.ObservationStatus.UNKNOWN
    assert world.due is not None
    assert world.due - world.clock <= world.limits.observe_backoff_cap
    world.tick(world.due)
    world.answer()
    assert world.job.pacing.retries == 0


@given(interval=intervals, polls_before_end=st.integers(min_value=0, max_value=5))
def test_a_conclusive_observation_ends_polling(interval: float, polls_before_end: int) -> None:
    world = World(limits(interval, 0.0))
    for _ in range(polls_before_end):
        assert world.due is not None
        world.tick(world.due)
        world.answer()
    assert world.due is not None
    world.tick(world.due)
    world.answer(core.ObservationStatus.SUCCEEDED)
    assert world.job.terminal
    assert world.due is None
    assert world.tick(world.clock + 1000 * interval) == []


@given(
    interval=intervals,
    sequence=st.integers(min_value=0, max_value=30),
    reply=st.fixed_dictionaries(
        {
            "status": st.sampled_from(list(core.ObservationStatus)),
            "terminal": st.booleans(),
            "released": st.booleans(),
            "accepted": st.booleans(),
        }
    ),
    through_ledger=st.booleans(),
    polled=st.integers(min_value=0, max_value=4),
)
def test_only_the_next_issued_sequence_or_a_replay_changes_a_job(
    *,
    interval: float,
    sequence: int,
    reply: dict[str, object],
    through_ledger: bool,
    polled: int,
) -> None:
    world = World(limits(interval, 0.0))
    for _ in range(polled):
        assert world.due is not None
        world.tick(world.due)
        world.answer()
    held = world.job.observation
    assert held is not None
    incoming = observation(world.submit, sequence, observed_at=held.observed_at + 1, **reply)
    state = committed(world.state, incoming) if through_ledger else world.state
    result = core.step(state, core.JobObserved(resource_id=JOB, observation=incoming))
    if sequence == held.sequence + 1:
        return
    assert result.state.evaluation == state.evaluation
    assert result.requests == ()
    assert result.events == ()


def test_a_never_issued_terminal_observation_emits_no_result() -> None:
    world = World(limits(10.0, 0.0))
    world.tick(10.0)
    world.answer()
    skipped = observation(
        world.submit,
        99,
        observed_at=world.clock + 1,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
    )
    result = core.step(
        committed(world.state, skipped), core.JobObserved(resource_id=JOB, observation=skipped)
    )
    assert result.events == ()
    assert result.requests == ()
    assert not result.state.evaluation.jobs[0].terminal


@given(interval=intervals, shortfall=st.integers(min_value=1, max_value=8))
def test_a_backoff_cap_below_the_interval_is_refused(interval: float, shortfall: int) -> None:
    cap = interval - shortfall * 1e-3
    if cap <= 0:
        return
    with pytest.raises(ValueError, match="observe_backoff_cap"):
        core.Limits(observe_interval=interval, observe_backoff_cap=cap)
