"""Plan-role family: the round-planning "designer" role for each strategy.

Every plan role shares the same reply type (``OrchestratorPlan``, still in
``vibesys.schemas`` pending its move to ``search/hypothesis``) and the same
fallback content, but renders from its own strategy's template, so each gets
its own ``Role`` value (different prompt => different role).
"""

from __future__ import annotations

from vibesys.runtime import Fresh, ReadOnly, Role
from vibesys.schemas import OrchestratorPlan


def _fallback_plan() -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "task": "Re-check minimal server boots and /health returns 200.",
            "pass_criteria": "/health returns 200.",
            "reasoning": "fallback: orchestrator produced no structured response",
        }
    )


MULTI_ORCHESTRATOR_PLAN = Role(
    id="orchestrator",
    template="loops/multi/orchestrator_plan_prompt.j2",
    reply=OrchestratorPlan,
    fallback=_fallback_plan,
    access=ReadOnly(),  # allow-list (roadmap index) resolved per call by the caller
    session=Fresh(),
    filter_skills=True,
    message="Produce this round's plan. Return only the JSON object.",
)

SINGLE_ORCHESTRATOR_PLAN = Role(
    id="orchestrator",
    template="loops/single/orchestrator_plan_prompt.j2",
    reply=OrchestratorPlan,
    fallback=_fallback_plan,
    access=ReadOnly(),  # allow-list (roadmap index) resolved per call by the caller
    session=Fresh(),
    message="Produce this round's plan. Return only the JSON object.",
)

PROFILE_SINGLE_ORCHESTRATOR_PLAN = Role(
    id="orchestrator",
    template="loops/profile_single/orchestrator_plan_prompt.j2",
    reply=OrchestratorPlan,
    fallback=_fallback_plan,
    access=ReadOnly(),  # allow-list (roadmap index) resolved per call by the caller
    session=Fresh(),
    message="Produce this round's plan. Return only the JSON object.",
)

ALL_ROLES = (
    MULTI_ORCHESTRATOR_PLAN,
    SINGLE_ORCHESTRATOR_PLAN,
    PROFILE_SINGLE_ORCHESTRATOR_PLAN,
)
