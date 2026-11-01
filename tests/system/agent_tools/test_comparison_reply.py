"""The comparison a tool's reply carries is the framework's, read at the run clock.

The production step loop hosts the skeleton search over the agent-tools world in its measured
mode (`world.py`): every measurement ends as it is accepted with a headline reading, the base's
and each candidate's, and the task states how to compare them. The agent submits a candidate and
then, in a later turn, submits the same content again: core answers the repeat with the handle it
already holds, and the reply carries the outcome and the comparison of the finished run.

The comparison is the one core logged, read at the moment of the reply (the run clock): a base
sample older than the task's staleness no longer counts, so the agent is told nothing of a
comparison against a base that may no longer stand for its host.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

import pytest
from tests.system.agent_tools.world import (
    BASE_READING,
    BASE_STALENESS_S,
    CANDIDATE_READING,
    AgentToolWorld,
    Turn,
)

from vs_core.api import RunStatus
from vs_dst.api import StepLoopHosting, simulate

if TYPE_CHECKING:
    from pathlib import Path

STEP_LOOP = StepLoopHosting()
_RUNS = itertools.count()
FRESH = (0.0, BASE_STALENESS_S / 2)
"""How long the agent works before its repeat while the base it was compared with still counts."""
STALE = (BASE_STALENESS_S + 40.0, 3 * BASE_STALENESS_S + 20.0)
"""How long it works when the base has gone stale on the run clock by the time it submits."""


def _repeat_after(tmp_path: Path, thinks_s: float) -> AgentToolWorld:
    """A run whose agent submits a candidate, then submits it again after ``thinks_s`` seconds."""
    root = tmp_path / f"run-{next(_RUNS)}"
    root.mkdir()
    turns = (
        Turn(program=(("submit",),)),
        Turn(program=(("submit",),), edit=False, thinks_s=thinks_s),
    )
    run = simulate(AgentToolWorld(root, turns, total=2, measured=True), STEP_LOOP)
    assert run.finished, run.halted
    assert run.core.run.status == RunStatus.TERMINAL
    world = run.world
    assert isinstance(world, AgentToolWorld)
    assert world.halts == []
    assert world.errors == []
    return world


@pytest.mark.parametrize("thinks_s", FRESH)
def test_a_repeat_while_the_base_still_counts_is_told_the_framework_s_comparison(
    tmp_path: Path, thinks_s: float
) -> None:
    world = _repeat_after(tmp_path, thinks_s)

    (_, first), (_, repeat) = world.comparisons
    assert first is None, "a submission whose run has not ended has no comparison to tell"
    assert repeat is not None
    assert repeat["metric"] == "throughput"
    assert repeat["unit"] == "tokens/s"
    assert repeat["direction"] == "max"
    assert repeat["candidate_value"] == CANDIDATE_READING
    assert repeat["base_value"] == BASE_READING
    assert repeat["ratio"] == CANDIDATE_READING / BASE_READING
    assert repeat["settling"] is False


@pytest.mark.parametrize("thinks_s", STALE)
def test_a_repeat_after_the_base_went_stale_on_the_run_clock_is_told_no_comparison(
    tmp_path: Path, thinks_s: float
) -> None:
    world = _repeat_after(tmp_path, thinks_s)

    (_, first), (_, repeat) = world.comparisons
    assert first is None
    assert repeat is None, "a stale base is not a base the agent is told a verdict against"
