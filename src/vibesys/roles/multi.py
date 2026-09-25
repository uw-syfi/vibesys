"""Role catalog for the ``multi`` strategy.

Independent designer, profiler, implementer, and judge. Fallback factories
reproduce the content of the synthesized responses
``vibesys/loops/multi/turns.py`` built before this catalog existed. The
implementer role sets ``timeout_fallback`` distinctly from ``fallback``, so
``ctx.agents.turn`` still sends the original, different text for a
structured-parse failure versus a ``subprocess.TimeoutExpired``.
"""

from __future__ import annotations

from vibesys.profilers import PROFILER_DEFINITIONS, ProfilerKind
from vibesys.runtime import Fresh, Keyed, ReadOnly, Role, Writes
from vibesys.schemas import (
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
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


def _fallback_pre_round_decision() -> PreRoundDecision:
    return PreRoundDecision(
        need_profile=False, profile_focus="", reasoning="fallback: default to skip"
    )


def _fallback_profiler_summary() -> ProfilerSummary:
    return ProfilerSummary(
        analysis="Profiler produced no structured response.",
        bottlenecks="n/a",
        suggestions="Re-run profiling on the next round.",
    )


def _fallback_implementer() -> ImplementerResponse:
    return ImplementerResponse(
        summary="Implementer produced no structured response.",
        expected_behavior="unknown",
        hypothesis_outcome="inconclusive",
        evidence="The implementer output could not be parsed.",
        next_step="Recover retained evidence and return a schema-valid response before review.",
    )


def _timeout_fallback_implementer(timeout: float) -> ImplementerResponse:
    return ImplementerResponse(
        summary="Implementer invocation timed out.",
        expected_behavior="unknown",
        hypothesis_outcome="inconclusive",
        evidence=(
            f"The framework stopped the implementer after {timeout:g} seconds "
            "without a structured response."
        ),
        next_step="Inspect retained evidence and return a schema-valid response on retry.",
    )


def _fallback_judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="Judge produced no structured response.",
        feedback="No structured response received.",
        verdict=Verdict.FAIL,
    )


MULTI_PRE_ROUND_DECISION = Role(
    id="orchestrator",
    template="loops/multi/orchestrator_pre_round_prompt.j2",
    reply=PreRoundDecision,
    fallback=_fallback_pre_round_decision,
    access=ReadOnly(),
    session=Fresh(),
    message=(
        "Decide whether a profiling pass is needed before planning this round. "
        "Return only the JSON object."
    ),
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


def _profiler_role(kind: ProfilerKind) -> Role:
    """One role per profiler kind: each renders a different template.

    ``ProfilerDefinition.prompt_template`` resolves ``profilers/<kind>.j2``
    against multi's own folder, which falls back to
    ``prompts/shared/profilers/<kind>.j2`` (no per-strategy profiler prompts
    exist today); the role's template path keeps that same two shared-root
    resolution.
    """
    return Role(
        id="profiler",
        template=f"loops/multi/profilers/{kind.value}.j2",
        reply=ProfilerSummary,
        fallback=_fallback_profiler_summary,
        access=ReadOnly(),  # allow-list (bounded evidence dir) resolved per call by the caller
        session=Fresh(),
        message="Profile the server and return exactly one JSON object matching the schema above.",
    )


MULTI_PROFILERS: dict[ProfilerKind, Role] = {
    kind: _profiler_role(kind) for kind in PROFILER_DEFINITIONS
}

MULTI_IMPLEMENTER = Role(
    id="implementer",
    template="loops/multi/implementer_prompt.j2",
    reply=ImplementerResponse,
    fallback=_fallback_implementer,
    timeout_fallback=_timeout_fallback_implementer,
    access=Writes(),
    session=Keyed(scope=SessionScope.HYPOTHESIS),
    paid=True,
    filter_skills=True,
    message="Work persistently on the active hypothesis and return only the JSON object.",
)

MULTI_IMPLEMENTER_CONTINUATION = Role(
    id="implementer",
    template="loops/multi/implementer_continuation_prompt.j2",
    reply=ImplementerResponse,
    fallback=_fallback_implementer,
    timeout_fallback=_timeout_fallback_implementer,
    access=Writes(),
    session=Keyed(scope=SessionScope.HYPOTHESIS),
    paid=True,
    filter_skills=True,
    message="Execute the required continuation step and return only the JSON object.",
)

MULTI_JUDGE = Role(
    id="judge",
    template="loops/multi/judge_prompt.j2",
    reply=JudgeResponse,
    fallback=_fallback_judge,
    access=ReadOnly(),
    session=Fresh(),
    filter_skills=True,
    message="Review the implementation per the criteria above. Return only the JSON verdict.",
)

ALL_ROLES = (
    MULTI_PRE_ROUND_DECISION,
    MULTI_ORCHESTRATOR_PLAN,
    *MULTI_PROFILERS.values(),
    MULTI_IMPLEMENTER,
    MULTI_IMPLEMENTER_CONTINUATION,
    MULTI_JUDGE,
)
