"""A crash case that starts from a copy of the outside world equals re-running to the crash.

The property of the library: for every crash point of a run, `rerun_with_crash` reaches the same
result, strategy state, core state, decisions and executed-effects ledger as running the whole
run again with the host killed at that commit, which is the slow way that copies nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import EnsureWorkspace
from vs_core.api.testing import Unknown
from vs_dst.api import NondeterministicWorldError, simulate
from vs_sim.api.testing import SweepBreadth, using_sweep_breadth

from ._scratch import run_with_crash
from ._tiny import TinyWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer


@settings(deadline=None, max_examples=3)
@given(items=st.integers(min_value=1, max_value=2), concurrent=st.sampled_from([1, 2]))
def test_a_cheap_crash_equals_rerunning_from_the_start_at_every_crash_point(
    items: int, concurrent: int
) -> None:
    normal = simulate(TinyWorld(items, concurrent))
    with using_sweep_breadth(SweepBreadth.ALL):
        points = normal.crash_points()
    assert len(points) == 2 * normal.commits

    for crash in points:
        cheap = normal.rerun_with_crash(crash)
        slow = run_with_crash(lambda: TinyWorld(items), crash)
        assert cheap.finished, crash
        assert cheap.core == slow.core, crash
        assert cheap.result == slow.core.run.result, crash
        assert cheap.state == slow.state, crash
        assert cheap.decisions == tuple(slow.decisions), crash
        assert cheap.executed_effects == tuple(slow.effects.executed), crash


def test_a_crash_case_runs_only_the_rest_of_the_run() -> None:
    normal = simulate(TinyWorld(2))
    late = max(normal.crash_points(), key=lambda point: (point.commit, point.kind.value))

    recovered = normal.rerun_with_crash(late)

    assert len(recovered.journal.entries) < len(normal.journal.entries) // 4


def test_a_crash_case_leaves_the_reference_run_and_its_snapshots_as_they_were() -> None:
    world = TinyWorld(2)
    normal = simulate(world)
    calls = list(world.seen)
    point = normal.crash_points()[4]

    first = normal.rerun_with_crash(point)
    second = normal.rerun_with_crash(point)

    assert world.seen == calls
    assert first.core == second.core
    assert first.executed_effects == second.executed_effects
    assert normal.no_effect_ran_twice()


class DriftingWorld(TinyWorld):
    """A world that answers differently once a flag outside its copied state is set."""

    drifted: ClassVar[bool] = False

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if self.drifted and isinstance(request, EnsureWorkspace):
            return Unknown()
        return super().__call__(request, core)


def test_a_world_whose_second_run_differs_is_refused_as_nondeterministic() -> None:
    normal = simulate(DriftingWorld(1))
    DriftingWorld.drifted = True
    try:
        with pytest.raises(NondeterministicWorldError, match="DriftingWorld is not deterministic"):
            normal.rerun_with_crash(normal.crash_points()[0])
    finally:
        DriftingWorld.drifted = False
