"""A run whose remaining time is shorter than a role's turn budget still asks its turns.

`[run] max_run_seconds` becomes core's run deadline, and core rejects a turn whose own
deadline lies past it. The per-role turn budgets (`planner_turn_seconds` 30 min,
`implementer_turn_seconds` 2 h by default) are upper bounds on one turn, not a
requirement that the run have that much time left: a turn asked near the end of a run
is bounded by the run's deadline. These tests drive whole runs on the real core with
scripted agents that answer at once, so time never runs out while they run.
"""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.strategy._run import config
from tests.vibesys.orchestration.dynamic.strategy.world import DynamicWorld

from vs_core.api import DispatchTurn, RequestTurn, ResumeSessionTurn
from vs_dst.api import SimulatedRun, simulate


def _run(deadline_at: float) -> SimulatedRun:
    return simulate(DynamicWorld(deadline_at=deadline_at))


@settings(max_examples=2, deadline=None, derandomize=True, database=None)
@example(deadline_at=1000.0)
@given(deadline_at=st.floats(min_value=100.0, max_value=config().implementer_turn_seconds))
def test_a_run_shorter_than_a_turn_budget_asks_its_turns_within_the_run(
    deadline_at: float,
) -> None:
    trace = _run(deadline_at)
    assert trace.finished
    assert not trace.liveness_violations
    assert trace.halted is None, trace.halted
    result = trace.core.run.result
    assert result is not None
    assert result.outcome == "success", result.reason
    turns = [item for item in trace.decisions if isinstance(item, RequestTurn)]
    assert {item.turn.session.role_id.root for item in turns} >= {
        "dynamic-orchestrator",
        "dynamic-implementer",
    }
    # Core bounds what it dispatches; the strategy's own turn budget stays an upper bound.
    dispatched = [
        intent.request
        for intent in trace.core.intents.intents
        if isinstance(intent.request, DispatchTurn | ResumeSessionTurn)
    ]
    assert dispatched
    assert all(request.deadline_at <= deadline_at for request in dispatched)
