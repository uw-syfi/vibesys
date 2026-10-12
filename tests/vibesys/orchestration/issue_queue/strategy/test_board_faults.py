"""The tracker fails around board writes: the run lands the same board, never a duplicate.

The fake tracker raises around the n-th write, before it lands (the write did not happen) or
after (it landed and its acknowledgement was lost). Either way the executor reports an unknown
effect; core inspects, the strategy sends the same keyed write again, and the board ends up as
a fault-free run leaves it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.issue_queue.strategy.world import (
    IssueQueueWorld,
    evaluated,
    implemented,
    judged,
)

from vs_dst.api import SimulatedRun, simulate

if TYPE_CHECKING:
    from vs_issue_tracker.api import FaultPhase


def _world(failing: dict[int, FaultPhase] | None = None) -> IssueQueueWorld:
    return IssueQueueWorld(
        implementer=[implemented(f"turn {n}") for n in range(4)],
        judge=[judged(passed=False), judged(bugs=("a bug",)), judged()],
        perf=[evaluated()],
        tracker_faults=failing,
    )


def _run(failing: dict[int, FaultPhase]) -> tuple[IssueQueueWorld, SimulatedRun]:
    world = _world(failing)
    run = simulate(world)
    assert run.finished
    assert run.halted is None
    assert not run.liveness_violations
    return world, run


CLEAN, CLEAN_RUN = _run({})


@settings(max_examples=12, deadline=None, derandomize=True, database=None)
@given(
    failing=st.dictionaries(
        st.integers(min_value=0, max_value=14), st.sampled_from(["before", "after"]), max_size=3
    )
)
def test_a_tracker_failing_around_any_write_leaves_the_board_of_a_clean_run(
    failing: dict[int, FaultPhase],
) -> None:
    world, run = _run(failing)
    assert run.result == CLEAN_RUN.result
    assert world.history() == CLEAN.history()
