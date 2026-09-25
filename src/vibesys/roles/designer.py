"""Plan-role family: the round-planning "designer" role for each strategy.

Every plan role shares the same reply type (``OrchestratorPlan``, still in
``vibesys.schemas`` pending its move to ``search/hypothesis``) and the same
fallback content, but renders from its own strategy's template, so each gets
its own ``Role`` value (different prompt => different role).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from vibesys.evaluators.perf_reply import ProfilerSummary
from vibesys.runtime import Fresh, ReadOnly, Role
from vibesys.schemas import OrchestratorPlan


class PlanContext(BaseModel):
    """Shared context for every strategy's designer/plan role.

    ``multi``, ``single``, and ``profile_single`` each declare their own
    ``Role`` (different template files), but all three templates read the
    exact same free-variable set, including the optional profile-focus
    addendum (``active_component``/``ledger_text``/``ranked_bottlenecks``):
    a plain strategy passes ``PlainGuidance``'s empty ``{}``, so those three
    fields default to their "no focus" values here instead. One context
    model serves all three roles (and ``profile_multi``, which reuses
    ``MULTI_ORCHESTRATOR_PLAN`` outright).
    """

    model_config = ConfigDict(frozen=True)

    objective_location: str
    profiler_summary: ProfilerSummary | None
    regression_info: str | None
    exhaustion_info: str | None
    progress_location: str
    roadmap_location: str
    pareto_archive_location: str
    plateau_warning: str | None
    domain_orchestrator: str
    runtime_notes: str
    framework_benchmark_enabled: bool
    official_eval_every: int
    provisional_candidates: int
    official_eval_cadence_due: bool
    active_component: str | None = None
    ledger_text: str | None = None
    ranked_bottlenecks: list[dict[str, object]] = Field(default_factory=list)


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
    context=PlanContext,
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
    context=PlanContext,
    access=ReadOnly(),  # allow-list (roadmap index) resolved per call by the caller
    session=Fresh(),
    message="Produce this round's plan. Return only the JSON object.",
)

PROFILE_SINGLE_ORCHESTRATOR_PLAN = Role(
    id="orchestrator",
    template="loops/profile_single/orchestrator_plan_prompt.j2",
    reply=OrchestratorPlan,
    fallback=_fallback_plan,
    context=PlanContext,
    access=ReadOnly(),  # allow-list (roadmap index) resolved per call by the caller
    session=Fresh(),
    message="Produce this round's plan. Return only the JSON object.",
)

ALL_ROLES = (
    MULTI_ORCHESTRATOR_PLAN,
    SINGLE_ORCHESTRATOR_PLAN,
    PROFILE_SINGLE_ORCHESTRATOR_PLAN,
)
