"""A run killed or paused at any point and resumed is never stuck (#3475).

For each searched run (`_flow_worlds.py`) the host is killed at every durable boundary and while
each effect is in flight, and the operator pauses the run at the same places; then the operator
resumes it, again if that does not end it. The run must continue to its end or end with a named
reason. It must never end with recovery blocked, with an exception, or waiting on something that
is not coming (`Interrupted.never_stuck`). A step finishes once, and a pause or a kill at a commit
re-runs nothing.

A pull request tries a few points of the dynamic run here, and the known failures below. The
exhaustive sweep of every search (`slow`, a sample per class at `--depth=pr`, every point under
`--depth=nightly`) runs in the nightly crash sweep.
"""

from __future__ import annotations

from functools import cache

import pytest
from tests.system.crash._flow_worlds import WORLDS

from vs_dst.api import Action, Interruption, Moment, SimulatedRun, simulate
from vs_sim.api.testing import evenly_spaced

REASON = "#3475 recovery redesign"
"""Why a case that fails today is expected to fail: the redesign makes it pass."""

SAMPLE = 3
"""Points of each class of interruption a pull request tries."""

CLASSES = [(action, moment) for action in Action for moment in Moment]

KILL, PAUSE = Action.KILL, Action.PAUSE
IN_FLIGHT = Moment.IN_FLIGHT

KNOWN_FAILING: dict[str, tuple[Interruption, ...]] = {
    "dynamic": (Interruption(KILL, IN_FLIGHT, 3), Interruption(PAUSE, IN_FLIGHT, 18)),
    "evolve": (Interruption(KILL, IN_FLIGHT, 3),),
    "single": (Interruption(PAUSE, IN_FLIGHT, 21),),
    "multi": (Interruption(PAUSE, IN_FLIGHT, 25),),
}
"""A few points that are stuck on today's main, which a pull request runs (the sweep below has the
rest): a kill with a turn in flight leaves recovery blocked, and a pause late in a run leaves it
waiting on a strategy that does not propose again."""

STUCK_GROUPS: frozenset[tuple[str, Action, Moment]] = frozenset(
    {
        *(("dynamic", KILL, IN_FLIGHT), ("dynamic-parallel", KILL, IN_FLIGHT)),
        *(("evolve", KILL, IN_FLIGHT), ("issue-queue", KILL, IN_FLIGHT)),
        ("single", KILL, IN_FLIGHT),
        *(("dynamic", PAUSE, moment) for moment in Moment),
        *(("single", PAUSE, moment) for moment in Moment),
        *(("multi", PAUSE, moment) for moment in Moment),
    }
)
"""The (search, class) pairs that have a stuck point on today's main, from sweeps of every point
of the dynamic run and of a sample of the others (12 per class). The sweep of the rest runs
nightly, and a pair it finds stuck belongs here."""


@cache
def _normal(world: str) -> SimulatedRun:
    return simulate(WORLDS[world]())


def _assert_never_stuck(normal: SimulatedRun, point: Interruption) -> None:
    interrupted = normal.interrupt(point)
    why = f"{point}: {interrupted.outcome.value}; hosts {interrupted.history}; {interrupted.detail}"
    assert interrupted.never_stuck(), why
    assert interrupted.every_step_finished_once(), why
    if point.action is Action.PAUSE or point.moment is not Moment.IN_FLIGHT:
        assert interrupted.reruns == 0, why


def _class_id(case: tuple[Action, Moment]) -> str:
    return f"{case[0].value}-{case[1].value.replace(' ', '-')}"


@pytest.mark.parametrize(
    ("action", "moment"), [pytest.param(*case, id=_class_id(case)) for case in CLASSES]
)
def test_a_dynamic_run_interrupted_and_resumed_is_never_stuck_at_a_sample_of_points(
    action: Action, moment: Moment
) -> None:
    normal = _normal("dynamic")
    points = normal.interruptions(action, moment)
    known = set(KNOWN_FAILING.get("dynamic", ()))

    for index in evenly_spaced(len(points), SAMPLE):
        if points[index] not in known:
            _assert_never_stuck(normal, points[index])


@pytest.mark.xfail(strict=True, reason=REASON)
@pytest.mark.parametrize(
    ("world", "point"),
    [
        pytest.param(world, point, id=f"{world}-{point}".replace(" ", "-"))
        for world, points in KNOWN_FAILING.items()
        for point in points
    ],
)
def test_the_runs_that_get_stuck_today_are_never_stuck(world: str, point: Interruption) -> None:
    _assert_never_stuck(_normal(world), point)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("world", "action_moment"),
    [
        pytest.param(
            world,
            case,
            id=f"{world}-{_class_id(case)}",
            marks=(
                [pytest.mark.xfail(strict=True, reason=REASON)]
                if (world, *case) in STUCK_GROUPS
                else []
            ),
        )
        for world in WORLDS
        for case in CLASSES
    ],
)
def test_a_run_interrupted_at_every_point_and_resumed_is_never_stuck(
    world: str, action_moment: tuple[Action, Moment]
) -> None:
    normal = _normal(world)
    for point in normal.interruptions(*action_moment):
        _assert_never_stuck(normal, point)
