"""The tracker fails around board writes: the run lands the same board, never a duplicate.

The fake tracker raises around the n-th write, before it lands (the write did not happen) or
after (it landed and its acknowledgement was lost). Either way the executor reports an unknown
effect; core inspects, the strategy sends the same keyed write again, and the board ends up as
a fault-free run leaves it.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.issue_queue.strategy._executors import Executors
from tests.vibesys.orchestration.issue_queue.strategy._replies import evaluated, implemented, judged
from tests.vibesys.orchestration.issue_queue.strategy._world import (
    Outside,
    run_with_restarts,
)

from vs_core.api import RunStatus

if TYPE_CHECKING:
    from vs_issue_tracker.api import FaultPhase


def _script(failing: dict[int, str] | None = None) -> Executors:
    executors = Executors(
        implementer=deque(implemented(f"turn {n}") for n in range(4)),
        judge=deque([judged(passed=False), judged(bugs=("a bug",)), judged()]),
        perf=deque([evaluated()]),
    )
    calls = iter(range(100_000))
    schedule = failing or {}

    def faults(operation: str, phase: FaultPhase) -> None:
        if operation in {"get", "list"}:
            return
        if schedule.get(next(calls)) == phase:
            message = f"tracker {phase} {operation}"
            raise ConnectionError(message)

    executors.tracker.faults = faults
    return executors


def _board(executors: Executors) -> list[tuple[object, ...]]:
    return [
        (issue.id, issue.status.value, issue.attempts, [e.action for e in issue.history])
        for issue in executors.tracker.list()
    ]


def _run(failing: dict[int, str]) -> tuple[Executors, object]:
    script = _script(failing)
    core = run_with_restarts(script, Outside(), [])
    assert core.run.status == RunStatus.TERMINAL
    return script, core.run.result


CLEAN, CLEAN_RESULT = _run({})


@settings(max_examples=12, deadline=None, derandomize=True, database=None)
@given(
    failing=st.dictionaries(
        st.integers(min_value=0, max_value=14), st.sampled_from(["before", "after"]), max_size=3
    )
)
def test_a_tracker_failing_around_any_write_leaves_the_board_of_a_clean_run(
    failing: dict[int, str],
) -> None:
    script, result = _run(failing)
    assert result == CLEAN_RESULT
    assert _board(script) == _board(CLEAN)
