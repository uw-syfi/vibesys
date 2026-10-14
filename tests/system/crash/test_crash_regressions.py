"""Crashes that once broke recovery, kept as named cases.

Each case pins a crash point of the skeleton run (named by its boundary, target and ordinal) that
a bug was found at. They stay whatever `--depth` says: the sample of
`test_crash_convergence.py` need not reach them.
"""

from __future__ import annotations

import logging

import pytest
from tests.support.crash_harness import (
    CORE_GAPS,
    all_crossings,
    converges_after,
    converges_after_one,
    core_gap,
    following,
    name,
)

from vs_faults.api import Boundary, Crossing
from vs_sim.api.testing import Depth, depth

HOLDING = "executor_request:close_session#1"
"""A crash while the skeleton's search is still alive: its flow's resources are open, the run
is closing. Named by request, not by a commit's ordinal, which moves when the run's writes do."""


def test_a_dead_host_leaves_no_search_generator_to_the_event_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The search runs inside the host's event loop but is not driven by it.

    A loop that heard of the search's async generators (a flow's ``@resource`` bodies) would
    close the ones still alive when it shuts down by running their release as a loop task, which
    cannot run a ``ctx`` wait and fails loudly. A dead host's search must start no more work: the
    next host settles the workspace from the journal. Here the host dies holding a workspace, and
    again right after the restart.
    """
    first = next(c for c in all_crossings() if name(c) == HOLDING)
    with caplog.at_level(logging.ERROR, logger="asyncio"):
        for second in following(first, 1):
            converges_after(first, second)
    assert [r.getMessage() for r in caplog.records if r.name == "asyncio"] == []


def _commit(ordinal: int) -> Crossing:
    return Crossing(Boundary.DURABLE_WRITE, "commit", ordinal)


# The pairs the P5 review reproduced as stalls.
_REVIEWED = [
    (Crossing(Boundary.EXECUTOR_REQUEST, "ensure_session", 1), _commit(28)),
    *((_commit(37), _commit(n)) for n in (38, 39, 40)),
    (_commit(49), _commit(52)),
]
# The three pairs below are distinct stalls; the neighbours of (37, 38) differ only in how far
# into the same recovery the second death falls, which the nightly job runs.
_PULL_REQUEST = [_REVIEWED[0], _REVIEWED[1], _REVIEWED[4]]
_PAIRS = _REVIEWED if depth() is Depth.NIGHTLY else _PULL_REQUEST


@pytest.mark.parametrize(
    ("first", "second"), _PAIRS, ids=[f"{name(a)}+{name(b)}" for a, b in _PAIRS]
)
def test_the_pairs_the_review_reproduced_converge(first: Crossing, second: Crossing) -> None:
    converges_after(first, second)


def test_the_known_core_gap_still_halts_the_run() -> None:
    """A request that began and never sealed reaches its limit while recovery still inspects it.

    ``vs_core._waits`` lists RECOVERY_CHECK as HALTS_THE_RUN: the deadline's extra inspection
    answers "unknown" again and puts the blocked check back to pending, which nothing will answer.
    The entry of ``CORE_GAPS`` fails the day core fixes this, and must then be removed, so every
    other crash point is held to convergence.
    """
    assert {"durable_write:receipt_begun#4"} == CORE_GAPS
    gap = next(c for c in all_crossings() if name(c) in CORE_GAPS)
    with core_gap(gap):
        converges_after_one(gap)
