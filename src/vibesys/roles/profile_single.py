"""Role catalog for the ``profile_single`` strategy.

One designer and one combined implementer/reviewer/profiler, same shape as
``single``'s roles. ``profile_single`` renders from its own
``prompts/loops/profile_single/`` folder (its templates add the component
guidance/attribution context ``single`` doesn't have), so it gets its own
roles: "different prompts => different roles" (no multi-mode roles).
"""

from __future__ import annotations

from vibesys.runtime import Fresh, Keyed, ReadOnly, Role, Writes
from vibesys.schemas import (
    CandidateDisposition,
    OrchestratorPlan,
    SingleAgentRoundResponse,
    Verdict,
)
from vs_agent.api import SessionScope


def _fallback_plan() -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "task": "Re-check minimal server boots and /health returns 200.",
            "pass_criteria": "/health returns 200.",
            "reasoning": "fallback: orchestrator produced no structured response",
        }
    )


def _fallback_combined() -> SingleAgentRoundResponse:
    """Used when the structured reply could not be parsed."""
    return SingleAgentRoundResponse(
        summary="Single-agent produced no structured response.",
        expected_behavior="unknown",
        self_review="No structured response received.",
        feedback="No structured response received.",
        verdict=Verdict.FAIL,
        bottlenecks="",
        suggestions="",
        profile_analysis="",
        candidate_disposition=CandidateDisposition.UNASSESSED,
    )


def _timeout_fallback_combined(timeout: float) -> SingleAgentRoundResponse:
    """Used when the turn hit ``subprocess.TimeoutExpired`` (a6e361c1 text)."""
    return SingleAgentRoundResponse(
        summary="Single-agent invocation timed out.",
        expected_behavior="unknown",
        self_review=(
            f"The framework stopped the agent after {timeout:g} seconds "
            "without a structured response."
        ),
        feedback="Inspect retained evidence and return a schema-valid response on retry.",
        verdict=Verdict.FAIL,
        bottlenecks="",
        suggestions="",
        profile_analysis="",
        candidate_disposition=CandidateDisposition.UNASSESSED,
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

PROFILE_SINGLE_COMBINED = Role(
    id="implementer",
    template="loops/profile_single/single_agent_round_prompt.j2",
    reply=SingleAgentRoundResponse,
    fallback=_fallback_combined,
    timeout_fallback=_timeout_fallback_combined,
    access=Writes(),
    session=Keyed(scope=SessionScope.HYPOTHESIS),
    paid=True,
    filter_skills=True,
    message=(
        "Carry out the orchestrator's task above end-to-end "
        "(implement, self-judge, profile) and return only the JSON object."
    ),
)

ALL_ROLES = (PROFILE_SINGLE_ORCHESTRATOR_PLAN, PROFILE_SINGLE_COMBINED)
