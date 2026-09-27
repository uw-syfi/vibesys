"""Designer planning policy for the real single-agent orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.errors import InvalidPlanError
from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.single.agents import DESIGNER
from vibesys.orchestration.single.prompts import render_plan_prompt
from vibesys.schemas import SkillResourceSelection, normalize_hypothesis_title
from vs_runtime.api import RunHost, SkillCatalogError, SkillResourceRequest, StructuredResponseError

if TYPE_CHECKING:
    from vibesys.orchestration.hypothesis import HypothesisSearch, HypothesisState
    from vibesys.orchestration.single.models import PlanContext
    from vs_runtime.api import Workspace


@dataclass(frozen=True, slots=True)
class DesignerPlanRequest:
    """All changing evidence needed for one new-hypothesis planning turn."""

    round_number: int
    state: HypothesisState
    context: PlanContext
    workspace: Workspace

    def __post_init__(self) -> None:
        """Reject a round that cannot name its fallback hypothesis."""
        if self.round_number < 1:
            message = "designer round_number must be positive"
            raise ValueError(message)


def _fallback_plan() -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "task": "Re-check minimal server boots and /health returns 200.",
            "pass_criteria": "/health returns 200.",
            "reasoning": "fallback: orchestrator produced no structured response",
        }
    )


def _validate_plan(
    plan: OrchestratorPlan, state: HypothesisState, search: HypothesisSearch
) -> None:
    updates = [item.hypothesis_id for item in plan.hypothesis_updates]
    if len(updates) != len(set(updates)):
        raise InvalidPlanError.duplicate_updates()
    if plan.hypothesis_id in updates:
        raise InvalidPlanError.self_reference()
    if state.by_id(plan.hypothesis_id) is not None:
        raise InvalidPlanError.reused_id(plan.hypothesis_id)
    search.validate_updates(state, plan.hypothesis_updates)


def _correction_message(plan: OrchestratorPlan, error: ValueError) -> str:
    rejected = ", ".join(sorted({item.hypothesis_id for item in plan.hypothesis_updates}))
    return (
        f"Your previous plan was rejected: {error}. "
        f"It proposed hypothesis_id {plan.hypothesis_id!r} and named "
        f"{rejected or '(no)'} in hypothesis_updates. "
        "A hypothesis_id names one investigation permanently: never reuse "
        "an identifier used earlier in this run, and choose one that has "
        "not appeared before. hypothesis_updates may name each prior "
        "hypothesis at most once, and never the new one. "
        "Produce a corrected plan for this round. Return only the JSON object."
    )


async def _resolve_recommendations(host: RunHost, plan: OrchestratorPlan) -> None:
    if not plan.recommended_skills:
        return
    requests = tuple(
        SkillResourceRequest(
            name=item.skill,
            resource_paths=tuple(item.resource_paths),
            purpose=item.purpose,
        )
        for item in plan.recommended_skills
    )
    try:
        result = await host.skills.resolve(requests)
    except SkillCatalogError as error:
        host.log(f"[skills] ignored recommendations because the catalog is invalid: {error}")
        plan.recommended_skills = []
        return
    for diagnostic in result.diagnostics:
        host.log(f"[skills] {diagnostic}")
    plan.recommended_skills = [
        SkillResourceSelection(
            skill=item.name,
            resource_paths=[path.removeprefix(f"{item.name}/") for path in item.resource_paths],
            purpose=item.purpose,
        )
        for item in result.resolved
    ]


async def request_plan(
    host: RunHost, search: HypothesisSearch, request: DesignerPlanRequest
) -> OrchestratorPlan:
    """Return one normalized, state-valid plan after at most one correction.

    A single run-owned designer session carries the rejected plan into its
    correction turn. Resource cleanup occurs on success, failure, and cancellation.
    """
    session = await host.agents.create_session(
        DESIGNER,
        workspace=request.workspace,
        writable_paths=(request.context.roadmap_location,),
    )
    try:
        try:
            plan = await session.turn(
                render_plan_prompt(request.context), response=OrchestratorPlan
            )
        except StructuredResponseError:
            plan = _fallback_plan()
        for attempt in range(2):
            plan.hypothesis_id = (
                plan.hypothesis_id.strip() or f"hypothesis-{request.round_number:04d}"
            )
            plan.title = normalize_hypothesis_title(plan.title)
            try:
                _validate_plan(plan, request.state, search)
            except ValueError as error:
                if attempt:
                    raise
                host.log(f"[orchestrator] plan rejected ({error}); reprompting once")
                try:
                    plan = await session.turn(
                        _correction_message(plan, error), response=OrchestratorPlan
                    )
                except StructuredResponseError:
                    plan = _fallback_plan()
                continue
            await _resolve_recommendations(host, plan)
            return plan
        message = "designer correction loop exited without a validated plan"
        raise RuntimeError(message)
    finally:
        await session.close()


__all__ = ["DesignerPlanRequest", "request_plan"]
