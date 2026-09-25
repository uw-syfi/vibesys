"""Agent turns and prompt decisions for the single strategy."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from vibesys import constants
from vibesys.agent_run import issue_board
from vibesys.domains.base import DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.errors import (
    InvalidPlanError,
    PlanCorrectionExhaustedError,
    UnsupportedProfilerError,
)
from vibesys.events import FrameworkSource
from vibesys.orchestration import progress_log
from vibesys.profilers import (
    ProfilerDefinition,
    ProfilerKind,
    profiler_definition,
    require_profiler_kind,
)
from vibesys.prompts import PROMPTS_DIR
from vibesys.prompts.contexts import domain_context, plan_focus_kwargs
from vibesys.roles.common import Verdict
from vibesys.roles.designer import SINGLE_ORCHESTRATOR_PLAN, PlanContext
from vibesys.roles.single_agent import (
    SINGLE_COMBINED,
    SingleAgentRoundContext,
    SingleAgentRoundResponse,
)
from vibesys.runtime import ReadOnly, Role
from vibesys.schemas import SkillResourceSelection, normalize_hypothesis_title
from vibesys.search.hypothesis.transitions import apply_strategy_updates, pareto_archive_conflict
from vibesys.skills import build_skill_catalog, resolve_skill_selections

if TYPE_CHECKING:
    from vibesys.loops.agent_options import AgentOrchestrationOptions
    from vibesys.loops.single.session import AttemptRequest, PlanRequest
    from vibesys.orchestration.runtime import RunContext
    from vibesys.search.hypothesis.attempts import AttemptState
    from vibesys.search.hypothesis.plan import OrchestratorPlan
    from vibesys.search.hypothesis.state import HypothesisState
    from vibesys.skills import ResolvedSkillSelection


class SingleAgentTurns:
    """One designer and one combined implementer, reviewer, and profiler."""

    def __init__(
        self, ctx: RunContext, options: AgentOrchestrationOptions, board: list[str]
    ) -> None:
        """Bind the run's public capabilities, strategy options, and board buffer.

        *board* is the owning session's pending framework-log buffer: turns
        append pure, unwritten Markdown blocks to it (see
        ``vibesys.orchestration.progress_log``'s ``render_*`` functions); the
        session hands the buffer to ``ctx.state.commit`` -- the host -- which
        is the only place that writes it to disk.
        """
        self.ctx = ctx
        self.options = options
        self._board = board
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
        return domain_context(
            modality=self.modality,
            interface=self.options.interface,
            reference_path=self.ctx.environment.reference_path,
            benchmark_command=view.paths.benchmark_command,
            accuracy_command=view.paths.accuracy_command,
            runtime_notes=view.prompt_notes,
            profile_execution=view.profile_execution,
            workspace_sources=self.ctx.environment.workspace_sources,
        ).model_dump()

    def _plan_context(self, request: PlanRequest) -> PlanContext:
        view = self.ctx.environment.view
        domain_orchestrator = render_domain_section(
            self.domain, DomainRole.ORCHESTRATOR, **self._domain_context()
        )
        return PlanContext(
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
            framework_benchmark_enabled=(
                self.ctx.request.input_bundle.benchmark_result is not None
                or self.ctx.request.input_bundle.benchmark_result_protocol is not None
            ),
            official_eval_every=self.options.official_eval_every,
            provisional_candidates=request.provisional_candidates,
            official_eval_cadence_due=(
                request.provisional_candidates + 1 >= self.options.official_eval_every
            ),
            **plan_focus_kwargs(request.profile_guidance.plan_prompt_context()),
        )

    def _validate_plan(self, plan: OrchestratorPlan, state: HypothesisState) -> None:
        updates = [item.hypothesis_id for item in plan.hypothesis_updates]
        if len(updates) != len(set(updates)):
            raise InvalidPlanError.duplicate_updates()
        if plan.hypothesis_id in updates:
            raise InvalidPlanError.self_reference()
        if state.by_id(plan.hypothesis_id) is not None:
            raise InvalidPlanError.reused_id(plan.hypothesis_id)
        apply_strategy_updates(state.clone(), plan.hypothesis_updates)

    def _designer_role(self) -> Role:
        """The orchestrator role, allow-listing this run's roadmap index.

        A read-only role's allow-list is per-call (it depends on this run's
        ``memory_layout``), so the role is built fresh here rather than
        declared static in ``vibesys.roles.designer`` and ``vibesys.roles.single_agent``.
        """
        allowed = (
            f"{self.roadmap_location.rstrip('/')}/index.md"
            if self.roadmap_location.endswith("/")
            else self.roadmap_location
        )
        return replace(SINGLE_ORCHESTRATOR_PLAN, access=ReadOnly(allow=(allowed,)))

    async def plan(self, request: PlanRequest) -> OrchestratorPlan:
        """Ask for a new plan, with one correction for invalid lifecycle edits.

        The retry here is state-dependent (a hypothesis ID collision against
        this run's live state), which ``Role.check`` cannot express (it sees
        only the parsed reply); so this wraps two plain ``ctx.agents.turn``
        calls rather than using the role's own correction loop.
        """
        role = self._designer_role()
        context = self._plan_context(request)
        feedback: str | None = None
        for attempt in range(2):
            label = (
                f"round-{request.round_number}" + (f"-retry-{attempt}" if attempt else "") + "-plan"
            )
            plan = cast(
                "OrchestratorPlan",
                await self.ctx.agents.turn(
                    role,
                    agent=self.designer,
                    context=context,
                    message=feedback,
                    label=label,
                ),
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
            self._board.append(progress_log.render_orchestrator_plan(request.round_number, plan))
            return plan
        raise PlanCorrectionExhaustedError

    def _skills(
        self, selections: list[SkillResourceSelection]
    ) -> tuple[list[SkillResourceSelection], list[ResolvedSkillSelection]]:
        if not selections:
            return [], []
        sources = self.ctx.environment.skill_source_paths
        if not sources:
            self.ctx.warning(
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
            self.ctx.warning(
                "ignored skill recommendations because the catalog is invalid",
                detail=f"{type(error).__name__}: {error}",
                source=FrameworkSource.LOOP,
                source_label="skills",
            )
            return [], []
        for diagnostic in diagnostics:
            self.ctx.warning(diagnostic, source=FrameworkSource.LOOP, source_label="skills")
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

    def _combined_context(
        self, request: AttemptRequest, state: AttemptState
    ) -> SingleAgentRoundContext:
        view = self.ctx.environment.view
        plan = request.plan
        profiler = self._profiler()
        plan_artifact = issue_board.write_plan_artifact(
            self.progress_path, request.round_number, plan
        )
        domain_ctx = self._domain_context()
        return SingleAgentRoundContext(
            domain_single_agent=render_domain_section(
                self.domain, DomainRole.SINGLE_AGENT, **domain_ctx
            ),
            domain_profiler=render_domain_section(self.domain, DomainRole.PROFILER, **domain_ctx),
            interface=self.options.interface,
            objective_location=view.paths.objective,
            plan_artifact_location=issue_board.display_path(plan_artifact, self.workspace.path),
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_location,
            validation_location=issue_board.display_path(
                issue_board.validation_artifact_root(self.progress_path), self.workspace.path
            ),
            feedback=state.feedback,
            profiler_kind=self.ctx.environment.profiler_kind.value,
            profiler_support_name=profiler.support_name if profiler else None,
            benchmark_command=view.paths.benchmark_command,
            accuracy_command=view.paths.accuracy_command,
            runtime_notes=view.prompt_notes,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
        )

    async def combined(
        self, request: AttemptRequest, state: AttemptState
    ) -> SingleAgentRoundResponse:
        """Execute the combined role and apply the strategy's Pareto guard."""
        plan = request.plan
        plan.recommended_skills, _ = self._skills(plan.recommended_skills)
        context = self._combined_context(request, state)

        async def mark_paid() -> None:
            issue_board.write_implementer_start_marker(
                self.progress_path, request.round_number, state.retry
            )

        response = cast(
            "SingleAgentRoundResponse",
            await self.ctx.agents.turn(
                SINGLE_COMBINED,
                agent=self.worker,
                context=context,
                session_key=plan.hypothesis_id,
                label=f"round-{request.round_number}-retry-{state.retry}-single-agent",
                before_paid=mark_paid,
            ),
        )
        response.skill_context_updates, _ = self._skills(response.skill_context_updates)
        if response.skill_context_updates:
            plan.recommended_skills, _ = self._skills(
                [*plan.recommended_skills, *response.skill_context_updates]
            )
            issue_board.write_plan_artifact(self.progress_path, request.round_number, plan)
        conflict = pareto_archive_conflict(
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
        self._board.append(
            progress_log.render_single_agent_round(request.round_number, state.retry, response)
        )
        return response
