"""Agent turns and prompt decisions for the multi strategy."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, TypeVar, cast

from pydantic import BaseModel

from vibesys import constants
from vibesys.agent_run import issue_board
from vibesys.agent_run.hypotheses import apply_strategy_updates
from vibesys.domains.base import DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.errors import (
    InvalidPlanError,
    MissingImplementationError,
    PlanCorrectionExhaustedError,
    RoleIsolationError,
    UnsupportedProfilerError,
)
from vibesys.events import CoreEventType, EventStatus, FrameworkSource, JudgeResultData
from vibesys.profilers import (
    ProfilerDefinition,
    ProfilerKind,
    profiler_definition,
    require_profiler_kind,
)
from vibesys.profilers import (
    mcp_spec as profiler_mcp_spec,
)
from vibesys.prompts import PROMPTS_DIR, render_template
from vibesys.render.sink import output_sink
from vibesys.schemas import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
    ProfilerSummary,
    SkillResourceSelection,
    Verdict,
    normalize_hypothesis_title,
)
from vibesys.search.hypothesis import OrchestratorPlan
from vibesys.skills import build_skill_catalog, resolve_skill_selections
from vs_agent.api import AgentSessionKey, SessionScope

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.agent_run.attempts import AttemptState
    from vibesys.agent_run.evidence import CarryOver
    from vibesys.agent_run.options import AgentOrchestrationOptions
    from vibesys.agent_run.state import AgentRunState
    from vibesys.loops.multi.decisions import AttemptRequest, PlanRequest
    from vibesys.orchestration.runtime import RunContext
    from vibesys.runtime import AgentHandle
    from vibesys.skills import ResolvedSkillSelection
    from vs_agent.api import MCPServerSpec

T = TypeVar("T", bound=BaseModel)


def _unauthorized_paths(changes: list[str], allowed: tuple[str, ...]) -> list[str]:
    """Exclude the paths a read-only role may update."""
    return [
        path
        for path in changes
        if not any(
            path == item.rstrip("/") or path.startswith(f"{item.rstrip('/')}/") for item in allowed
        )
    ]


def _fallback_plan() -> OrchestratorPlan:
    """Fail closed with the existing minimal health check plan."""
    return OrchestratorPlan.model_validate(
        {
            "task": "Re-check minimal server boots and /health returns 200.",
            "pass_criteria": "/health returns 200.",
            "reasoning": "fallback: orchestrator produced no structured response",
        }
    )


class MultiAgentTurns:
    """Independent designer, profiler, implementer, and judge roles."""

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
        self.template_dir = PROMPTS_DIR / "loops" / "multi"

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
            unauthorized = _unauthorized_paths(await self.workspace.pending_changes(), (allowed,))
            if unauthorized:
                await self.workspace.restore(revision, clean=True, preserve_paths=(allowed,))
                remaining = _unauthorized_paths(await self.workspace.pending_changes(), (allowed,))
                if remaining:
                    raise RoleIsolationError(remaining, role="orchestrator")
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

    async def _read_only(  # noqa: PLR0913  # lint-waiver: LW-020020 [PLR0913]; the read-only agent turn forwards independent prompt, response, and session options.
        self,
        agent: AgentHandle,
        *,
        message: str,
        prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        label: str,
        allowed: tuple[str, ...] = (),
        mcp_servers: list[MCPServerSpec] | None = None,
    ) -> T:
        revision = await self.workspace.snapshot(f"{label}-input")
        try:
            return await agent.turn_structured(
                message,
                system_prompt=prompt,
                response_cls=response_cls,
                fallback_factory=fallback_factory,
                label=label,
                reuse_session=False,
                mcp_servers=mcp_servers,
            )
        finally:
            unauthorized = _unauthorized_paths(await self.workspace.pending_changes(), allowed)
            if unauthorized:
                await self.workspace.restore(revision, clean=True, preserve_paths=allowed)
                remaining = _unauthorized_paths(await self.workspace.pending_changes(), allowed)
                if remaining:
                    raise RoleIsolationError(remaining, role=label)
                self.ctx.log(
                    f"[role-isolation] reverted {len(unauthorized)} workspace change(s): {', '.join(unauthorized[:8])}"
                )

    async def pre_round_decision(
        self, round_number: int, carry: CarryOver, *, has_history: bool
    ) -> PreRoundDecision:
        """Ask the orchestrator whether a specialist profile is useful."""
        view = self.ctx.environment.view
        prompt = render_template(
            "orchestrator_pre_round_prompt.j2",
            template_dir=self.template_dir,
            objective=self.objective,
            objective_location=view.paths.objective,
            regression_info=carry.regression_info,
            exhaustion_info=carry.exhaustion_info,
            progress_location=self.progress_location,
            profiler_kind=self.ctx.environment.profiler_kind.value,
            profile_execution=view.profile_execution,
            has_history=has_history,
        )
        decision = await self._read_only(
            self.designer,
            message="Decide whether a profiling pass is needed before planning this round. Return only the JSON object.",
            prompt=prompt,
            response_cls=PreRoundDecision,
            fallback_factory=lambda: PreRoundDecision(
                need_profile=False, profile_focus="", reasoning="fallback: default to skip"
            ),
            label=f"round-{round_number}-pre",
        )
        issue_board.append_pre_round_decision(self.progress_path, round_number, decision)
        return decision

    async def profile(self, round_number: int, focus: str) -> ProfilerSummary | None:
        """Collect read-only profiler evidence before the designer plan."""
        definition = self._profiler()
        if definition is None:
            return None
        view = self.ctx.environment.view
        prompt = render_template(
            definition.prompt_template,
            template_dir=self.template_dir,
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
        )
        artifact = issue_board.display_path(
            issue_board.profiler_artifact_root(self.progress_path, round_number),
            self.workspace.path,
        ).rstrip("/")
        prompt += f"""

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
        spec = profiler_mcp_spec(self.ctx.environment.profiler_kind)
        try:
            summary = await self._read_only(
                self.profiler,
                message="Profile the server and return exactly one JSON object matching the schema above.",
                prompt=prompt,
                response_cls=ProfilerSummary,
                fallback_factory=lambda: ProfilerSummary(
                    analysis="Profiler produced no structured response.",
                    bottlenecks="n/a",
                    suggestions="Re-run profiling on the next round.",
                ),
                label=f"round-{round_number}-profiler",
                allowed=(artifact,),
                mcp_servers=[spec] if spec is not None else None,
            )
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-020021 [BLE001]; profile evidence is optional, so a profiler failure is reported and the round proceeds without it.
            output_sink().framework_warning(
                "profiler failed",
                detail=str(error),
                source=FrameworkSource.LOOP,
                round_label=f"round-{round_number}",
            )
            return None
        issue_board.append_profiler_summary(self.progress_path, round_number, summary)
        await self.workspace.snapshot(f"round-{round_number}-profiler")
        return summary

    def _implementer_prompt(
        self, request: AttemptRequest, state: AttemptState, skills: list
    ) -> str:
        plan = request.plan
        hypothesis = request.active_hypothesis
        view = self.ctx.environment.view
        artifact = issue_board.write_plan_artifact(self.progress_path, request.round_number, plan)
        prior = tuple(
            issue_board.display_path(path, self.workspace.path)
            for path in issue_board.implementer_artifact_paths(
                self.progress_path, request.round_number
            )
        )

        def location(path: Path) -> str:
            return issue_board.display_path(path, self.workspace.path)

        return render_template(
            "implementer_continuation_prompt.j2"
            if hypothesis.next_step
            else "implementer_prompt.j2",
            template_dir=self.template_dir,
            reference_path=self.ctx.environment.reference_path,
            modality=self.modality,
            interface=self.options.interface,
            domain_implementer=render_domain_section(
                self.domain, DomainRole.IMPLEMENTER, **self._domain_context()
            ),
            task=plan.task,
            pass_criteria=plan.pass_criteria,
            objective=self.objective,
            objective_location=view.paths.objective,
            plan_artifact_location=location(artifact),
            hypothesis_id=plan.hypothesis_id,
            hypothesis=plan.hypothesis,
            activation_evidence=plan.activation_evidence,
            falsification_criteria=plan.falsification_criteria,
            expected_effect=plan.expected_effect,
            minimum_acceptance_criteria=plan.minimum_acceptance_criteria,
            invariants=plan.invariants,
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_location,
            validation_location=location(issue_board.validation_artifact_root(self.progress_path)),
            validation_recipe_contract_location=location(
                issue_board.validation_recipe_schema_path(self.progress_path)
            ),
            retry=state.retry,
            feedback=state.feedback,
            continuation_step=hypothesis.next_step,
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
            runtime_notes=view.prompt_notes,
            profile_execution=view.profile_execution,
            framework_benchmark_enabled=self._framework_benchmark_configured(),
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            recommended_skills=skills,
            prior_attempt_artifact_locations=prior,
            **request.engine.controller.guidance.implementer_prompt_context(),
        )

    def _framework_benchmark_configured(self) -> bool:
        bundle = self.ctx.request.input_bundle
        return bundle.benchmark_result is not None or bundle.benchmark_result_protocol is not None

    async def implement(
        self, request: AttemptRequest, state: AttemptState
    ) -> tuple[ImplementerResponse, bool]:
        """Prepare the prompt, then mark and invoke one paid implementer turn."""
        plan = request.plan
        plan.recommended_skills, skills = self._skills(plan.recommended_skills)
        prompt = self._implementer_prompt(request, state, skills)
        synthesized = False

        def fallback() -> ImplementerResponse:
            nonlocal synthesized
            synthesized = True
            return ImplementerResponse(
                summary="Implementer produced no structured response.",
                expected_behavior="unknown",
                hypothesis_outcome="inconclusive",
                evidence="The implementer output could not be parsed.",
                next_step="Recover retained evidence and return a schema-valid response before review.",
            )

        message = (
            "Execute the required continuation step and return only the JSON object."
            if request.active_hypothesis.next_step
            else "Work persistently on the active hypothesis and return only the JSON object."
        )
        label = f"round-{request.round_number}-retry-{state.retry}-implementer"
        session_key = AgentSessionKey(SessionScope.HYPOTHESIS, plan.hypothesis_id)
        issue_board.write_implementer_start_marker(
            self.progress_path, request.round_number, state.retry
        )
        await self.workspace.snapshot(
            f"round-{request.round_number}-retry-{state.retry}-paid-marker"
        )
        try:
            response = await self.worker.turn_structured(
                message,
                system_prompt=prompt,
                response_cls=ImplementerResponse,
                fallback_factory=fallback,
                label=label,
                reuse_session=True,
                session_key=session_key,
            )
        except subprocess.TimeoutExpired as error:
            synthesized = True
            response = ImplementerResponse(
                summary="Implementer invocation timed out.",
                expected_behavior="unknown",
                hypothesis_outcome="inconclusive",
                evidence=f"The framework stopped the implementer after {error.timeout:g} seconds without a structured response.",
                next_step="Inspect retained evidence and return a schema-valid response on retry.",
            )
            self.ctx.log(
                f"[implementer] attempt {state.retry} timed out after {error.timeout:g} seconds"
            )
        response.skill_context_updates, _ = self._skills(response.skill_context_updates)
        if response.skill_context_updates:
            plan.recommended_skills, _ = self._skills(
                [*plan.recommended_skills, *response.skill_context_updates]
            )
            issue_board.write_plan_artifact(self.progress_path, request.round_number, plan)
        issue_board.write_implementer_artifact(
            self.progress_path, request.round_number, state.retry, response
        )
        issue_board.append_implementer(
            self.progress_path, request.round_number, state.retry, response
        )
        await self.workspace.snapshot(
            f"round-{request.round_number}-retry-{state.retry}-implementer"
        )
        return response, synthesized

    async def review(
        self, request: AttemptRequest, state: AttemptState, conflict: str | None
    ) -> JudgeResponse:
        """Ask the independent judge to audit the parsed implementation."""
        if state.implementation is None:
            raise MissingImplementationError
        # TODO(stack PR 07): drop this cast once vibesys.roles.implementer owns  # noqa: FIX002, TD003  # LW-040036 [FIX002, TD003]; the placeholder marks work owned by a later change and has no issue yet.
        # ImplementerResponse and AttemptState.implementation is typed against
        # it directly; at BASE it is vibesys.schemas.ImplementerResponse, which
        # structurally (but not nominally) satisfies search's ImplementerReply.
        implementation = cast("ImplementerResponse", state.implementation)
        plan = request.plan
        hypothesis = request.active_hypothesis
        view = self.ctx.environment.view

        def location(path: Path) -> str:
            return issue_board.display_path(path, self.workspace.path)

        plan_artifact = issue_board.write_plan_artifact(
            self.progress_path, request.round_number, plan
        )
        evidence = issue_board.write_implementer_artifact(
            self.progress_path, request.round_number, state.retry, implementation
        )
        domain_context = self._domain_context()
        domain_context["accuracy_command"] = None
        domain_context["benchmark_command"] = None
        prompt = render_template(
            "judge_prompt.j2",
            template_dir=self.template_dir,
            accuracy_command=view.paths.accuracy_command,
            benchmark_command=view.paths.benchmark_command,
            pass_criteria=plan.pass_criteria,
            modality=self.modality,
            interface=self.options.interface,
            domain_judge=render_domain_section(self.domain, DomainRole.JUDGE, **domain_context),
            retry=state.retry,
            runtime_notes=view.prompt_notes,
            profile_execution=view.profile_execution,
            objective=self.objective,
            objective_location=view.paths.objective,
            plan_artifact_location=location(plan_artifact),
            implementer_artifact_location=location(evidence),
            hypothesis_id=plan.hypothesis_id,
            hypothesis=plan.hypothesis,
            activation_evidence=plan.activation_evidence,
            falsification_criteria=plan.falsification_criteria,
            expected_effect=plan.expected_effect,
            minimum_acceptance_criteria=plan.minimum_acceptance_criteria,
            invariants=plan.invariants,
            implementer_outcome=implementation.hypothesis_outcome.value,
            implementer_evidence=implementation.evidence,
            implementer_perf_metric=implementation.perf_metric,
            implementer_perf_unit=implementation.perf_unit,
            implementer_metrics=implementation.metrics,
            implementer_evaluation_artifact=implementation.evaluation_artifact,
            candidate_disposition=implementation.candidate_disposition.value,
            candidate_metrics=implementation.candidate_metrics,
            candidate_evaluation_artifact=implementation.candidate_evaluation_artifact,
            candidate_operating_point=implementation.candidate_operating_point,
            candidate_retention_reason=implementation.candidate_retention_reason,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
            gate_approved_metrics=hypothesis.gate_approved_metrics,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_location,
            validation_location=location(issue_board.validation_artifact_root(self.progress_path)),
            validation_recipe_contract_location=location(
                issue_board.validation_recipe_schema_path(self.progress_path)
            ),
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            framework_benchmark_enabled=self._framework_benchmark_configured(),
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            pareto_archive_conflict=conflict,
        )
        response = await self._read_only(
            self.judge,
            message="Review the implementation per the criteria above. Return only the JSON verdict.",
            prompt=prompt,
            response_cls=JudgeResponse,
            fallback_factory=lambda: JudgeResponse(
                analysis="Judge produced no structured response.",
                feedback="No structured response received.",
                verdict=Verdict.FAIL,
            ),
            label=f"round-{request.round_number}-retry-{state.retry}-judge",
        )
        response.skills_used, _ = self._skills(response.skills_used)
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
        issue_board.append_judge(self.progress_path, request.round_number, state.retry, response)
        await self.workspace.snapshot(f"round-{request.round_number}-retry-{state.retry}-judge")
        return response
