"""A job's queue time is the cluster's: a job step is held for the queue budget before its limit counts.

A step is one request the runner loop hands to a runner whole, and core sees its job only at the
end. No fact says the job still waits in a queue, or for the root proof a run environment holds its
work for, so none can credit that time the way the ledger of observations did (`credited`). Without
a rule of its own, a measurement the cluster queues for longer than its limit ended `timed-out`
while its job was still pending, and a run whose Slurm probe waited in the queue lost its
measurement to the measurement's own limit.

The rule is over every step of every kind the recovery rule treats differently: a step whose
lifecycle owns a job gets the run's queue budget on top of its stated limit (never past the run's
end), and every other kind keeps the limit it was given. The worlds hold all four kinds, steps a
log of an earlier version had already dispatched (their limit is their own), runs of every length
(a short run caps every limit), and a clock that has moved since the run began.
"""

from __future__ import annotations

from dataclasses import replace

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import Cancel, CancelReason, Run, decide
from vs_sim.api.testing import examples

from .recovery_loop import Loop
from .recovery_worlds import Kind, StepSpec, World, build, worlds

_RUN_SECONDS = st.sampled_from([None, 500.0, 5_000.0, 100_000.0])
"""The default run lasts 1000 s and its end caps every limit; the longer ones leave room for the budget."""

_EARLIER = st.sampled_from([0.0, 3.0, 40.0, 500.0])
"""How much run time passed before the loop first decides."""


def started_loop(world: World, clock: float) -> Loop:
    state, registry = build(world)
    loop = Loop(state, registry)
    loop.open_epoch()
    loop.clock = clock
    return loop


def expected_deadline(loop: Loop, run: Run) -> float:
    """The limit a step is first started with, from what the rule is documented to give."""
    record = loop.state.records.record(run.tag)
    assert record is not None
    limit = record.limit_from_dispatch
    if record.dispatched_at is not None or limit is None:
        return record.until.at
    owned = record.lifecycle is core.LifecycleClass.OWNED_JOB
    stated = loop.clock + limit
    if owned:
        stated += loop.state.run.limits.queue_wait_budget
    deadline = loop.state.run.deadline_at
    return min(stated, deadline) if deadline > loop.clock else stated


def cancelled_for_its_limit(loop: Loop, at: float) -> set[core.RequestId]:
    return {
        action.tag
        for action in decide(loop.state, at)
        if isinstance(action, Cancel) and action.reason is CancelReason.DEADLINE
    }


@example(world=World((StepSpec(limit=10.0, kind=Kind.JOB),), run_seconds=100_000.0), earlier=0.0)
@example(world=World((StepSpec(limit=10.0, kind=Kind.QUERY),), run_seconds=100_000.0), earlier=0.0)
@example(world=World((StepSpec(limit=10.0, kind=Kind.JOB),), run_seconds=500.0), earlier=40.0)
@settings(max_examples=examples(pr=60, nightly=1000), deadline=None)
@given(
    world=worlds().flatmap(lambda w: _RUN_SECONDS.map(lambda s: replace(w, run_seconds=s))),
    earlier=_EARLIER,
)
def test_only_a_job_step_is_held_for_the_queue_budget_before_its_limit_counts(
    world: World, earlier: float
) -> None:
    loop = started_loop(world, earlier)
    runs = [action for action in decide(loop.state, loop.clock) if isinstance(action, Run)]
    for run in runs:
        assert run.deadline_at == expected_deadline(loop, run), run.tag
    loop.tick()
    for tag in loop.running():
        deadline = loop.deadlines[tag]
        assert tag not in cancelled_for_its_limit(loop, deadline - 0.25), tag
        assert tag in cancelled_for_its_limit(loop, deadline), tag


@settings(max_examples=examples(pr=20, nightly=300), deadline=None)
@given(limit=st.sampled_from([2.0, 10.0, 100.0]), queued=st.sampled_from([0.5, 1.5, 7.0]))
def test_a_job_that_waits_longer_than_its_limit_in_the_queue_is_not_cancelled_for_it(
    limit: float, queued: float
) -> None:
    """The cluster queues the job for a multiple of the step's limit: the step still holds."""
    world = World((StepSpec(limit=limit, kind=Kind.JOB),), run_seconds=100_000.0)
    loop = started_loop(world, 0.0)
    loop.tick()
    (tag,) = loop.running()
    at = limit * (1 + queued)
    assert tag not in cancelled_for_its_limit(loop, at)
