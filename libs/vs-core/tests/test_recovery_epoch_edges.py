"""Where a pause, a clock or a late report meets the edge of an epoch, the gates and the model agree.

Four causes found by the nightly depth of `test_recovery_progress.py`, each a place where one part
of core, or core and the model of the runner loop, disagreed about whether something is still live:

A. A Resume in a new epoch is taken while the dead epoch's pause is still draining (core).
B. The model compares the attempts of the steps still open when an epoch opens, as core keeps them.
C. The fold refuses the report of a replaced attempt, even after core ended the step itself (core).
D. `may_start` says yes only where `decide` would run the step, once a drain deadline has passed.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import Tick, decide, may_start
from vs_sim.api.testing import examples

from .recovery_loop import OPEN, Loop, Move, may_start_agrees_with_decide, play
from .recovery_worlds import (
    CLOSING,
    CLOSING_ITS_STEP,
    DISPATCHED,
    IN_FLIGHT,
    ONE_QUERY,
    RETAINING,
    Staging,
    World,
    build,
    staged_worlds,
)


def started(world: World) -> Loop:
    state, registry = build(world)
    loop = Loop(state, registry)
    loop.open_epoch()
    return loop


# --- A ----------------------------------------------------------------------------------------


@pytest.mark.regression
@example(world=RETAINING, before=[], pause=("pause", None))
@example(world=CLOSING, before=[], pause=("pause", None))
@example(world=RETAINING, before=[("tick", None)], pause=("pause", 20.0))
@example(world=RETAINING, before=[], pause=("pause_now", None))
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
@given(
    world=staged_worlds(Staging.RETAIN, Staging.CLOSE),
    before=st.lists(st.sampled_from([("tick", None), ("finish", 0)]), max_size=3),
    pause=st.sampled_from([("pause", None), ("pause", 1.0), ("pause", 20.0), ("pause_now", None)]),
)
def test_a_resume_in_a_new_epoch_is_taken_while_the_pause_still_has_effects_to_drain(
    world: World, before: list[Move], pause: Move
) -> None:
    """The process that took the pause died with a drain effect to run; its successor is resumed."""
    loop = started(world)
    for move in before:
        play(loop, move)
        loop.tick()
    play(loop, pause)
    refused = len(loop.refusals)

    play(loop, ("relaunch_resume", None))

    assert len(loop.refusals) == refused, loop.refusals[refused:]
    assert loop.state.run.status is core.RunStatus.RUNNING


# --- B ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("world", "moves"),
    [
        (DISPATCHED, [("tock", 3.0), ("pause_now", None), ("crash", None)]),
        (
            ONE_QUERY,
            [("tick", None), ("tock", 40.0), ("pause_now", None), ("crash", None)],
        ),
        (
            ONE_QUERY,
            [("tick", None), ("pause", 20.0), ("tock", 40.0), ("crash", None)],
        ),
    ],
)
def test_b_a_parked_epoch_charges_nothing_for_the_attempt_of_a_step_core_ended(
    world: World, moves: list[Move]
) -> None:
    """Core keeps no attempt of a step it ended itself, so the model has none to compare."""
    loop = started(world)
    for move in moves:
        play(loop, move)

    assert loop.state.run.status is core.RunStatus.PARKED
    assert not any(record.phase in OPEN for record in loop.state.records.records)
    assert loop.state.steps.attempts == ()


# --- C ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("world", [IN_FLIGHT, replace(ONE_QUERY, retries=2)])
def test_c_the_fold_refuses_the_report_of_a_replaced_attempt_after_core_ended_the_step(
    world: World,
) -> None:
    loop = started(world)
    play(loop, ("tick", None))
    if world.retries:
        play(loop, ("relaunch_resume", None))
        play(loop, ("tick", None))
    play(loop, ("tock", 40.0))  # core ends the step at its limit; the attempt still runs
    assert not any(record.phase in OPEN for record in loop.state.records.records)

    assert loop.finish_late(0)


# --- D ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("world", [RETAINING, CLOSING_ITS_STEP])
def test_d_a_step_may_start_only_when_decide_would_run_it_once_the_drain_deadline_passed(
    world: World,
) -> None:
    loop = started(world)
    play(loop, ("pause", 1.0))
    play(loop, ("clock", 3.0))  # the drain's deadline is 1.0; nothing has folded the clock yet

    may_start_agrees_with_decide(loop)

    assert [type(a) for a in decide(loop.state, loop.clock)] == [Tick]
    tags = [record.request_id for record in loop.state.records.records]
    assert not any(may_start(loop.state, tag, loop.clock) for tag in tags)
