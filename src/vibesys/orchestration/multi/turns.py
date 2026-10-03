"""Explicit designer, profiler, implementer, and judge conversations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.constants import DomainName
from vibesys.orchestration.domains.base import DomainRole
from vibesys.orchestration.domains.registry import resolve_domain
from vibesys.orchestration.domains.rendering import render_domain_section
from vibesys.orchestration.hypothesis import (
    InvalidPlanError,
    OrchestratorPlan,
    SkillResourceSelection,
    normalize_hypothesis_title,
)
from vibesys.orchestration.multi.agents import DESIGNER, IMPLEMENTER, JUDGE, PROFILER
from vibesys.orchestration.multi.contracts import (
    ImplementerContext,
    ImplementerContinuationContext,
    ImplementerResponse,
    JudgeContext,
    JudgeResponse,
    PlanContext,
    PreRoundContext,
    PreRoundDecision,
    ProfilerCampaign,
    ProfilerContext,
)
from vibesys.orchestration.multi.prompts import (
    render_archive_conflict,
    render_continuation_prompt,
    render_implementer_prompt,
    render_judge_prompt,
    render_pareto_guard,
    render_plan_prompt,
    render_pre_round_prompt,
    render_profiler_prompt,
)
from vibesys.orchestration.profilers import (
    ProfilerKind,
    ProfilerSummary,
    UnsupportedProfilerError,
    profiler_definition,
)
from vibesys.orchestration.prompts import render_plan_correction
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.structured_turn import (
    TurnFailed,
    attempt_structured_turn,
    structured_turn,
)
from vs_runtime.api import (
    ResolvedSkillResources,
    Run,
    SkillCatalogError,
    SkillResourceRequest,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.hypothesis import (
        ArchiveConflict,
        AttemptState,
        CarryOver,
        HypothesisSearch,
        HypothesisState,
    )
    from vibesys.orchestration.hypothesis.state import Hypothesis, RoundRecord
    from vibesys.orchestration.multi.files import MultiFiles
    from vibesys.orchestration.multi.models import MultiOptions, ProfileGuidedMultiOptions
    from vibesys.orchestration.profile_focus import FocusView
    from vs_runtime.api import AgentBinding, AgentSession, Workspace


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Evidence supplied to one independent designer plan conversation."""

    round_number: int
    state: HypothesisState
    carry: CarryOver
    profiler_summary: ProfilerSummary | None
    plateau_warning: str | None
    provisional_candidates: int
    workspace: Workspace
    guidance: FocusView | None


@dataclass(frozen=True, slots=True)
class AttemptRequest:
    """Multi policy facts shared by one implementer and its judge."""

    round_number: int
    plan: OrchestratorPlan
    planned_official_reason: str | None
    records: tuple[RoundRecord, ...]
    active_hypothesis: Hypothesis
    workspace: Workspace
    guidance: FocusView | None


async def _resolve_skills(
    run: Run,
    selections: list[SkillResourceSelection],
) -> tuple[list[SkillResourceSelection], list[ResolvedSkillResources]]:
    if not selections:
        return [], []
    requests = tuple(
        SkillResourceRequest(
            name=item.skill,
            resource_paths=tuple(item.resource_paths),
            purpose=item.purpose,
        )
        for item in selections
    )
    try:
        result = await run.skills.resolve(requests)
    except SkillCatalogError as error:
        run.observations.warning(
            f"[skills] ignored recommendations because the catalog is invalid: {error}"
        )
        return [], []
    for diagnostic in result.diagnostics:
        run.observations.warning(f"[skills] {diagnostic}")
    portable = [
        SkillResourceSelection(
            skill=item.name,
            resource_paths=[path.removeprefix(f"{item.name}/") for path in item.resource_paths],
            purpose=item.purpose,
        )
        for item in result.resolved
    ]
    return portable, list(result.resolved)


class MultiAgentTurns:
    """Render policy prompts and own the explicit conversation topology."""

    def __init__(
        self,
        run: Run,
        options: MultiOptions | ProfileGuidedMultiOptions,
        search: HypothesisSearch,
        files: MultiFiles,
    ) -> None:
        """Bind run effects and pure policy without opening conversations."""
        self.run = run
        self.options = options
        self.search = search
        self.files = files
        self.workspace = run.workspaces.root
        self.domain = resolve_domain(DomainName(run.facts.domain_id))
        self.modality = options.modality
        if self.modality is None and self.domain.name is DomainName.LLM_SERVING:
            self.modality = "text_generation"
        self._workers: dict[str, AgentSession] = {}
        self._closed = False

    def _domain_context(self, *, trusted_commands: bool = True) -> dict[str, object]:
        facts = self.run.facts
        return {
            "modality": self.modality,
            "interface": self.options.interface,
            "reference_path": facts.reference_location,
            "benchmark_command": facts.benchmark_command if trusted_commands else None,
            "accuracy_command": facts.accuracy_command if trusted_commands else None,
            "runtime_notes": facts.environment_notes,
            "profile_execution": facts.profile_execution.value,
            "workspace_sources": tuple(item.model_dump() for item in facts.workspace_sources),
        }

    async def pre_round(
        self,
        round_number: int,
        carry: CarryOver,
        *,
        has_history: bool,
    ) -> PreRoundDecision:
        """Ask a fresh designer conversation whether specialist evidence is useful."""
        facts = self.run.facts
        context = PreRoundContext(
            objective_location=facts.objective_location,
            regression_info=carry.regression,
            exhaustion_info=carry.exhaustion,
            progress_location=self.files.progress_location,
            profiler_kind=facts.profiler_id,
            profile_execution=facts.profile_execution.value,
            has_history=has_history,
        )
        session = await self.run.agents.create_session(
            DESIGNER,
            workspace=self.workspace,
            writable_paths=(self.files.roadmap_location,),
        )
        try:
            decision = await structured_turn(
                session, render_pre_round_prompt(context), PreRoundDecision
            )
        finally:
            await session.close()
        self.files.note_pre_round(round_number, decision)
        return decision

    def _plan_context(self, request: PlanRequest) -> PlanContext:
        facts = self.run.facts
        return PlanContext(
            objective_location=facts.objective_location,
            profiler_summary=request.profiler_summary,
            regression_info=request.carry.regression,
            exhaustion_info=request.carry.exhaustion,
            progress_location=self.files.progress_location,
            roadmap_location=self.files.roadmap_location,
            pareto_archive_location=self.files.pareto_location,
            plateau_warning=request.plateau_warning,
            domain_orchestrator=render_domain_section(
                self.domain,
                DomainRole.ORCHESTRATOR,
                **self._domain_context(),
            ),
            runtime_notes=facts.environment_notes,
            framework_benchmark_enabled=facts.benchmark_configured,
            official_eval_every=self.options.official_eval_every,
            provisional_candidates=request.provisional_candidates,
            official_eval_cadence_due=(
                request.provisional_candidates + 1 >= self.options.official_eval_every
            ),
            active_component=(
                request.guidance.active_component if request.guidance is not None else None
            ),
            ledger=(request.guidance.ledger if request.guidance is not None else None),
            ranked_bottlenecks=(
                [
                    {
                        "component": item.name,
                        "cost_share": item.share * 100,
                        "evidence": item.evidence,
                    }
                    for item in request.guidance.ranked_bottlenecks
                ]
                if request.guidance is not None
                else []
            ),
        )

    def _validate_plan(self, plan: OrchestratorPlan, state: HypothesisState) -> None:
        updates = [item.hypothesis_id for item in plan.hypothesis_updates]
        if len(updates) != len(set(updates)):
            raise InvalidPlanError.duplicate_updates()
        if plan.hypothesis_id in updates:
            raise InvalidPlanError.self_reference()
        if state.by_id(plan.hypothesis_id) is not None:
            raise InvalidPlanError.reused_id(plan.hypothesis_id)
        self.search.validate_updates(state, plan.hypothesis_updates)

    async def plan(self, request: PlanRequest) -> OrchestratorPlan:
        """Return one normalized, state-valid plan after at most one correction."""
        context = self._plan_context(request)
        session = await self.run.agents.create_session(
            DESIGNER,
            workspace=request.workspace,
            writable_paths=(self.files.roadmap_location,),
        )
        try:
            plan = await structured_turn(session, render_plan_prompt(context), OrchestratorPlan)
            for attempt in range(2):
                plan.hypothesis_id = (
                    plan.hypothesis_id.strip() or f"hypothesis-{request.round_number:04d}"
                )
                plan.title = normalize_hypothesis_title(plan.title)
                try:
                    self._validate_plan(plan, request.state)
                except ValueError as error:
                    if attempt:
                        raise
                    feedback = render_plan_correction(
                        error=str(error),
                        hypothesis_id=plan.hypothesis_id,
                        updated_hypothesis_ids=[
                            item.hypothesis_id for item in plan.hypothesis_updates
                        ],
                        require_unseen_id=False,
                    )
                    self.run.observations.warning(
                        f"[orchestrator] plan rejected ({error}); reprompting once"
                    )
                    plan = await structured_turn(session, feedback, OrchestratorPlan)
                    continue
                portable, _ = await _resolve_skills(self.run, plan.recommended_skills)
                plan.recommended_skills = portable
                self.files.write_plan(request.round_number, plan)
                self.files.note_plan(request.round_number, plan)
                return plan
            message = "designer correction loop exited without a validated plan"
            raise RuntimeError(message)
        finally:
            await session.close()

    async def profile(self, round_number: int, focus: str) -> ProfilerSummary | None:
        """Collect optional evidence in one fresh bounded-write conversation."""
        facts = self.run.facts
        kind = ProfilerKind(facts.profiler_id)
        if kind is ProfilerKind.NONE:
            return None
        definition = profiler_definition(kind)
        if definition.requires_domain_torch_support and not self.domain.supports_torch_profiler:
            raise UnsupportedProfilerError
        artifact = self.files.profiler_location(round_number).rstrip("/")
        context = ProfilerContext(
            profile_focus=focus,
            benchmark_command=facts.benchmark_command,
            modality=self.modality,
            domain_profiler=render_domain_section(
                self.domain,
                DomainRole.PROFILER,
                **self._domain_context(),
            ),
            runtime_notes=facts.environment_notes,
            profile_execution=facts.profile_execution.value,
            objective=None,
            profiler_support_name=definition.support_name,
            profiler_mcp_name=definition.mcp_name,
            campaign=ProfilerCampaign(
                progress_location=self.files.progress_location,
                evidence_location=artifact,
            ),
        )
        session = await self.run.agents.create_session(
            PROFILER,
            workspace=self.workspace,
            writable_paths=(artifact,),
        )
        try:
            summary = await structured_turn(
                session, render_profiler_prompt(kind.value, context), ProfilerSummary
            )
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-920440 [BLE001]; profiling is advisory; a failed specialist must not abort the policy round.
            self.run.observations.warning(f"[profiler] failed: {error}")
            return None
        finally:
            await session.close()
        self.files.note_profile(round_number, summary)
        return summary

    async def _implementer_context(
        self,
        request: AttemptRequest,
        state: AttemptState,
    ) -> ImplementerContext:
        plan = request.plan
        hypothesis = request.active_hypothesis
        plan.recommended_skills, resolved = await _resolve_skills(self.run, plan.recommended_skills)
        plan_location = self.files.write_plan(request.round_number, plan)
        facts = self.run.facts
        return ImplementerContext(
            reference_path=facts.reference_location,
            modality=self.modality,
            interface=self.options.interface,
            domain_implementer=render_domain_section(
                self.domain,
                DomainRole.IMPLEMENTER,
                **self._domain_context(),
            ),
            objective_location=facts.objective_location,
            plan_artifact_location=plan_location,
            progress_location=self.files.progress_location,
            pareto_archive_location=self.files.pareto_location,
            validation_location=self.files.validation_location,
            validation_recipe_contract_location=self.files.validation_schema_location,
            retry=state.retry,
            feedback=state.feedback,
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
            runtime_notes=facts.environment_notes,
            framework_benchmark_enabled=facts.benchmark_configured,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            recommended_skills=resolved,
            prior_attempt_artifact_locations=self.files.prior_implementer_locations(
                request.round_number
            ),
            active_component=(
                request.guidance.active_component if request.guidance is not None else None
            ),
        )

    async def _continuation_context(
        self,
        request: AttemptRequest,
        state: AttemptState,
    ) -> ImplementerContinuationContext:
        plan = request.plan
        hypothesis = request.active_hypothesis
        plan.recommended_skills, resolved = await _resolve_skills(self.run, plan.recommended_skills)
        plan_location = self.files.write_plan(request.round_number, plan)
        return ImplementerContinuationContext(
            hypothesis_id=plan.hypothesis_id,
            objective_location=self.run.facts.objective_location,
            plan_artifact_location=plan_location,
            progress_location=self.files.progress_location,
            pareto_archive_location=self.files.pareto_location,
            validation_location=self.files.validation_location,
            validation_recipe_contract_location=self.files.validation_schema_location,
            runtime_notes=self.run.facts.environment_notes,
            prior_attempt_artifact_locations=self.files.prior_implementer_locations(
                request.round_number
            ),
            retry=state.retry,
            continuation_step=hypothesis.next_step or plan.task,
            feedback=state.feedback,
            recommended_skills=resolved,
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
        )

    async def implement(
        self,
        request: AttemptRequest,
        state: AttemptState,
    ) -> ImplementerResponse | TurnFailed:
        """Run one attempt in the hypothesis's context-preserving conversation.

        A reply that stays invalid after its correction, or a timed-out turn,
        returns :class:`TurnFailed`; the caller records it as a failed attempt.
        """
        if self._closed:
            message = "multi-agent turns are closed"
            raise RuntimeError(message)
        plan = request.plan
        continuing = bool(request.active_hypothesis.next_step)
        context: BaseModel = (
            await self._continuation_context(request, state)
            if continuing
            else await self._implementer_context(request, state)
        )
        session = self._workers.get(plan.hypothesis_id)
        if session is None:
            session = await self.run.agents.create_session(
                IMPLEMENTER,
                workspace=request.workspace,
                member_id=plan.hypothesis_id,
            )
            self._workers[plan.hypothesis_id] = session
        elif session.workspace != request.workspace:
            message = f"hypothesis {plan.hypothesis_id!r} changed workspace"
            raise ValueError(message)
        prompt = (
            render_continuation_prompt(context)
            if isinstance(context, ImplementerContinuationContext)
            else render_implementer_prompt(context)
        )
        response = await attempt_structured_turn(session, prompt, ImplementerResponse)
        if isinstance(response, TurnFailed):
            self.files.note_implementation_failed(request.round_number, state.retry, response)
            return response
        updates, _ = await _resolve_skills(self.run, response.skill_context_updates)
        if updates:
            plan.recommended_skills, _ = await _resolve_skills(
                self.run,
                [*plan.recommended_skills, *updates],
            )
            self.files.write_plan(request.round_number, plan)
        self.files.write_implementer(request.round_number, state.retry, response)
        self.files.note_implementation(request.round_number, state.retry, response)
        return response

    async def review(
        self,
        request: AttemptRequest,
        state: AttemptState,
        conflict: ArchiveConflict | None,
    ) -> JudgeResponse:
        """Audit one implementation using a fresh read-only conversation."""
        implementation = state.implementation
        if implementation is None:
            message = "judge requires an implementer response"
            raise RuntimeError(message)
        facts = self.run.facts
        plan_location = self.files.write_plan(request.round_number, request.plan)
        evidence_location = self.files.write_implementer(
            request.round_number,
            state.retry,
            implementation,
        )
        context = JudgeContext(
            domain_judge=render_domain_section(
                self.domain,
                DomainRole.JUDGE,
                **self._domain_context(trusted_commands=False),
            ),
            framework_benchmark_enabled=facts.benchmark_configured,
            framework_revert_applied=request.active_hypothesis.revert_applied,
            framework_revert_round=request.active_hypothesis.parent_round,
            framework_revert_commit=request.active_hypothesis.revert_commit,
            gate_approved_evaluation_artifact=(
                request.active_hypothesis.gate_approved_evaluation_artifact
            ),
            gate_approved_perf_metric=request.active_hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=request.active_hypothesis.gate_approved_perf_unit,
            gate_revalidation_pending=request.active_hypothesis.gate_revalidation_pending,
            implementer_artifact_location=evidence_location,
            interface=self.options.interface,
            modality=self.modality,
            objective_location=facts.objective_location,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            pareto_archive_conflict=conflict,
            pareto_archive_location=self.files.pareto_location,
            plan_artifact_location=plan_location,
            progress_location=self.files.progress_location,
            retry=state.retry,
            runtime_notes=facts.environment_notes,
            validation_location=self.files.validation_location,
            validation_recipe_contract_location=self.files.validation_schema_location,
        )
        session = await self.run.agents.create_session(JUDGE, workspace=request.workspace)
        try:
            response = await structured_turn(session, render_judge_prompt(context), JudgeResponse)
        finally:
            await session.close()
        if response.verdict is Verdict.PASS and conflict is not None:
            response = response.model_copy(
                update={
                    "analysis": render_pareto_guard(response.analysis, conflict),
                    "feedback": render_archive_conflict(conflict),
                    "verdict": Verdict.FAIL,
                }
            )
        self.files.note_judge(request.round_number, state.retry, response)
        return response

    def binding(self, hypothesis_id: str) -> AgentBinding:
        """Return runtime attribution for a hypothesis whose work started."""
        try:
            return self._workers[hypothesis_id].binding
        except KeyError as error:
            message = f"hypothesis {hypothesis_id!r} has no implementer session"
            raise ValueError(message) from error

    async def close(self) -> None:
        """Release every named implementer conversation once."""
        if self._closed:
            return
        self._closed = True
        for session in reversed(tuple(self._workers.values())):
            await session.close()


__all__ = ["AttemptRequest", "MultiAgentTurns", "PlanRequest"]
