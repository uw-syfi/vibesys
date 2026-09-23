"""Built-in role declarations preserve the shared context's invocation contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from vibesys.loops.agent.roles import BuiltInAgentRoles
from vibesys.schemas import OrchestratorPlan
from vs_agent.api import AgentSessionKey, SessionScope

if TYPE_CHECKING:
    from vibesys.run.protocol import LoopContext


def test_declared_implementer_routes_hypothesis_session_through_shared_context() -> None:
    context = MagicMock()
    plan = OrchestratorPlan(
        task="repair",
        pass_criteria="passes",  # noqa: S106
        reasoning="evidence",
    )
    context.invoke.return_value = plan
    agents = BuiltInAgentRoles.bind(cast("LoopContext", context))

    result = agents.implementer.turn_structured(
        "implement plan",
        system_prompt="implementation instructions",
        response_cls=OrchestratorPlan,
        fallback_factory=lambda: plan,
        label="round-2-retry-1-implementer",
        session_key=AgentSessionKey(SessionScope.HYPOTHESIS, "H-2"),
        reuse_session=True,
    )

    assert result is plan
    assert (agents.orchestrator.id, agents.implementer.id, agents.judge.id, agents.profiler.id) == (
        "orchestrator",
        "implementer",
        "judge",
        "profiler",
    )
    context.invoke.assert_called_once_with(
        kind="implementer",
        system_prompt="implementation instructions",
        user_prompt="implement plan",
        response_cls=OrchestratorPlan,
        fallback_factory=context.invoke.call_args.kwargs["fallback_factory"],
        round_label="round-2-retry-1-implementer",
        session_key=AgentSessionKey(SessionScope.HYPOTHESIS, "H-2"),
        reuse_session=True,
        mcp_servers=None,
    )
