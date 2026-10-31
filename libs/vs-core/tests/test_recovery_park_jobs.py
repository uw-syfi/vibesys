"""A pause leaves a running job alone: the step is released, and resume adopts the job.

A job lives on a cluster, a container or a local process that outlives the run's process. Pausing
must not kill it: the loop interrupts the step that watches it, the step stays open (it has no
result) and runs again, uncharged, in the epoch that resumes the run, where the runner's `clear`
finds the job and attaches to it. Core therefore issues no cancel of a job when it pauses, in either
mode, and never holds the park for the job to end.
"""

from __future__ import annotations

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Cancel,
    CancelReason,
    Interruption,
    Run,
    decide,
    interruption_of,
    started_message,
)
from vs_proto.api import Oneof
from vs_sim.api.testing import examples

from .recovery_loop import Loop, Move, play
from .test_measurement_pacing import World, limits


def job_world() -> tuple[Loop, core.RequestId]:
    """A run whose measurement step is running, with its job submitted and observed."""
    world = World(limits(1, 1))
    loop = Loop(world.state, core.OperationRegistry(()))
    loop.open_epoch()
    tag = world.submit.request_id
    assert tag is not None
    record = loop.state.records.record(tag)
    assert record is not None
    run = Run(world.submit, tag, 1, record.until.at)
    loop.add(Oneof("step_started", started_message(run)))
    return loop, tag


def cancels_of_jobs(loop: Loop) -> list[core.CancelOwnedJob]:
    return [
        row.request
        for row in loop.state.records.records
        if isinstance(row.request, core.CancelOwnedJob)
    ]


@pytest.mark.parametrize("kind", ["pause", "pause_now"])
def test_a_pause_releases_the_step_that_watches_a_job_and_cancels_no_job(kind: str) -> None:
    loop, tag = job_world()
    play(loop, (kind, None))
    # The step watches a job the pause lets go of, so nothing holds the park; the loop still runs
    # the attempt, and a parked run stops what runs. That stop is an interruption of the attempt,
    # not a cancel of the job: the step stays open (`interruption_of`), and a runner whose work
    # outlives the host leaves it running for `clear` to adopt (the `StepRunner` contract).
    assert loop.state.run.status is core.RunStatus.PARKED
    assert loop.running() == [tag]
    (cancel,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Cancel)]
    assert (cancel.tag, cancel.reason) == (tag, CancelReason.PAUSE_NOW)
    assert interruption_of(cancel.reason) is Interruption.PAUSE
    loop.apply(cancel)
    assert loop.state.run.status is core.RunStatus.PARKED
    assert cancels_of_jobs(loop) == []
    assert [job.terminal for job in loop.state.evaluation.jobs] == [False]
    released = loop.state.records.record(tag)
    assert released is not None
    assert released.phase is core.RequestPhase.PREPARED, "the step is open and waits to be adopted"
    assert not [
        row
        for row in loop.state.records.records
        if row.phase.value in ("dispatched", "reconciling")
    ], "a parked run has no request dispatched and unended"


@pytest.mark.parametrize("kind", ["pause", "pause_now"])
def test_resume_runs_the_released_step_again_uncharged_so_its_runner_can_adopt_the_job(
    kind: str,
) -> None:
    loop, tag = job_world()
    play(loop, (kind, None))
    (cancel,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Cancel)]
    loop.apply(cancel)
    play(loop, ("relaunch_resume", None))
    assert loop.state.run.status is core.RunStatus.RUNNING
    (rerun,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Run)]
    assert (rerun.tag, rerun.attempt) == (tag, 2)
    attempts = loop.state.steps.of(tag)
    assert attempts is not None
    assert attempts.charged == 0, "a pause is no debit of the retry bound"
    assert [job.terminal for job in loop.state.evaluation.jobs] == [False]


MOVES: st.SearchStrategy[Move] = st.one_of(
    st.just(("tick", None)),
    st.tuples(st.just("clock"), st.sampled_from([0.5, 3.0, 40.0])),
    st.tuples(st.just("pause"), st.sampled_from([None, 1.0, 20.0])),
    st.tuples(st.just("pause_now"), st.sampled_from([None, 1.0])),
    st.just(("crash", None)),
    st.just(("relaunch_resume", None)),
)


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(moves=st.lists(MOVES, max_size=10))
@example(moves=[("pause", None)])
@example(moves=[("pause_now", None), ("relaunch_resume", None), ("pause", 1.0)])
@example(moves=[("pause", 1.0), ("clock", 3.0), ("crash", None), ("relaunch_resume", None)])
def test_no_pause_in_any_order_of_events_ever_cancels_a_job_and_every_action_is_recordable(
    moves: list[Move],
) -> None:
    loop, _ = job_world()
    for move in moves:
        play(loop, move)
        loop.tick()
        assert cancels_of_jobs(loop) == []
        assert [job.terminal for job in loop.state.evaluation.jobs] == [False]
    assert loop.state.run.status is not core.RunStatus.DRAINING or loop.running()
