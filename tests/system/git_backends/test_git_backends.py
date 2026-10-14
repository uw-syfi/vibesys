"""The composed run behaves the same on in-memory Git as on the real Git CLI.

Most composition tests run on ``FakeGitRepositories`` because they need Git only as a
means. This module keeps the real implementation in the composition coverage: the same
scripted run, straight and crashed at a point in the middle of it, must end the same way
on both, including the tree it adopts (tree ids are Git's own, so they are equal across
implementations).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from tests.support.crash_harness import crash_plan, crash_points, run
from tests.support.skeleton_sim import Simulation, simulate

from vs_faults.api import FaultPlan
from vs_project.api.testing import GitKind
from vs_sim.api.testing import Depth, VirtualClock, depth, run_virtual


def _run(plan: FaultPlan, git: GitKind) -> Simulation:
    with tempfile.TemporaryDirectory() as scratch:
        return run_virtual(VirtualClock(), simulate(Path(scratch), plan, git=git))


def _plans() -> dict[str, FaultPlan]:
    points = crash_points()
    return {
        "straight": FaultPlan(seed=0),
        "crash-in-the-middle": crash_plan(points[len(points) // 2]),
    }


# The crashed run contains the straight one up to its crash, so a pull request plays only it.
@pytest.mark.parametrize(
    "name",
    ["straight", "crash-in-the-middle"] if depth() is Depth.NIGHTLY else ["crash-in-the-middle"],
)
def test_a_run_ends_the_same_on_in_memory_and_real_git(name: str) -> None:
    plan = _plans()[name]

    real = _run(plan, GitKind.REAL)
    fake = run(plan)  # the run on in-memory Git, shared with the crash tests

    assert fake.summary == real.summary
    assert real.summary.adopted_tree is not None
