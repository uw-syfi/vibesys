"""A request in flight when the host dies is not failed for the time the host was down.

A restarted host starts after the dead one's lease ran out, so a request's limit may have
passed while nobody was waiting. That says how long the far side went unasked, not that it
failed: the far side may have finished the work meanwhile, and the restarted host asks it
before it ends the request at its limit. A far side that never answers still gets no more
time than the request's limit (`test_limits_survive_restart.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_core.api import EnsureWorkspace, FailureReason
from vs_core.api.testing import Unknown
from vs_dst.api import simulate
from vs_sim.api.testing import Depth, depth, examples

from ._tiny import TinyWorld, spread

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request, RequestRecord
    from vs_core.api.testing import Answer
    from vs_dst.api import SimulatedRun

LIMITS = st.floats(min_value=20.0, max_value=3000.0, allow_nan=False)
"""Shorter and longer than the time a restart takes (a lease and a little more)."""

SHORTER_THAN_A_RESTART, LONGER_THAN_A_RESTART = 20.0, 3000.0
"""The two ends of `LIMITS`: the cases a restart treats differently, pinned at every depth."""


class SilentWorkspaces(TinyWorld):
    """A world whose executor never says whether a workspace was made."""

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, EnsureWorkspace):
            return Unknown()
        return super().__call__(request, core)


def _workspaces(run: SimulatedRun) -> list[RequestRecord]:
    return [r for r in run.core.records.records if isinstance(r.request, EnsureWorkspace)]


@settings(deadline=None, max_examples=examples(pr=1, nightly=8), derandomize=True, database=None)
@example(patience=SHORTER_THAN_A_RESTART)
@example(patience=LONGER_THAN_A_RESTART)
@given(patience=LIMITS)
def test_a_request_the_far_side_finished_is_not_timed_out_by_the_restart(patience: float) -> None:
    normal = simulate(TinyWorld(1, patience=patience))
    points = normal.crash_points()

    for crash in points if depth() is Depth.NIGHTLY else spread(points, 3):
        recovered = normal.rerun_with_crash(crash)
        assert recovered.finished, crash
        assert recovered.result == normal.result, crash
        for record in _workspaces(recovered):
            assert record.failure is None, (crash, record.failure)
        assert recovered.no_effect_ran_twice(), crash


@settings(deadline=None, max_examples=examples(pr=1, nightly=4), derandomize=True, database=None)
@example(patience=SHORTER_THAN_A_RESTART)
@example(patience=LONGER_THAN_A_RESTART)
@given(patience=LIMITS)
def test_a_request_the_far_side_never_answers_ends_at_its_limit_after_a_restart(
    patience: float,
) -> None:
    normal = simulate(SilentWorkspaces(1, patience=patience))
    points = normal.crash_points()

    for crash in (
        points[:: max(1, len(points) // 6)] if depth() is Depth.NIGHTLY else spread(points, 3)
    ):
        recovered = normal.rerun_with_crash(crash)
        for record in _workspaces(recovered):
            assert record.failure is not None, crash
            assert record.failure.reason is FailureReason.TIMED_OUT, crash
            assert record.failure.limit_at == record.until.at, crash
