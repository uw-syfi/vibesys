"""Slow publications hold back neither the next request nor a session's close.

Publications are diagnostic: the loop delivers them between its other work, one at a time.
Before, it delivered every pending publication before starting any request, so each request
waited behind the whole backlog. With publications as long as a renewal interval, three
workers whose turns follow one another ran a third slower, and the closes and cancels the
run issued near its deadline waited for a slot until the deadline blocked them. A closing
session then waited on blocked requests that nothing would answer, and the run halted
with the sessions named as orphan waits.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from tests.support.skeleton_world import LEASE
from tests.system.skeleton_timing import Plan, StaggeredConfig, StaggeredStrategy, run_timed

import vs_core.api as core

_RENEWAL = LEASE / 3
# A request starts a scheduling step after the publication it waited for.
_SLACK_S = 1.0


@pytest.mark.parametrize(
    ("workers", "renewals_per_publication"),
    [
        (3, 1.2),
        pytest.param(3, 1.0, marks=pytest.mark.slow),
        pytest.param(2, 2.0, marks=pytest.mark.slow),
        pytest.param(3, 2.0, marks=pytest.mark.slow),
    ],
)
def test_a_slow_publication_holds_back_neither_the_next_turn_nor_a_close(
    workers: int, renewals_per_publication: float
) -> None:
    publish_s = _RENEWAL * renewals_per_publication
    timed = run_timed(
        StaggeredStrategy(config=StaggeredConfig(workers=workers)),
        Plan(
            duration=lambda _worker, _ordinal: 1.5 * LEASE,
            control_poll_s=4 * LEASE,
            publish_s=publish_s,
        ),
    )

    assert timed.failure is None
    assert timed.core.run.status == core.RunStatus.TERMINAL
    assert [session.phase for session in timed.core.sessions.sessions] == [
        core.SessionPhase.TERMINAL
    ] * workers
    spans = sorted(timed.timeline.spans, key=lambda span: span.start)
    assert len(spans) == workers
    assert all(
        later.start - earlier.end <= publish_s + _SLACK_S for earlier, later in pairwise(spans)
    )
