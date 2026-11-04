"""A run that ends by a cancel cancels its work and runs on the cleanup that frees what it holds.

A cancelled run is closing until every session, workspace, job and scope it holds is released,
and a release is a step like any other: it has to run to its result. The rule once settled every
open step of a cancelled run as ``Cancelled(RUN_ENDING)``, the closes of its sessions included,
so the run stayed closing until the host's stop grace cancelled it (#3475). Each kind of step
declares what a run ending by a cancel does with it (``request_end_policy``): the property is
over generated runs, in any standing and after any history, and over the step kinds of every
world.
"""

from __future__ import annotations

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Cancel,
    Cancelled,
    CancelReason,
    EndPolicy,
    Run,
    Settle,
    decide,
    request_end_policy,
)
from vs_sim.api.testing import examples

from .recovery_loop import MOVES, OPEN, Loop, Move, play
from .recovery_worlds import StepSpec, World, build, worlds

_RESTARTS = frozenset({"crash", "relaunch_resume"})
_STEADY = st.lists(MOVES.filter(lambda move: move[0] not in _RESTARTS), max_size=10)


def cancelled_run(world: World, moves: list[Move]) -> Loop | None:
    """The run of ``world`` after ``moves``, stopped by a cancel; None if it had ended already."""
    state, registry = build(world)
    loop = Loop(state, registry)
    loop.open_epoch()
    for move in moves:
        play(loop, move)
        loop.tick()
    if loop.state.run.result is not None:
        return None
    control = loop.control("stop", mode=core.StopMode.CANCEL)
    result = core.RunResultProposal(outcome="cancelled", reason="operator")
    loop.offer(core.RunControlEvent(control=control, now_at=loop.clock, result=result))
    return loop


def ends_the_step(action: object) -> bool:
    """Whether ``action`` is the run's ending cancelling or settling a step."""
    match action:
        case Cancel(reason=CancelReason.RUN_ENDING):
            return True
        case Settle(result=Cancelled(reason=CancelReason.RUN_ENDING)):
            return True
        case _:
            return False


@pytest.mark.regression
@example(
    world=World(steps=(StepSpec(limit=2.0),), closes=(0,)),
    moves=[],
)
@example(world=World(steps=(StepSpec(limit=2.0),), retains=(0,)), moves=[])
@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(world=worlds(), moves=_STEADY)
def test_a_run_ending_by_a_cancel_ends_the_work_and_never_the_cleanup(
    world: World, moves: list[Move]
) -> None:
    loop = cancelled_run(world, moves)
    if loop is None or loop.state.run.status is core.RunStatus.TERMINAL:
        return
    actions = decide(loop.state, loop.clock)
    by_tag = {action.tag: action for action in actions if isinstance(action, Cancel | Settle | Run)}
    for row in loop.state.records.records:
        if row.phase not in OPEN:
            continue
        policy = request_end_policy(row.request, loop.state.registry)
        action = by_tag.get(row.request_id)
        if policy is EndPolicy.CANCEL:
            assert ends_the_step(action), (row.request.kind, action)
        else:
            assert not ends_the_step(action), (row.request.kind, action)
