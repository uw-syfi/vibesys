"""A stopped run ends when its work is done, never by waiting out the host's stop grace (#3475).

The host gives a stopped run 60 s (`STOP_GRACE_S`) and then cancels it, so a run that cannot end
on its own at a stop ends as `stopped`, a minute late, however it was stopped. Two things hold the
end of a run: the work it still has in flight, and the cleanup that frees what it holds. A cancel
ends the work at once, so only cleanup remains, and cleanup takes no time on the far side of these
worlds; a drain lets the admitted work finish, so the run ends when that work's limit does.

The properties are over every stop kind and every point of a run (a sample on pull requests, every
point under ``--depth=nightly``): the stop is delivered there, the host runs on, and the run
reaches its end, holding no session, within the time its work needs.
"""

from __future__ import annotations

from functools import cache

import pytest

from vs_core.api import SessionPhase
from vs_dst.api import SimulatedRun, StepLoopHosting, StopKind, Stopped, simulate

from ._tiny import TinyWorld
from ._turns import WITHIN, HeldTurnsWorld, TurnsWorld
from .test_close_withdraws import HeldWorld

CANCEL_TAKES = 1.0
"""Virtual seconds a cancelled run may take to end: a cancelled attempt is given the loop's
``cancel_grace_s`` (1 s here) to return, and the far side of these worlds frees what it holds at
once. Far below the 60 s the host would otherwise wait."""

WORLDS = {
    "workspaces": lambda: TinyWorld(2, retain=True),
    "session": lambda: TurnsWorld(2),
    "held turn": lambda: HeldTurnsWorld(2),
    "held measurement": lambda: HeldWorld(leaves_after=1000.0, until_stopped=True),
}
"""Each far side: the work a run holds when it is stopped (a workspace, a session and its turns, a
turn nothing answers, a measurement job nothing starts)."""

MUST_WAIT = {"workspaces": 0.0, "session": 0.0, "held turn": WITHIN, "held measurement": 0.0}
"""How long a drain waits for work it admitted: the limit of the turn the far side never answers.
Nothing else in these worlds outlives a stop that asks for no waiting."""


@cache
def _normal(world: str) -> SimulatedRun:
    """The run of ``world`` that nothing stops (one per world, which every kind of stop starts from)."""
    normal = simulate(WORLDS[world](), StepLoopHosting())
    assert normal.finished
    return normal


@cache
def _stops(world: str, kind: StopKind) -> tuple[Stopped, ...]:
    """The run of ``world`` stopped by ``kind`` at each point of the sweep."""
    normal = _normal(world)
    return tuple(normal.stop(point, kind) for point in normal.stop_points())


def _bound(world: str, kind: StopKind) -> float:
    """How long after a stop of ``kind`` the run of ``world`` may take to end."""
    return CANCEL_TAKES + (0.0 if kind.mode.value == "cancel" else MUST_WAIT[world])


@pytest.mark.parametrize("kind", StopKind)
@pytest.mark.parametrize("world", WORLDS)
def test_a_run_stopped_at_any_point_reaches_its_end(world: str, kind: StopKind) -> None:
    stops = _stops(world, kind)
    assert stops
    for stopped in stops:
        assert stopped.delivered_at is not None, stopped.point
        assert stopped.ended, f"{kind.value} at {stopped.point}: {stopped.why_not_ended}"
        assert stopped.core is not None
        assert stopped.core.run.result is not None, (kind, stopped.point)


@pytest.mark.parametrize("kind", StopKind)
@pytest.mark.parametrize("world", WORLDS)
def test_a_stopped_run_ends_within_the_time_its_work_needs_not_the_stop_grace(
    world: str, kind: StopKind
) -> None:
    for stopped in _stops(world, kind):
        took = stopped.took
        assert took is not None
        assert took <= _bound(world, kind), f"{kind.value} at {stopped.point}: took {took:g} s"


@pytest.mark.parametrize("kind", StopKind)
@pytest.mark.parametrize("world", WORLDS)
def test_a_stopped_run_ends_holding_no_session(world: str, kind: StopKind) -> None:
    for stopped in _stops(world, kind):
        assert stopped.core is not None
        open_sessions = [
            row.spec.session_id.root
            for row in stopped.core.sessions.sessions
            if row.phase is not SessionPhase.TERMINAL
        ]
        assert not open_sessions, f"{kind.value} at {stopped.point} left {open_sessions} open"
