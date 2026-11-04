"""A stopped run of every search ends when its cleanup does, not at the host's stop grace (#3475).

The host gives a stopped run 60 s (`STOP_GRACE_S`) and then cancels it, so a run that cannot end
by itself at a stop ends as `stopped` a minute late. A cancel stop (the operator's, or the run
environment's) ends the work at once and runs the cleanup that frees what the run holds, so the
run ends within a second of the stop; a drain lets the admitted work finish, so it ends when that
work's limit does. The vs-dst sweep (`libs/vs-dst/tests/test_stops_end_promptly.py`) holds the
rule over tiny worlds; this one holds it over the product searches, whose flows open sessions
that a cancel must close (four of the six stalled `closing` on one, at a quarter of the points).

A pull request stops the dynamic run at a sample of points by each cancel; the exhaustive sweep of
every search by every kind of stop (`slow`, every point under `--depth=nightly`) runs nightly.
"""

from __future__ import annotations

from functools import cache

import pytest
from tests.system.crash._flow_worlds import WORLDS

from vs_dst.api import SimulatedRun, StepLoopHosting, StopKind, simulate

CANCEL_TAKES = 1.0
"""Virtual seconds a cancelled run may take to end: a cancelled attempt is given the loop's
``cancel_grace_s`` (1 s) to return. The host would otherwise wait 60 s."""

CANCELS = [kind for kind in StopKind if kind.mode.value == "cancel"]


@cache
def _normal(world: str) -> SimulatedRun:
    normal = simulate(WORLDS[world](), StepLoopHosting())
    assert normal.finished
    return normal


def _assert_ends_promptly(normal: SimulatedRun, kind: StopKind) -> None:
    for point in normal.stop_points():
        stopped = normal.stop(point, kind)
        why = f"{kind.value} at {point}: {stopped.why_not_ended or f'took {stopped.took} s'}"
        assert stopped.delivered_at is not None, why
        assert stopped.ended, why
        if kind in CANCELS:
            assert stopped.took is not None
            assert stopped.took <= CANCEL_TAKES, why


@pytest.mark.parametrize("kind", CANCELS)
def test_a_dynamic_run_cancelled_at_a_sample_of_points_ends_within_a_second(
    kind: StopKind,
) -> None:
    _assert_ends_promptly(_normal("dynamic"), kind)


@pytest.mark.slow
@pytest.mark.parametrize("kind", StopKind)
@pytest.mark.parametrize("world", WORLDS)
def test_a_run_of_every_search_stopped_at_every_point_ends_promptly(
    world: str, kind: StopKind
) -> None:
    _assert_ends_promptly(_normal(world), kind)
