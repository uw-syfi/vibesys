"""A request's limit is fixed when core accepts it: a host that dies and restarts keeps it.

The far side never answers the discard of a settled workspace, so the request waits for its
limit: core's bounded cleanup time from the moment it was issued. Killing the host at each
commit around the dispatch and restarting it must not start a new wait from the restart
clock once the request was committed: from the first commit that holds the record, every
later crash point recovers it with the limit it had, already ended ``Failed(TIMED_OUT)``
at it. Before that commit the record does not exist yet, and the restarted run issues it
afresh.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from vs_core.api import DiscardWorkspace, FailureReason, RequestPhase
from vs_core.api.testing import Unknown
from vs_dst.api import CrashKind, simulate
from vs_sim.api.testing import Depth, depth

from ._tiny import TinyWorld, spread

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request, RequestRecord
    from vs_core.api.testing import Answer
    from vs_dst.api import CrashPoint, SimulatedRun

RUN_DEADLINE = 1_500.0
"""Short, so that the virtual clock has little to wait through for an unanswered request."""


class SilentDiscard(TinyWorld):
    """A world whose executor never says whether a workspace discard ran."""

    def __init__(self, items: int = 1) -> None:
        super().__init__(items)
        harness = replace(self.setup.harness, deadline_at=RUN_DEADLINE)
        self.setup = replace(self.setup, harness=harness)

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, DiscardWorkspace):
            return Unknown()
        return super().__call__(request, core)


def _discards(run: SimulatedRun) -> list[RequestRecord]:
    return [r for r in run.core.records.records if isinstance(r.request, DiscardWorkspace)]


def _in_time_order(point: CrashPoint) -> tuple[int, bool]:
    """Before a commit comes first, after it second, then the next commit."""
    return point.commit, point.kind is CrashKind.AFTER_COMMIT


def test_a_committed_request_keeps_its_limit_through_a_crash_and_restart() -> None:
    normal = simulate(SilentDiscard(1))
    (reference,) = _discards(normal)
    assert reference.phase is RequestPhase.BLOCKED
    assert reference.failure is not None
    assert reference.failure.reason is FailureReason.TIMED_OUT
    assert reference.failure.limit_at == reference.until.at

    # A sample on a pull request (the property below is monotone along the run, so a sample
    # shows it as an every-point sweep does), every boundary at nightly depth.
    points = sorted(normal.crash_points(), key=_in_time_order)
    if depth() is not Depth.NIGHTLY:
        points = spread(points, 6)
    kept = []
    for crash in points:
        recovered = normal.rerun_with_crash(crash)
        rows = _discards(recovered)
        if not rows:
            # The restarted host's first clock reading already ended the workspace's start.
            continue
        (record,) = rows
        # A late answer may still complete the record; the failure its owner heard stays.
        assert record.failure is not None, crash
        assert record.failure.limit_at == record.until.at, crash
        if record.until == reference.until:
            assert record.failure == reference.failure, crash
        kept.append(record.until == reference.until)

    assert any(kept), "a crash after the request was committed must keep its limit"
    first = kept.index(True)
    assert all(kept[first:]), "once committed, every later crash point keeps the limit"
