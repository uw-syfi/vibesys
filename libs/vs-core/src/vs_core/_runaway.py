"""A warning for an agent that asks for an egregious number of evaluations.

Evaluations have no allowance: a plan cannot cap them and core admits every call it can
(`vs_core._measurements`). What it can do is notice a runaway. An attempt whose agent has had
`TaskTerms.runaway_evaluations` calls admitted (each a submission the call allocated, never a
repeat of one it already holds) is recorded once, and the run says so. The record is a fact of
the state, so a resumed run does not say it again, and nothing waits on it or refuses because of it.

The count is the attempt's, across its turns, so it can only be later than a count of one turn:
a warning is therefore never wrong about a turn it names, and only an egregious count reaches it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.measurement import Runaway

if TYPE_CHECKING:
    from .types.common import Scope
    from .types.evaluation import EvaluationState
    from .types.kernel import EvaluationContext

__all__ = ["runaway_noted"]


def runaway_noted(
    state: EvaluationState, context: EvaluationContext, scope: Scope
) -> EvaluationState:
    """``state`` plus the runaway of ``scope``, when its admitted calls first reach the limit."""
    task = context.run.facts.task
    if task is None or any(held.scope == scope for held in state.runaways):
        return state
    evaluations = sum(
        1
        for call in state.agent_calls
        if call.scope == scope and call.request_id is not None and not call.repeat
    )
    if evaluations < task.runaway_evaluations:
        return state
    runaway = Runaway(scope=scope, evaluations=evaluations, limit=task.runaway_evaluations)
    return state.model_copy(update={"runaways": (*state.runaways, runaway)})
