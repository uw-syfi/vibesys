"""A run with turns that is paused anywhere and resumed ends as the uninterrupted run did (#3475).

A pause's drain once refused the turns it had admitted: a turn asked for before the pause never
got its input reserved, and a turn asked for after another was refused at admission, so the
resumed run lost the turn. A pull request's sample of an interruption's points (`SAMPLE_SIZE`)
missed the few windows where this happened, so every point is tried.
"""

from __future__ import annotations

from functools import cache

from vs_dst.api import Action, Interrupted, Moment, StepLoopHosting, simulate
from vs_sim.api.testing import Depth, using_depth

from ._turns import TurnsWorld

STEP_LOOP = StepLoopHosting()


@cache
def _pauses() -> tuple[Interrupted, ...]:
    """The run paused at every point of every moment, and resumed."""
    normal = simulate(TurnsWorld(2), STEP_LOOP)
    assert normal.finished
    with using_depth(Depth.NIGHTLY):
        points = [p for moment in Moment for p in normal.interruptions(Action.PAUSE, moment)]
    assert points
    return tuple(normal.interrupt(point) for point in points)


@cache
def _answered() -> int:
    """How many turns the far side answers when the run is not interrupted."""
    world = simulate(TurnsWorld(2), STEP_LOOP).world
    assert isinstance(world, TurnsWorld)
    return world.turns_answered


def test_a_pause_at_every_point_of_a_run_with_turns_loses_none_of_its_turns() -> None:
    for interrupted in _pauses():
        assert isinstance(interrupted.world, TurnsWorld)
        assert interrupted.world.turns_answered == _answered(), interrupted.interruption
        assert interrupted.reruns == 0, interrupted.interruption


def test_a_pause_at_every_point_of_a_run_with_turns_resumes_to_the_uninterrupted_result() -> None:
    for interrupted in _pauses():
        why = f"{interrupted.interruption}: {interrupted.outcome.value}; {interrupted.detail}"
        assert interrupted.matches_uninterrupted(), why
        assert interrupted.never_stuck(), why
        assert interrupted.every_step_finished_once(), why
