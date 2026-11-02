"""A run killed or paused at any point and resumed is never stuck (#3475).

For each searched run (`_flow_worlds.py`) the host is killed at every durable boundary and while
each effect is in flight, and the operator pauses the run at the same places; then the operator
resumes it, again if that does not end it. The run must continue to its end or end with a named
reason. It must never end with recovery blocked, with an exception, or waiting on something that
is not coming (`Interrupted.never_stuck`). A step finishes once, and a pause or a kill at a commit
re-runs nothing.

A pull request tries a few points of the dynamic run here. The exhaustive sweep of every search
(`slow`, a sample per class at `--depth=pr`, every point under `--depth=nightly`) runs in the
nightly crash sweep.
"""

from __future__ import annotations

from functools import cache

import pytest
from tests.system.crash._flow_worlds import WORLDS

from vs_dst.api import (
    SWEPT_MOMENTS,
    Action,
    Interruption,
    Moment,
    SimulatedRun,
    StepLoopHosting,
    simulate,
)
from vs_sim.api.testing import evenly_spaced

SAMPLE = 3
"""Points of each class of interruption a pull request tries."""

KILL, PAUSE = Action.KILL, Action.PAUSE

CLASSES = [(action, moment) for action in (KILL, PAUSE) for moment in SWEPT_MOMENTS]
"""Every kill and pause: a run resumed after either ends as the uninterrupted run did. A stop is
not one of them (the stop ends the run, and a Resume reopens it to an end of its own): see
`test_stop.py`."""
IN_FLIGHT = Moment.IN_FLIGHT

STEP_LOOP = StepLoopHosting()


@cache
def _normal(world: str) -> SimulatedRun:
    return simulate(WORLDS[world](), STEP_LOOP)


def _assert_never_stuck(normal: SimulatedRun, point: Interruption) -> None:
    interrupted = normal.interrupt(point)
    why = f"{point}: {interrupted.outcome.value}; hosts {interrupted.history}; {interrupted.detail}"
    assert interrupted.never_stuck(), why
    assert interrupted.every_step_finished_once(), why
    if point.action is Action.PAUSE or point.moment is not Moment.IN_FLIGHT:
        assert interrupted.reruns == 0, why


def _assert_resumed_as_uninterrupted(normal: SimulatedRun, point: Interruption) -> None:
    """The run ends as the uninterrupted one did: the result, and the adoption it names."""
    interrupted = normal.interrupt(point)
    why = f"{point}: {interrupted.outcome.value}; hosts {interrupted.history}; {interrupted.detail}"
    assert interrupted.matches_uninterrupted(), why
    assert interrupted.every_step_finished_once(), why


def _class_id(case: tuple[Action, Moment]) -> str:
    return f"{case[0].value}-{case[1].value.replace(' ', '-')}"


# The step loop runs the recovery rule of docs/contributing/run-recovery.md. Every kill, at every
# point of every search, resumes to the uninterrupted result, and so does every pause; no step's
# effect finishes twice. A kill with an effect in flight may run that effect again (it left no
# result to reuse); every other interruption re-runs nothing.

TURN_STARTING_WHEN_PAUSED: dict[str, Interruption] = {
    "single": Interruption(PAUSE, IN_FLIGHT, 5),
    "dynamic-parallel": Interruption(PAUSE, IN_FLIGHT, 3),
}
"""A pause that lands while a turn's session is being created (the designer's, in a single run), or
while a turn is asked for after another (a successor, in the dynamic-parallel run). The pause's
drain must let both go on: the turn was lost, and the resumed run ended with no winner."""


@pytest.mark.parametrize("world", TURN_STARTING_WHEN_PAUSED)
def test_a_pause_while_a_turn_starts_resumes_to_the_uninterrupted_result(world: str) -> None:
    normal = _normal(world)
    point = TURN_STARTING_WHEN_PAUSED[world]

    _assert_resumed_as_uninterrupted(normal, point)
    _assert_never_stuck(normal, point)


@pytest.mark.parametrize(
    ("action", "moment"), [pytest.param(*case, id=_class_id(case)) for case in CLASSES]
)
def test_a_dynamic_run_on_the_step_loop_interrupted_and_resumed_ends_as_it_did_at_a_sample_of_points(
    action: Action, moment: Moment
) -> None:
    normal = _normal("dynamic")
    points = normal.interruptions(action, moment)

    for index in evenly_spaced(len(points), SAMPLE):
        _assert_resumed_as_uninterrupted(normal, points[index])
        _assert_never_stuck(normal, points[index])


@pytest.mark.slow
@pytest.mark.parametrize(
    ("world", "action_moment"),
    [
        pytest.param(
            world,
            case,
            id=f"{world}-{_class_id(case)}",
        )
        for world in WORLDS
        for case in CLASSES
    ],
)
def test_a_run_on_the_step_loop_interrupted_at_every_point_and_resumed_ends_as_it_did(
    world: str, action_moment: tuple[Action, Moment]
) -> None:
    normal = _normal(world)
    for point in normal.interruptions(*action_moment):
        _assert_resumed_as_uninterrupted(normal, point)
        _assert_never_stuck(normal, point)
