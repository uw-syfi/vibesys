"""A run whose remaining time is shorter than a role's turn budget still asks its turns.

`[run] max_run_seconds` becomes core's run deadline, and core rejects a turn whose own
deadline lies past it. The per-role turn budgets (`planner_turn_seconds` 30 min,
`implementer_turn_seconds` 2 h by default) are upper bounds on one turn, not a
requirement that the run have that much time left: a turn asked near the end of a run
is bounded by the run's deadline. These tests drive whole runs on the real core with
scripted agents that answer at once, so time never runs out while they run.
"""

from __future__ import annotations

from collections import deque

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import FACTS, LIMITS, config
from tests.vibesys.orchestration.dynamic.strategy._shell import Run, drive_shell

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vs_core.api import RequestTurn, RunEnvelope
from vs_core.testing.drive import Harness


def _run(deadline_at: float) -> Run:
    executors = Executors(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )
    settings = config()
    harness = Harness(
        registry=dynamic_operation_registry(),
        facts=FACTS,
        limits=LIMITS,
        envelope_type=RunEnvelope[DynamicStrategyState],
        requirements=requirements_for(settings),
        deadline_at=deadline_at,
    )
    return drive_shell(
        DynamicStrategy(config=settings), executors, harness, reply_schemas(settings)
    )


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@example(deadline_at=1000.0)
@given(deadline_at=st.floats(min_value=100.0, max_value=config().implementer_turn_seconds))
def test_a_run_shorter_than_a_turn_budget_asks_its_turns_within_the_run(
    deadline_at: float,
) -> None:
    trace = _run(deadline_at)
    assert trace.halted is None, trace.halted
    result = trace.core.run.result
    assert result is not None
    assert result.outcome == "success", result.reason
    turns = [item for item in trace.decisions if isinstance(item, RequestTurn)]
    assert {item.turn.session.role_id.root for item in turns} >= {
        "dynamic-orchestrator",
        "dynamic-implementer",
    }
    assert all(item.turn.deadline_at <= deadline_at for item in turns)
