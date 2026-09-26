"""Agent turns and prompt decisions for the single strategy."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from vibesys import constants
from vibesys.agent_run import issue_board
from vibesys.agent_run.evidence import _pareto_archive_conflict
from vibesys.agent_run.hypotheses import apply_strategy_updates
from vibesys.domains.base import DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.errors import (
    InvalidPlanError,
    PlanCorrectionExhaustedError,
    RoleIsolationError,
    UnsupportedProfilerError,
)
from vibesys.events import FrameworkSource
from vibesys.profilers import (
    ProfilerDefinition,
    ProfilerKind,
    profiler_definition,
    require_profiler_kind,
)
from vibesys.prompts import PROMPTS_DIR, render_template
from vibesys.render.sink import output_sink
from vibesys.schemas import (
    SingleAgentRoundResponse,
    SkillResourceSelection,
    Verdict,
    normalize_hypothesis_title,
)
from vibesys.search.hypothesis import OrchestratorPlan
from vibesys.skills import build_skill_catalog, resolve_skill_selections
from vs_agent.api import AgentSessionKey, SessionScope

if TYPE_CHECKING:
    from vibesys.agent_run.attempts import AttemptState
    from vibesys.agent_run.options import AgentOrchestrationOptions
    from vibesys.agent_run.state import AgentRunState
    from vibesys.loops.single.session import AttemptRequest, PlanRequest
    from vibesys.orchestration.runtime import RunContext
    from vibesys.skills import ResolvedSkillSelection


def _fallback_plan() -> OrchestratorPlan:
    """Fail closed with the existing minimal health check plan."""
    return OrchestratorPlan.model_validate(
        {
            "task": "Re-check minimal server boots and /health returns 200.",
            "pass_criteria": "/health returns 200.",
            "reasoning": "fallback: orchestrator produced no structured response",
        }
    )


class SingleAgentTurns:
    """One designer and one combined implementer, reviewer, and profiler."""

    def __init__(self, ctx: RunContext, options: AgentOrchestrationOptions) -> None:
        """Bind the run's public capabilities and strategy options."""
        self.ctx = ctx
        self.options = options
        self.workspace = ctx.workspaces.root
        self.domain = resolve_domain(ctx.request.input_bundle.domain)
        self.modality = options.modality
        if self.modality is None and self.domain.name is constants.DomainName.LLM_SERVING:
            self.modality = "text_generation"
        self.objective = ctx.request.objective or ctx.request.input_bundle.objective
        self.roadmap_path, self.progress_path = issue_board.resolve_paths(
            self.workspace.path, options.memory_layout
        )
        self.progress_location = issue_board.display_path(self.progress_path, self.workspace.path)
        self.roadmap_location = issue_board.display_path(self.roadmap_path, self.workspace.path)
        self.pareto_location = issue_board.display_path(
            issue_board.pareto_archive_path(self.progress_path), self.workspace.path
        )
        self.template_dir = PROMPTS_DIR / "loops" / "single"

    async def open(self) -> None:
        """Start the two roles this strategy can invoke."""
        self.designer = await self.ctx.agents.spawn(
            self.ctx.agents.default_definition("orchestrator")
        )
        try:
            self.worker = await self.ctx.agents.spawn(
                self.ctx.agents.default_definition("implementer")
            )
        except BaseException:
            await self.designer.close()
            raise

    async def close(self) -> None:
        """Close agent clients before the run host closes workspaces."""
        if hasattr(self, "worker"):
            await self.worker.close()
        if hasattr(self, "designer"):
            await self.designer.close()

    def _domain_context(self) -> dict[str, object]:
        view = self.ctx.environment.view
        return {
            "modality": self.modality,
            "interface": self.options.interface,
            "reference_path": self.ctx.environment.reference_path,
            "benchmark_command": view.paths.benchmark_command,
            "accuracy_command": view.paths.accuracy_command,
            "runtime_notes": view.prompt_notes,
            "profile_execution": view.profile_execution,
            "workspace_sources": self.ctx.environment.workspace_sources,
        }

    def _plan_prompt(self, request: PlanRequest) -> str:
        view = self.ctx.environment.view
        domain_orchestrator = render_domain_section(
            self.domain, DomainRole.ORCHESTRATOR, **self._domain_context()
        )
        return render_template(
            "orchestrator_plan_prompt.j2",
            template_dir=self.template_dir,
            objective=self.objective,
            objective_location=view.paths.objective,
            profiler_summary=request.profiler_summary,
            regression_info=request.carry.regression_info,
            exhaustion_info=request.carry.exhaustion_info,
            progress_location=self.progress_location,
            roadmap_location=self.roadmap_location,
            pareto_archive_location=self.pareto_location,
            plateau_warning=request.plateau_warning,
            domain_orchestrator=domain_orchestrator,
            runtime_notes=view.prompt_notes,
            profile_execution=view.profile_execution,
            framework_benchmark_enabled=(
                self.ctx.request.input_bundle.benchmark_result is not None
                or self.ctx.request.input_bundle.benchmark_result_protocol is not None
            ),
            official_eval_every=self.options.official_eval_every,
            provisional_candidates=request.provisional_candidates,
            official_eval_cadence_due=(
                request.provisional_candidates + 1 >= self.options.official_eval_every
            ),
            **request.profile_guidance.plan_prompt_context(),
        )

    def _validate_plan(self, plan: OrchestratorPlan, state: AgentRunState) -> None:
        updates = [item.hypothesis_id for item in plan.hypothesis_updates]
        if len(updates) != len(set(updates)):
            raise InvalidPlanError.duplicate_updates()
        if plan.hypothesis_id in updates:
            raise InvalidPlanError.self_reference()
        if state.by_id(plan.hypothesis_id) is not None:
            raise InvalidPlanError.reused_id(plan.hypothesis_id)
        apply_strategy_updates(state.clone(), plan.hypothesis_updates)

    async def _designer_turn(self, prompt: str, message: str, label: str) -> OrchestratorPlan:
        allowed = (
            f"{self.roadmap_location.rstrip('/')}/index.md"
            if self.roadmap_location.endswith("/")
            else self.roadmap_location
        )
        revision = await self.workspace.snapshot(f"{label}-input")
        try:
            return await self.designer.turn_structured(
                message,
                system_prompt=prompt,
                response_cls=OrchestratorPlan,
                fallback_factory=_fallback_plan,
                label=label,
                reuse_session=False,
            )
        finally:
            changed = await self.workspace.pending_changes()
            unauthorized = [
                path for path in changed if path != allowed and not path.startswith(f"{allowed}/")
            ]
            if unauthorized:
                await self.workspace.restore(revision, clean=True, preserve_paths=(allowed,))
                remaining = [
                    path
                    for path in await self.workspace.pending_changes()
                    if path != allowed and not path.startswith(f"{allowed}/")
                ]
                if remaining:
                    raise RoleIsolationError(remaining)
                self.ctx.log(
                    f"[role-isolation] reverted {len(unauthorized)} workspace change(s) "
                    f"attempted by orchestrator: {', '.join(unauthorized[:8])}"
                )

    async def plan(self, request: PlanRequest) -> OrchestratorPlan:
        """Ask for a new plan, with one correction for invalid lifecycle edits."""
        prompt = self._plan_prompt(request)
        feedback: str | None = None
        for attempt in range(2):
            label = (
                f"round-{request.round_number}" + (f"-retry-{attempt}" if attempt else "") + "-plan"
            )
            plan = await self._designer_turn(
                prompt, feedback or "Produce this round's plan. Return only the JSON object.", label
            )
            plan.hypothesis_id = (
                plan.hypothesis_id.strip() or f"hypothesis-{request.round_number:04d}"
            )
            plan.title = normalize_hypothesis_title(plan.title)
            try:
                self._validate_plan(plan, request.state)
            except InvalidPlanError as error:
                if attempt:
                    raise
                self.ctx.log(f"[orchestrator] plan rejected ({error}); reprompting once")
                rejected = ", ".join(
                    sorted({update.hypothesis_id for update in plan.hypothesis_updates})
                )
                feedback = (
                    f"Your previous plan was rejected: {error}. "
                    f"It proposed hypothesis_id {plan.hypothesis_id!r} and named "
                    f"{rejected or '(no)'} in hypothesis_updates. "
                    "A hypothesis_id names one investigation permanently: never reuse "
                    "an identifier used earlier in this run, and choose one that has "
                    "not appeared before. hypothesis_updates may name each prior "
                    "hypothesis at most once, and never the new one. "
                    "Produce a corrected plan for this round. Return only the JSON object."
                )
                continue
            plan.recommended_skills, _ = self._skills(plan.recommended_skills)
            issue_board.write_plan_artifact(self.progress_path, request.round_number, plan)
            issue_board.append_orchestrator_plan(self.progress_path, request.round_number, plan)
            return plan
        raise PlanCorrectionExhaustedError

    def _skills(
        self, selections: list[SkillResourceSelection]
    ) -> tuple[list[SkillResourceSelection], list[ResolvedSkillSelection]]:
        if not selections:
            return [], []
        sources = self.ctx.environment.skill_source_paths
        if not sources:
            output_sink().framework_warning(
                "ignored skill recommendations because no skills are installed",
                source=FrameworkSource.LOOP,
                source_label="skills",
            )
            return [], []
        try:
            resolved, diagnostics = resolve_skill_selections(
                selections, build_skill_catalog(sources)
            )
        except (OSError, ValueError) as error:
            output_sink().framework_warning(
                "ignored skill recommendations because the catalog is invalid",
                detail=f"{type(error).__name__}: {error}",
                source=FrameworkSource.LOOP,
                source_label="skills",
            )
            return [], []
        for diagnostic in diagnostics:
            output_sink().framework_warning(
                diagnostic, source=FrameworkSource.LOOP, source_label="skills"
            )
        return [
            SkillResourceSelection(
                skill=item.skill,
                resource_paths=[
                    path.removeprefix(f"{item.skill}/") for path in item.resource_paths
                ],
                purpose=item.purpose,
            )
            for item in resolved
        ], resolved

    def _profiler(self) -> ProfilerDefinition | None:
        kind = require_profiler_kind(self.ctx.environment.profiler_kind)
        if kind is ProfilerKind.NONE:
            return None
        definition = profiler_definition(kind)
        if definition.requires_domain_torch_support and not self.domain.supports_torch_profiler:
            raise UnsupportedProfilerError
        return definition

    def _combined_prompt(
        self, request: AttemptRequest, state: AttemptState, skills: list[ResolvedSkillSelection]
    ) -> str:
        view = self.ctx.environment.view
        plan = request.plan
        profiler = self._profiler()
        plan_artifact = issue_board.write_plan_artifact(
            self.progress_path, request.round_number, plan
        )
        domain_context = self._domain_context()
        return render_template(
            "single_agent_round_prompt.j2",
            template_dir=self.template_dir,
            reference_path=self.ctx.environment.reference_path,
            modality=self.modality,
            interface=self.options.interface,
            domain_single_agent=render_domain_section(
                self.domain, DomainRole.SINGLE_AGENT, **domain_context
            ),
            domain_profiler=render_domain_section(
                self.domain, DomainRole.PROFILER, **domain_context
            ),
            task=plan.task,
            pass_criteria=plan.pass_criteria,
            hypothesis_id=plan.hypothesis_id,
            hypothesis=plan.hypothesis,
            activation_evidence=plan.activation_evidence,
            falsification_criteria=plan.falsification_criteria,
            expected_effect=plan.expected_effect,
            minimum_acceptance_criteria=plan.minimum_acceptance_criteria,
            invariants=plan.invariants,
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_location,
            validation_location=issue_board.display_path(
                issue_board.validation_artifact_root(self.progress_path), self.workspace.path
            ),
            retry=state.retry,
            feedback=state.feedback,
            objective=self.objective,
            objective_location=view.paths.objective,
            plan_artifact_location=issue_board.display_path(plan_artifact, self.workspace.path),
            recommended_skills=skills,
            profile_focus=request.last_profile_focus,
            profiler_kind=self.ctx.environment.profiler_kind,
            profiler_support_name=profiler.support_name if profiler else None,
            profiler_mcp_name=profiler.mcp_name if profiler else None,
            supports_torch_profiler=self.domain.supports_torch_profiler,
            benchmark_command=view.paths.benchmark_command,
            accuracy_command=view.paths.accuracy_command,
            runtime_notes=view.prompt_notes,
            profile_execution=view.profile_execution,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            framework_benchmark_enabled=(
                self.ctx.request.input_bundle.benchmark_result is not None
                or self.ctx.request.input_bundle.benchmark_result_protocol is not None
            ),
        )

    async def combined(
        self, request: AttemptRequest, state: AttemptState
    ) -> SingleAgentRoundResponse:
        """Execute the combined role and apply the strategy's Pareto guard."""
        plan = request.plan
        plan.recommended_skills, resolved = self._skills(plan.recommended_skills)
        prompt = self._combined_prompt(request, state, resolved)
        try:
            response = await self.worker.turn_structured(
                "Carry out the orchestrator's task above end-to-end "
                "(implement, self-judge, profile) and return only the JSON object.",
                system_prompt=prompt,
                response_cls=SingleAgentRoundResponse,
                fallback_factory=lambda: SingleAgentRoundResponse(
                    summary="Single-agent produced no structured response.",
                    expected_behavior="unknown",
                    self_review="No structured response received.",
                    feedback="No structured response received.",
                    verdict=Verdict.FAIL,
                    bottlenecks="",
                    suggestions="",
                    profile_analysis="",
                ),
                label=f"round-{request.round_number}-retry-{state.retry}-single-agent",
                reuse_session=True,
                session_key=AgentSessionKey(SessionScope.HYPOTHESIS, plan.hypothesis_id),
            )
        except subprocess.TimeoutExpired as error:
            response = SingleAgentRoundResponse(
                summary="Single-agent invocation timed out.",
                expected_behavior="unknown",
                self_review=(
                    f"The framework stopped the agent after {error.timeout:g} seconds "
                    "without a structured response."
                ),
                feedback="Inspect retained evidence and return a schema-valid response on retry.",
                verdict=Verdict.FAIL,
                bottlenecks="",
                suggestions="",
                profile_analysis="",
            )
            self.ctx.log(
                f"[single-agent] attempt {state.retry} timed out after {error.timeout:g} seconds"
            )
        response.skill_context_updates, _ = self._skills(response.skill_context_updates)
        if response.skill_context_updates:
            plan.recommended_skills, _ = self._skills(
                [*plan.recommended_skills, *response.skill_context_updates]
            )
            issue_board.write_plan_artifact(self.progress_path, request.round_number, plan)
        conflict = _pareto_archive_conflict(
            candidate_disposition=response.candidate_disposition,
            candidate_metrics=dict(response.candidate_metrics),
            records=request.records,
            space=state.agent_run_state.metrics,
        )
        if response.verdict is Verdict.PASS and conflict:
            response = response.model_copy(
                update={
                    "self_review": f"{response.self_review}\n\nFramework Pareto guard: {conflict}",
                    "feedback": conflict,
                    "verdict": Verdict.FAIL,
                }
            )
        issue_board.append_single_agent_round(
            self.progress_path, request.round_number, state.retry, response
        )
        await self.workspace.snapshot(
            f"round-{request.round_number}-retry-{state.retry}-single-agent"
        )
        return response
