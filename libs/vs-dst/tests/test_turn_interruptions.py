"""A run with turns that is paused anywhere and resumed ends as the uninterrupted run did (#3475).

A pause's drain once refused the turns it had admitted: a turn asked for before the pause never
got its input reserved, and a turn asked for after another was refused at admission, so the
resumed run lost the turn. A pull request's sample of an interruption's points (`SAMPLE_SIZE`)
missed the few windows where this happened, so every point is tried.
"""

from __future__ import annotations

from functools import cache

import pytest

from vs_dst.api import Action, Hosting, Interrupted, Moment, ShellHosting, StepLoopHosting, simulate
from vs_sim.api.testing import Depth, using_depth

from ._turns import TurnsWorld

SHELL_FINISH_TWICE = (
    "the shell resumes a paused run that finished while it drained with the Finish held and the "
    "restarted flow's, and core refuses the second: a bug of the host the step loop replaces"
)


HOSTINGS: dict[str, Hosting] = {"shell": ShellHosting(), "step-loop": StepLoopHosting()}


@cache
def _pauses(hosting: str) -> tuple[Interrupted, ...]:
    """The run paused at every point of every moment, and resumed (one sweep per host)."""
    normal = simulate(TurnsWorld(2), HOSTINGS[hosting])
    assert normal.finished
    with using_depth(Depth.NIGHTLY):
        points = [p for moment in Moment for p in normal.interruptions(Action.PAUSE, moment)]
    assert points
    return tuple(normal.interrupt(point) for point in points)


@cache
def _answered(hosting: str) -> int:
    """How many turns the far side answers when the run is not interrupted."""
    world = simulate(TurnsWorld(2), HOSTINGS[hosting]).world
    assert isinstance(world, TurnsWorld)
    return world.turns_answered


@pytest.mark.parametrize("hosting", HOSTINGS)
def test_a_pause_at_every_point_of_a_run_with_turns_loses_none_of_its_turns(hosting: str) -> None:
    for interrupted in _pauses(hosting):
        assert isinstance(interrupted.world, TurnsWorld)
        assert interrupted.world.turns_answered == _answered(hosting), interrupted.interruption
        assert interrupted.reruns == 0, interrupted.interruption


@pytest.mark.parametrize(
    "hosting",
    [
        pytest.param(
            "shell", marks=pytest.mark.xfail(strict=True, reason=SHELL_FINISH_TWICE), id="shell"
        ),
        pytest.param("step-loop", id="step-loop"),
    ],
)
def test_a_pause_at_every_point_of_a_run_with_turns_resumes_to_the_uninterrupted_result(
    hosting: str,
) -> None:
    for interrupted in _pauses(hosting):
        why = f"{interrupted.interruption}: {interrupted.outcome.value}; {interrupted.detail}"
        assert interrupted.matches_uninterrupted(), why
        assert interrupted.never_stuck(), why
        assert interrupted.every_step_finished_once(), why
