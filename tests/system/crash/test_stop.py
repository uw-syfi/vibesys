"""C5: an operator stop at any request ends the run with nothing open, and a Resume reopens it (#3475).

A measurement submission and a job poll have no cancellation in core, so a stop that arrives
while one is in flight waits for its observation, then releases the run's owned jobs through the
requests core issues. For each searched run (`_flow_worlds.py`) the operator stops the host at a
durable boundary or while an effect is in flight, and the host must end the run: terminal, a
result, every attempt closed, every job released. The operator then resumes it (a Resume lifts the
stop the log holds): the run goes on to an end of its own, finishing each step once. It is never
stuck, and the stop it lifted does not end it a second time.

The stop can land on the run's adoption, which a Resume takes back when it was verified; it can
be followed by the host's death while it winds down; and the host that resumed the run can be
stopped in turn. A pull request tries a few points of each class; the exhaustive sweep (`slow`)
runs in the nightly crash sweep.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

import pytest
from tests.system.crash._flow_worlds import WORLDS

from vs_core.api import OwnedJob, RegisteredOwnedJob, RunStatus
from vs_core.testing.liveness import End, final_state
from vs_dst.api import (
    Action,
    Interrupted,
    Interruption,
    Moment,
    SimulatedRun,
    StepLoopHosting,
    simulate,
)
from vs_sim.api.testing import evenly_spaced

if TYPE_CHECKING:
    from vs_core.api import CoreState

SAMPLE = 2
"""Points of a class of stops a pull request combines with a second interruption."""

STOP, KILL = Action.STOP, Action.KILL

ADOPTION_PREFIXES = ("adopt:", "verify-adoption:")
"""How the identity of a request names the run's adoption of its winner."""

STEP_LOOP = StepLoopHosting()


@cache
def _normal(world: str) -> SimulatedRun:
    return simulate(WORLDS[world](), STEP_LOOP)


def _jobs(core: CoreState) -> list[OwnedJob | RegisteredOwnedJob]:
    return [*core.evaluation.jobs, *core.evaluation.registered_jobs]


def _assert_nothing_left(core: CoreState | None, why: str) -> None:
    """The run is terminal with a result, every attempt settled and every job it owns released."""
    assert core is not None, why
    assert core.run.status is RunStatus.TERMINAL, why
    assert core.run.result is not None, why
    assert final_state(core, End.TERMINAL) == [], why
    unreleased = [(job.resource_id, job.status) for job in _jobs(core) if not job.released]
    assert unreleased == [], f"{why}: {unreleased}"


def _delivered(interrupted: Interrupted) -> bool:
    """Whether the stop reached a host: one that comes before the host has opened its log has
    nowhere to go, and fails that host (the operator's Resume starts the run)."""
    return interrupted.cores[0] is not None


def _stopped_at(normal: SimulatedRun, point: Interruption) -> Interrupted | None:
    """Stop at ``point``: the host leaves nothing open, and the resumed run ends by itself.

    Returns what became of the run, or None when the stop reached no host.
    """
    interrupted = normal.interrupt(point)
    why = f"{point}: {interrupted.outcome.value}; hosts {interrupted.history}; {interrupted.detail}"
    assert interrupted.never_stuck(), why
    assert interrupted.every_step_finished_once(), why
    if point.moment is not Moment.IN_FLIGHT:
        assert interrupted.reruns == 0, why
    if not _delivered(interrupted):
        return None
    _assert_nothing_left(interrupted.cores[0], f"{why}: the host that was stopped")
    _assert_nothing_left(interrupted.core, f"{why}: the host that resumed the run")
    assert interrupted.result is not None, why
    assert interrupted.result.outcome != "cancelled", why
    assert interrupted.resumes == 1, why
    return interrupted


def _class_id(moment: Moment) -> str:
    return moment.value.replace(" ", "-")


@pytest.mark.parametrize("world", sorted(WORLDS))
def test_a_stop_in_the_middle_of_each_class_ends_the_run_with_nothing_open_and_a_resume_goes_on(
    world: str,
) -> None:
    normal = _normal(world)

    for moment in Moment:
        points = normal.interruptions(STOP, moment)
        assert _stopped_at(normal, points[len(points) // 2]) is not None, moment


@pytest.mark.slow
@pytest.mark.parametrize("moment", [pytest.param(m, id=_class_id(m)) for m in Moment])
@pytest.mark.parametrize("world", sorted(WORLDS))
def test_a_stop_at_every_point_ends_the_run_with_nothing_open_and_a_resume_goes_on(
    world: str, moment: Moment
) -> None:
    normal = _normal(world)

    for point in normal.interruptions(STOP, moment):
        _stopped_at(normal, point)


def _adoption_stops(normal: SimulatedRun) -> list[Interruption]:
    """A stop while each request of the run's adoption of its winner is in flight."""
    return [
        Interruption(STOP, Moment.IN_FLIGHT, index)
        for index, identity in enumerate(normal.executed_effects)
        if identity.startswith(ADOPTION_PREFIXES)
    ]


@pytest.mark.parametrize("world", ["dynamic", "evolve"])
def test_a_stop_that_races_the_adoption_of_the_winner_leaves_a_run_a_resume_takes_up_again(
    world: str,
) -> None:
    """Regression: a stop that raced the run's adoption ended it, cancelled or failed, with no way
    back, so a run with hours of rounds in it could only be abandoned. The stop's ending cleans up
    and keeps the result, and the Resume reopens the run: it adopts afresh and ends by itself.
    """
    normal = _normal(world)
    stops = _adoption_stops(normal)
    assert len(stops) == 2, "the adopt request and the request that verifies it"

    for point in stops:
        interrupted = _stopped_at(normal, point)
        assert interrupted is not None, point
        assert interrupted.result is not None, point
        assert "adoption-failed" not in interrupted.result.reason, point


def _mid_run(normal: SimulatedRun) -> tuple[Interruption, ...]:
    """The stops after a commit once the host is open and before the run's last."""
    points = normal.interruptions(STOP, Moment.AFTER_COMMIT)
    return points[len(points) // 4 : 3 * len(points) // 4]


def test_a_run_stopped_again_after_a_resume_is_resumable_again() -> None:
    """The host that reopened a stopped run is stopped in turn: the run needs a second Resume, and
    neither stop ends it for good or makes its adoption fail."""
    normal = _normal("dynamic")
    first, *later = _mid_run(normal)
    taken = 0

    for second in later[-SAMPLE:]:
        interrupted = normal.interrupt(first, then=second)
        why = f"{first}, {second}: {interrupted.outcome.value}; {interrupted.history}"
        assert interrupted.never_stuck(), why
        assert interrupted.every_step_finished_once(), why
        _assert_nothing_left(interrupted.core, why)
        assert interrupted.result is not None, why
        assert interrupted.result.outcome != "cancelled", why
        assert "adoption-failed" not in interrupted.result.reason, why
        taken += interrupted.resumes == 2  # the second stop is one only the resumed host reaches

    assert taken, "no second stop ended the host that resumed the run"


def test_a_host_that_dies_while_it_winds_down_after_a_stop_is_replaced_by_one_that_ends_the_run() -> (
    None
):
    """Whenever the host gives up (it dies) after the stop reached it, a Resume starts a host that
    ends the run: it lifts the stop, or finishes the drain when core takes no Resume of a run that
    is already ending. Either way nothing is left open and every job is released."""
    normal = _normal("dynamic")
    stops = _mid_run(normal)
    kills = normal.interruptions(KILL, Moment.AFTER_COMMIT)
    landed = 0

    for stop_at in [stops[i] for i in evenly_spaced(len(stops), SAMPLE)]:
        for kill in [kills[i] for i in evenly_spaced(len(kills), SAMPLE)]:
            interrupted = normal.interrupt_while_draining(stop_at, kill)
            why = f"{stop_at}, {kill}: {interrupted.outcome.value}; {interrupted.history}"
            assert interrupted.never_stuck(), why
            assert interrupted.every_step_finished_once(), why
            _assert_nothing_left(interrupted.core, why)
            landed += interrupted.interruption.action is KILL

    assert landed, "no kill landed on the host that wound down"
