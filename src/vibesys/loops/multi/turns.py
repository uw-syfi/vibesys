"""Agent turns and prompt decisions for the multi strategy.

Every role turn (orchestrator pre-round decision and plan, profiler,
implementer, judge) runs through ``ctx.agents.turn`` with the roles declared
across roles/designer.py, pre_round.py, profiler.py, implementer.py, and
judge.py. This module builds each turn's typed prompt context and the
strategy-only logic that cannot live in a generic host verb: the designer's
plan-ID validation retry (state dependent, not a pure reply check) and
composing the profiler's per-round campaign-context addendum.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from vibesys import constants
from vibesys.domains.base import DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.errors import (
    InvalidPlanError,
    MissingImplementationError,
    PlanCorrectionExhaustedError,
    UnsupportedProfilerError,
)
from vibesys.events import CoreEventType, EventStatus, FrameworkSource, JudgeResultData
from vibesys.orchestration import artifacts, memory, progress_log
from vibesys.profilers import (
    ProfilerDefinition,
    ProfilerKind,
    profiler_definition,
    require_profiler_kind,
)
from vibesys.profilers import (
    mcp_spec as profiler_mcp_spec,
)
from vibesys.prompts.contexts import (
    display_path,
    domain_context,
    implementer_focus_kwargs,
    plan_focus_kwargs,
)
from vibesys.roles.common import Verdict
from vibesys.roles.designer import MULTI_ORCHESTRATOR_PLAN, PlanContext
from vibesys.roles.implementer import (
    MULTI_IMPLEMENTER,
    MULTI_IMPLEMENTER_CONTINUATION,
    ImplementerContext,
    ImplementerContinuationContext,
    ImplementerResponse,
)
from vibesys.roles.judge import MULTI_JUDGE, JudgeContext, JudgeResponse
from vibesys.roles.pre_round import MULTI_PRE_ROUND_DECISION, PreRoundContext, PreRoundDecision
from vibesys.roles.profiler import MULTI_PROFILERS, ProfilerContext, ProfilerSummary
from vibesys.runtime import ReadOnly
from vibesys.schemas import normalize_hypothesis_title
from vibesys.skills import build_skill_catalog, resolve_skill_selections

if TYPE_CHECKING:
    from vibesys.loops.agent_options import AgentOrchestrationOptions
    from vibesys.loops.multi.decisions import AttemptRequest, PlanRequest
    from vibesys.orchestration.runtime import RunContext
    from vibesys.schemas import SkillResourceSelection
    from vibesys.search.hypothesis import CarryOver, HypothesisSearch
    from vibesys.search.hypothesis.attempts import AttemptState
    from vibesys.search.hypothesis.plan import OrchestratorPlan
    from vibesys.search.hypothesis.state import HypothesisState
    from vibesys.skills import ResolvedSkillSelection

# The implementer's fallback texts are the sentinel that identifies a
# synthesized (unparseable or timed-out) reply; see roles/multi.py.
_SYNTHESIZED_IMPLEMENTER_SUMMARIES = frozenset(
    {
        "Implementer produced no structured response.",
        "Implementer invocation timed out.",
    }
)


class MultiAgentTurns:
    """Independent designer, profiler, implementer, and judge roles."""

    def __init__(
        self, ctx: RunContext, options: AgentOrchestrationOptions, search: HypothesisSearch
    ) -> None:
        """Bind the run's public capabilities, strategy options, and search policy.

        Declares this run's progress-board path once with `ctx.progress`
        (see `vibesys.orchestration.progress`): turns note pure, unwritten
        Markdown blocks (see `vibesys.orchestration.progress_log`'s
        `render_*` functions) via `ctx.progress.note`; only
        `ctx.state.commit`/`ctx.gates.run` -- the host -- write them to disk.
        """
        self.ctx = ctx
        self.options = options
        self.search = search
        self.workspace = ctx.workspaces.root
        self.domain = resolve_domain(ctx.request.input_bundle.domain)
        self.modality = options.modality
        if self.modality is None and self.domain.name is constants.DomainName.LLM_SERVING:
            self.modality = "text_generation"
        self.objective = ctx.request.objective or ctx.request.input_bundle.objective
        self.roadmap_path, self.progress_path = memory.resolve_paths(
            self.workspace.path, options.memory_layout
        )
        ctx.progress.declare(self.progress_path)
        self.progress_location = display_path(self.progress_path, self.workspace.path)
        self.roadmap_location = display_path(self.roadmap_path, self.workspace.path)
        self.pareto_location = display_path(
            memory.pareto_archive_path(self.progress_path), self.workspace.path
        )

    async def open(self) -> None:
        """Start each role this strategy can invoke."""
        self.designer = await self.ctx.agents.spawn(
            self.ctx.agents.default_definition("orchestrator")
        )
        self.worker = await self.ctx.agents.spawn(self.ctx.agents.default_definition("implementer"))
        self.judge = await self.ctx.agents.spawn(self.ctx.agents.default_definition("judge"))
        self.profiler = await self.ctx.agents.spawn(self.ctx.agents.default_definition("profiler"))

    async def close(self) -> None:
        """Close agent clients before the run host closes workspaces."""
        if hasattr(self, "profiler"):
            await self.profiler.close()
        if hasattr(self, "judge"):
            await self.judge.close()
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
            framework_benchmark_enabled=self._framework_benchmark_configured(),
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
        self.search.validate_updates(state, plan.hypothesis_updates)

    async def plan(self, request: PlanRequest) -> OrchestratorPlan:
        """Ask for a new plan, with one correction for invalid lifecycle edits.

        The correction here is state dependent (a hypothesis ID collision
        against this run's history), which ``Role.check`` cannot express (it
        only sees the reply in isolation); it stays strategy code wrapping
        ``ctx.agents.turn``, as today.
        """
        context = self._plan_context(request)
        allowed = (
            f"{self.roadmap_location.rstrip('/')}/index.md"
            if self.roadmap_location.endswith("/")
            else self.roadmap_location
        )
        role = replace(MULTI_ORCHESTRATOR_PLAN, access=ReadOnly(allow=(allowed,)))
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
            artifacts.write_plan_artifact(self.progress_path, request.round_number, plan)
            self.ctx.progress.note(
                progress_log.render_orchestrator_plan(request.round_number, plan)
            )
            return plan
        raise PlanCorrectionExhaustedError

    def _profiler(self) -> ProfilerDefinition | None:
        kind = require_profiler_kind(self.ctx.environment.profiler_kind)
        if kind is ProfilerKind.NONE:
            return None
        definition = profiler_definition(kind)
        if definition.requires_domain_torch_support and not self.domain.supports_torch_profiler:
            raise UnsupportedProfilerError
        return definition

    async def pre_round_decision(
        self, round_number: int, carry: CarryOver, *, has_history: bool
    ) -> PreRoundDecision:
        """Ask the orchestrator whether a specialist profile is useful."""
        view = self.ctx.environment.view
        context = PreRoundContext(
            objective_location=view.paths.objective,
            regression_info=carry.regression_info,
            exhaustion_info=carry.exhaustion_info,
            progress_location=self.progress_location,
            profiler_kind=self.ctx.environment.profiler_kind.value,
            profile_execution=view.profile_execution,
            has_history=has_history,
        )
        decision = cast(
            "PreRoundDecision",
            await self.ctx.agents.turn(
                MULTI_PRE_ROUND_DECISION,
                agent=self.designer,
                context=context,
                label=f"round-{round_number}-pre",
            ),
        )
        self.ctx.progress.note(progress_log.render_pre_round_decision(round_number, decision))
        return decision

    def _profiler_campaign_context(self, artifact: str) -> str:
        """The per-round addendum every profiler template renders at its tail.

        Kept as Python (not baked into the shared templates) because it
        depends on this round's bounded evidence directory.
        """
        return f"""

## Recent campaign context

The durable progress artifact is `{self.progress_location}`. Inspect the most recent
applicable round with tools to identify the current candidate, hypothesis, and
retained evaluation artifacts. Read older rounds only when the requested focus
depends on them.

For the requested profile focus, resolve artifacts explicitly referenced by
the most recent applicable round before considering older similarly named
artifacts. Do not launch a duplicate expensive evaluation when retained
current-candidate evidence already answers the focus; collect the smallest
additional profile that closes a specific evidence gap instead.

## Read-only evidence boundary

Use only capture interfaces present when this turn started. Never edit or add
candidate source, configuration, tests, locks, instrumentation, endpoints, or
entrypoints. If the requested production path is not observable, report that
capability mismatch; a later Implementer may add reviewed instrumentation.

Write bounded durable profile evidence only below
`{artifact}/`; keep large transient traces under `/tmp`.
"""

    async def profile(self, round_number: int, focus: str) -> ProfilerSummary | None:
        """Collect read-only profiler evidence before the designer plan."""
        definition = self._profiler()
        if definition is None:
            return None
        kind = require_profiler_kind(self.ctx.environment.profiler_kind)
        view = self.ctx.environment.view
        artifact = display_path(
            artifacts.profiler_artifact_root(self.progress_path, round_number),
            self.workspace.path,
        ).rstrip("/")
        context = ProfilerContext(
            profile_focus=focus,
            benchmark_command=view.paths.benchmark_command,
            modality=self.modality,
            domain_profiler=render_domain_section(
                self.domain, DomainRole.PROFILER, **self._domain_context()
            ),
            runtime_notes=view.prompt_notes,
            profile_execution=view.profile_execution,
            objective=self.objective,
            profiler_support_name=definition.support_name,
            profiler_mcp_name=definition.mcp_name,
            profiler_campaign_context=self._profiler_campaign_context(artifact),
        )
        role = replace(MULTI_PROFILERS[kind], access=ReadOnly(allow=(artifact,)))
        spec = profiler_mcp_spec(self.ctx.environment.profiler_kind)
        try:
            summary = cast(
                "ProfilerSummary",
                await self.ctx.agents.turn(
                    role,
                    agent=self.profiler,
                    context=context,
                    label=f"round-{round_number}-profiler",
                    mcp_servers=[spec] if spec is not None else None,
                ),
            )
        except Exception as error:  # noqa: BLE001  # profile evidence is optional
            self.ctx.warning(
                "profiler failed",
                detail=str(error),
                source=FrameworkSource.LOOP,
                round_label=f"round-{round_number}",
            )
            return None
        self.ctx.progress.note(progress_log.render_profiler_summary(round_number, summary))
        return summary

    def _framework_benchmark_configured(self) -> bool:
        bundle = self.ctx.request.input_bundle
        return bundle.benchmark_result is not None or bundle.benchmark_result_protocol is not None

    def _resolved_skills(
        self, selections: list[SkillResourceSelection]
    ) -> list[ResolvedSkillSelection]:
        """Resolve an already-filtered selection list for prompt display.

        ``plan.recommended_skills`` was already validated and filtered by the
        designer's own turn (``MULTI_ORCHESTRATOR_PLAN.filter_skills``); this
        only adds each skill's router path for the implementer prompt, so it
        does not re-warn on selections already accepted this run.
        """
        if not selections or not self.ctx.environment.skill_source_paths:
            return []
        try:
            resolved, _ = resolve_skill_selections(
                selections, build_skill_catalog(self.ctx.environment.skill_source_paths)
            )
        except (OSError, ValueError):
            return []
        return resolved

    def _implementer_context(
        self, request: AttemptRequest, state: AttemptState
    ) -> ImplementerContext:
        plan = request.plan
        hypothesis = request.active_hypothesis
        view = self.ctx.environment.view
        artifact = artifacts.write_plan_artifact(self.progress_path, request.round_number, plan)
        prior = tuple(
            display_path(path, self.workspace.path)
            for path in artifacts.implementer_artifact_paths(
                self.progress_path, request.round_number
            )
        )
        profile_extra = request.profile_focus.implementer_prompt_context()
        return ImplementerContext(
            reference_path=self.ctx.environment.reference_path,
            modality=self.modality,
            interface=self.options.interface,
            domain_implementer=render_domain_section(
                self.domain, DomainRole.IMPLEMENTER, **self._domain_context()
            ),
            objective_location=view.paths.objective,
            plan_artifact_location=display_path(artifact, self.workspace.path),
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_location,
            validation_location=display_path(
                artifacts.validation_artifact_root(self.progress_path), self.workspace.path
            ),
            validation_recipe_contract_location=display_path(
                artifacts.validation_recipe_schema_path(self.progress_path), self.workspace.path
            ),
            retry=state.retry,
            feedback=state.feedback,
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
            runtime_notes=view.prompt_notes,
            framework_benchmark_enabled=self._framework_benchmark_configured(),
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            recommended_skills=self._resolved_skills(plan.recommended_skills),
            prior_attempt_artifact_locations=prior,
            **implementer_focus_kwargs(profile_extra),
        )

    def _implementer_continuation_context(
        self, request: AttemptRequest, state: AttemptState
    ) -> ImplementerContinuationContext:
        plan = request.plan
        hypothesis = request.active_hypothesis
        view = self.ctx.environment.view
        artifact = artifacts.write_plan_artifact(self.progress_path, request.round_number, plan)
        prior = tuple(
            display_path(path, self.workspace.path)
            for path in artifacts.implementer_artifact_paths(
                self.progress_path, request.round_number
            )
        )
        return ImplementerContinuationContext(
            hypothesis_id=plan.hypothesis_id,
            objective_location=view.paths.objective,
            plan_artifact_location=display_path(artifact, self.workspace.path),
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_location,
            validation_location=display_path(
                artifacts.validation_artifact_root(self.progress_path), self.workspace.path
            ),
            validation_recipe_contract_location=display_path(
                artifacts.validation_recipe_schema_path(self.progress_path), self.workspace.path
            ),
            runtime_notes=view.prompt_notes,
            prior_attempt_artifact_locations=prior,
            retry=state.retry,
            continuation_step=cast("str", hypothesis.next_step),
            feedback=state.feedback,
            recommended_skills=self._resolved_skills(plan.recommended_skills),
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
        )

    async def implement(
        self, request: AttemptRequest, state: AttemptState
    ) -> tuple[ImplementerResponse, bool]:
        """Prepare the prompt, then mark and invoke one paid implementer turn."""
        plan = request.plan
        continuing = bool(request.active_hypothesis.next_step)
        role = MULTI_IMPLEMENTER_CONTINUATION if continuing else MULTI_IMPLEMENTER
        context: ImplementerContext | ImplementerContinuationContext = (
            self._implementer_continuation_context(request, state)
            if continuing
            else self._implementer_context(request, state)
        )
        label = f"round-{request.round_number}-retry-{state.retry}-implementer"

        async def mark_paid() -> None:
            artifacts.write_implementer_start_marker(
                self.progress_path, request.round_number, state.retry
            )

        response = cast(
            "ImplementerResponse",
            await self.ctx.agents.turn(
                role,
                agent=self.worker,
                context=context,
                session_key=plan.hypothesis_id,
                label=label,
                before_paid=mark_paid,
            ),
        )
        synthesized = response.summary in _SYNTHESIZED_IMPLEMENTER_SUMMARIES
        if response.skill_context_updates:
            plan.recommended_skills = [*plan.recommended_skills, *response.skill_context_updates]
            artifacts.write_plan_artifact(self.progress_path, request.round_number, plan)
        artifacts.write_implementer_artifact(
            self.progress_path, request.round_number, state.retry, response
        )
        self.ctx.progress.note(
            progress_log.render_implementer(request.round_number, state.retry, response)
        )
        return response, synthesized

    async def review(
        self, request: AttemptRequest, state: AttemptState, conflict: str | None
    ) -> JudgeResponse:
        """Ask the independent judge to audit the parsed implementation."""
        implementation = state.implementation
        if implementation is None:
            raise MissingImplementationError
        plan = request.plan
        hypothesis = request.active_hypothesis
        view = self.ctx.environment.view

        plan_artifact = artifacts.write_plan_artifact(
            self.progress_path, request.round_number, plan
        )
        evidence = artifacts.write_implementer_artifact(
            self.progress_path, request.round_number, state.retry, implementation
        )
        domain_ctx = self._domain_context()
        domain_ctx["accuracy_command"] = None
        domain_ctx["benchmark_command"] = None
        context = JudgeContext(
            domain_judge=render_domain_section(self.domain, DomainRole.JUDGE, **domain_ctx),
            framework_benchmark_enabled=self._framework_benchmark_configured(),
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
            gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            implementer_artifact_location=display_path(evidence, self.workspace.path),
            interface=self.options.interface,
            modality=self.modality,
            objective_location=view.paths.objective,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            pareto_archive_conflict=conflict,
            pareto_archive_location=self.pareto_location,
            plan_artifact_location=display_path(plan_artifact, self.workspace.path),
            progress_location=self.progress_location,
            retry=state.retry,
            runtime_notes=view.prompt_notes,
            validation_location=display_path(
                artifacts.validation_artifact_root(self.progress_path), self.workspace.path
            ),
            validation_recipe_contract_location=display_path(
                artifacts.validation_recipe_schema_path(self.progress_path), self.workspace.path
            ),
        )
        response = cast(
            "JudgeResponse",
            await self.ctx.agents.turn(
                MULTI_JUDGE,
                agent=self.judge,
                context=context,
                label=f"round-{request.round_number}-retry-{state.retry}-judge",
            ),
        )
        if response.verdict is Verdict.PASS and conflict:
            response = response.model_copy(
                update={
                    "analysis": f"{response.analysis}\n\nFramework Pareto guard: {conflict}",
                    "feedback": conflict,
                    "verdict": Verdict.FAIL,
                }
            )
        self.ctx.events.emit(
            CoreEventType.JUDGE_RESULT,
            status=EventStatus.COMPLETED
            if response.verdict is Verdict.PASS
            else EventStatus.FAILED,
            round_label=f"round-{request.round_number}-retry-{state.retry}",
            agent_kind="judge",
            data=JudgeResultData(
                verdict=response.verdict.value, feedback=response.feedback, attempt=state.retry
            ),
        )
        self.ctx.progress.note(
            progress_log.render_judge(request.round_number, state.retry, response)
        )
        return response
