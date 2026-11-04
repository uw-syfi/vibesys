"""A run held by the provider's quota, at any point of it, ends as the uninterrupted run did (#3475).

The quota pauses the run (a pause the run holds itself, owned by ``QUOTA``): nothing new starts,
and a turn in flight waits in its call, begun and sealing nothing, as the capacity gate holds it.
Core moves that turn's limit by the wait, so the wait is not the turn's time. The quota's timed
lift (``QUOTA_WAIT_S``, longer than the turn's own limit) ends the pause while the host runs. A
host that dies in the pause is resumed by the operator, whose Resume lifts it.

The credit of the wait is part of the step's own limit, which a step returned to unstarted takes
back. A host killed after the lift and before the turn answered lost it: the next process ended
the turn as timed out against the limit it had before the pause, and the run did not end.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from functools import cache

from vs_dst.api import (
    QUOTA_OUTLASTS_PATIENCE_S,
    QUOTA_WAIT_S,
    Action,
    Interrupted,
    Interruption,
    Moment,
    SimulatedRun,
    StepLoopHosting,
    simulate,
)
from vs_sim.api.testing import Depth, swept, using_depth

from ._turns import WITHIN, TurnsWorld

STEP_LOOP = StepLoopHosting()


@cache
def _normal() -> SimulatedRun:
    normal = simulate(TurnsWorld(2), STEP_LOOP)
    assert normal.finished
    return normal


@cache
def _pauses() -> tuple[Interruption, ...]:
    """The quota pause at every point of every moment of the run."""
    with using_depth(Depth.NIGHTLY):
        points = tuple(
            p for moment in Moment for p in _normal().interruptions(Action.QUOTA_PAUSE, moment)
        )
    assert points
    return points


@cache
def _kills() -> tuple[Interruption, ...]:
    with using_depth(Depth.NIGHTLY):
        return _normal().interruptions(Action.KILL)


@cache
def _answered() -> int:
    """How many turns the far side answers when the run is not interrupted."""
    world = simulate(TurnsWorld(2), STEP_LOOP).world
    assert isinstance(world, TurnsWorld)
    return world.turns_answered


def _ended_as_the_uninterrupted_run(interrupted: Interrupted, why: object) -> None:
    detail = f"{why}: {interrupted.outcome.value}; {interrupted.detail}"
    assert interrupted.matches_uninterrupted(), detail
    assert interrupted.never_stuck(), detail
    assert interrupted.every_step_finished_once(), detail


def test_the_quota_wait_is_longer_than_a_turns_own_limit() -> None:
    """The cases would show nothing if a turn could outwait the pause on its own limit."""
    assert QUOTA_WAIT_S > WITHIN


def test_a_quota_pause_lifted_by_its_timer_at_every_point_loses_nothing() -> None:
    for point in _pauses():
        interrupted = _normal().interrupt(point)
        _ended_as_the_uninterrupted_run(interrupted, point)
        assert isinstance(interrupted.world, TurnsWorld)
        assert interrupted.world.turns_answered == _answered(), point
        assert interrupted.reruns == 0, point
        assert interrupted.resumes == 0, f"{point}: the timer lifts it, no one resumes it"


def test_a_host_that_dies_in_a_quota_pause_at_every_point_is_resumed_to_the_same_end() -> None:
    """The operator gives up on the host before the quota lifts, and the Resume lifts the pause."""
    for point in _pauses():
        outlasting = replace(point, wait=QUOTA_OUTLASTS_PATIENCE_S)
        _ended_as_the_uninterrupted_run(_normal().interrupt(outlasting), outlasting)


def test_a_turn_the_quota_holds_in_its_call_outlives_its_own_limit_and_is_answered_once() -> None:
    """A turn held for longer than its limit ends well: core moved its limit by the wait."""
    held = Interruption(Action.QUOTA_PAUSE, Moment.BEFORE_COMMIT, 7, QUOTA_WAIT_S)
    interrupted = _normal().interrupt(held)
    _ended_as_the_uninterrupted_run(interrupted, held)
    clocks = [record.clock for record in interrupted.log_records]
    assert max(clocks) - min(clocks) >= QUOTA_WAIT_S, "the run did not wait out the quota"
    assert isinstance(interrupted.world, TurnsWorld)
    assert interrupted.world.turns_answered == _answered()


def test_a_kill_right_after_the_quota_lifts_keeps_the_credit_of_the_wait() -> None:
    """The turn waited longer than its limit, the quota lifted, and the host died before it answered.

    The next process runs the turn again with the limit it was credited. Without the credit, the
    run clock is past the limit the turn had before the pause: it ends as timed out, and the run
    never ends.
    """
    held = Interruption(Action.QUOTA_PAUSE, Moment.BEFORE_COMMIT, 7, QUOTA_WAIT_S)
    killed = Interruption(Action.KILL, Moment.AFTER_COMMIT, 11)
    interrupted = _normal().interrupt_while_draining(held, killed)
    assert interrupted.interruption.action is Action.KILL, "the kill must land on the paused host"
    _ended_as_the_uninterrupted_run(interrupted, (held, killed))


def test_a_quota_pause_followed_by_a_kill_anywhere_ends_as_the_uninterrupted_run() -> None:
    """Every quota pause, then the host killed at every kill point after it: a sample at PR depth."""
    pairs = list(itertools.product(_pauses(), _kills()))
    for number in swept(len(pairs), sample=60):
        held, killed = pairs[number]
        interrupted = _normal().interrupt_while_draining(held, killed)
        _ended_as_the_uninterrupted_run(interrupted, (held, killed))
