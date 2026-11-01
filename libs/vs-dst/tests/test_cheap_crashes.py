"""A crash case starts from a copy of the outside world and runs only the rest of the run.

The copies leave the reference run as it was, and a world whose run is not deterministic is
refused by name, because a crash point of one of its runs is not a crash point of the next.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import pytest

from vs_core.api import EnsureWorkspace
from vs_core.api.testing import Unknown
from vs_dst.api import NondeterministicWorldError, simulate

from ._tiny import TinyWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer


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
