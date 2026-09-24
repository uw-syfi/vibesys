"""Concrete turn and effect adapters for the built-in agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from vibesys.events import FrameworkSource
from vibesys.loops.agent import issue_board
from vibesys.loops.agent.hypothesis_controller import (
    HypothesisEngine,
    persist_active_hypothesis,
    persist_agent_run_state,
    plan_changed_keys,
    publish_experiments_changed,
)
from vibesys.loops.agent.policy_gates import (
    _run_framework_gates,
    _run_framework_validation_gate,
)
from vibesys.loops.agent.policy_support import (
    _run_implementer,
    _run_judge,
    _run_orchestrator_plan,
    _run_pre_round_decision,
    _run_profiler,
    _run_single_agent_round,
)
from vibesys.render.sink import output_sink

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.domains.base import DomainDefinition
    from vibesys.evaluators.gates import FrameworkBenchmarkOutcome
    from vibesys.evaluators.input_manifest import BenchmarkResult, ProfileGuidedInput
    from vibesys.evaluators.metrics import Objective
    from vibesys.loops.agent.model import AgentRunState, Hypothesis
    from vibesys.loops.agent.policy_attempts import AttemptRequest, AttemptState
    from vibesys.loops.agent.policy_rounds import RoundPreparationRequest
    from vibesys.loops.agent.policy_scheduler import PlanRequest
    from vibesys.loops.agent.policy_support import _ImplementerAttempt
    from vibesys.loops.agent.roles import BuiltInAgentRoles
    from vibesys.loops.agent.state import AgentRunStateStore
    from vibesys.run import LoopContext
    from vibesys.schemas import (
        ImplementerResponse,
        JudgeResponse,
        OrchestratorPlan,
        PreRoundDecision,
        ProfilerSummary,
        SingleAgentRoundResponse,
    )


@dataclass(frozen=True)
class _LocalAgentPolicyIO:
    """Bind concrete run resources once, outside policy decision code."""

    ctx: LoopContext
    agents: BuiltInAgentRoles
    state_store: AgentRunStateStore
    domain_definition: DomainDefinition
    objective: str
    modality: str | None
    interface: str
    progress_path: Path
    progress_location: str
    roadmap_path: Path
    roadmap_location: str
    pareto_archive_location: str
    framework_benchmark_configured: bool
    benchmark_result: BenchmarkResult | None
    benchmark_result_protocol: Literal[2] | None
    objectives: list[Objective]
    accuracy_timeout_seconds: int | None
    benchmark_timeout_seconds: int | None
    judge_every: int
    official_eval_every: int

    def plan(self, request: PlanRequest) -> OrchestratorPlan:
        return _run_orchestrator_plan(
            self.ctx,
            agent=self.agents.orchestrator,
            agent_run_state=request.state,
            round_number=request.round_number,
            objective=self.objective,
            profiler_summary=request.profiler_summary,
            carry=request.carry,
            progress_path=self.progress_path,
            progress_location=self.progress_location,
            roadmap_location=self.roadmap_location,
            pareto_archive_location=self.pareto_archive_location,
            plateau_warning=request.plateau_warning,
            modality=self.modality,
            interface=self.interface,
            domain_definition=self.domain_definition,
            framework_benchmark_enabled=self.framework_benchmark_configured,
            official_eval_every=self.official_eval_every,
            provisional_candidates=request.provisional_candidates,
            official_eval_cadence_due=(
                request.provisional_candidates + 1 >= self.official_eval_every
            ),
            profile_guidance=request.profile_guidance,
        )

    def persist_started(self, state: AgentRunState, plan: OrchestratorPlan) -> None:
        persist_agent_run_state(
            self.ctx,
            self.state_store,
            state,
            label=f"agent: start hypothesis {plan.hypothesis_id}",
        )
        publish_experiments_changed(
            self.ctx, state, "active_hypothesis_changed", plan_changed_keys(plan)
        )

    def record_continuation(self, round_number: int, hypothesis: Hypothesis) -> None:
        plan = hypothesis.plan
        issue_board.append_hypothesis_continuation(
            self.progress_path,
            round_number,
            plan=plan,
            started_round=hypothesis.started_round,
            continuation_step=hypothesis.next_step or plan.task,
        )
        self.ctx.lprint(
            f"[hypothesis] continuing {plan.hypothesis_id}; designer invocation skipped"
        )

    def checkout_rollback(
        self, commit: str, parent_round: int, failed_child_round: int | None
    ) -> bool:
        memory_paths = tuple(
            str(path.relative_to(self.ctx.workspace))
            for path in (
                self.roadmap_path,
                self.progress_path,
                issue_board.pareto_archive_path(self.progress_path),
            )
        )
        if not self.ctx.git.checkout_tree(commit, clean=True, preserve_paths=memory_paths):
            return False
        if failed_child_round is None:
            self.ctx.lprint(f"Reverted workspace to round {parent_round} ({commit[:8]}).")
        else:
            self.ctx.lprint(
                "Reverted workspace to the pre-hypothesis parent of "
                f"failed round {failed_child_round} ({commit[:8]}), "
                f"based on parent round {parent_round}."
            )
        return True

    def persist_rollback(self, state: AgentRunState, hypothesis: Hypothesis) -> AgentRunState:
        return persist_active_hypothesis(
            self.ctx,
            self.state_store,
            state,
            hypothesis,
            label=f"agent: set hypothesis {hypothesis.hypothesis_id} parent",
        )

    def warn(self, message: str) -> None:
        output_sink().framework_warning(message, source=FrameworkSource.LOOP)

    def pre_round_decision(
        self, request: RoundPreparationRequest, *, has_history: bool
    ) -> PreRoundDecision:
        return _run_pre_round_decision(
            self.ctx,
            agent=self.agents.orchestrator,
            round_number=request.round_number,
            objective=self.objective,
            carry=request.carry,
            progress_path=self.progress_path,
            progress_location=self.progress_location,
            has_history=has_history,
        )

    def profile(self, request: RoundPreparationRequest, focus: str) -> ProfilerSummary | None:
        return _run_profiler(
            self.ctx,
            agent=self.agents.profiler,
            round_number=request.round_number,
            profile_focus=focus,
            modality=self.modality,
            interface=self.interface,
            domain_definition=self.domain_definition,
            progress_path=self.progress_path,
            objective=self.objective,
        )

    def implement(self, request: AttemptRequest, state: AttemptState) -> _ImplementerAttempt:
        hypothesis = request.active_hypothesis
        artifacts = tuple(
            issue_board.display_path(path, self.ctx.workspace)
            for path in issue_board.implementer_artifact_paths(
                self.progress_path, request.round_number
            )
        )
        self.ctx.reselect_gpu()
        return _run_implementer(
            self.ctx,
            agent=self.agents.implementer,
            round_number=request.round_number,
            retry=state.retry,
            plan=request.plan,
            objective=self.objective,
            modality=self.modality,
            interface=self.interface,
            domain_definition=self.domain_definition,
            feedback=state.feedback,
            continuation_step=hypothesis.next_step,
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
            progress_path=self.progress_path,
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_archive_location,
            framework_benchmark_enabled=self.framework_benchmark_configured,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            prior_attempt_artifact_locations=artifacts,
            profile_guidance=request.engine.controller.guidance,
        )

    def judge(
        self, request: AttemptRequest, state: AttemptState, conflict: str | None
    ) -> JudgeResponse:
        hypothesis = request.active_hypothesis
        self.ctx.reselect_gpu()
        return _run_judge(
            self.ctx,
            agent=self.agents.judge,
            round_number=request.round_number,
            retry=state.retry,
            plan=request.plan,
            implementation=cast("ImplementerResponse", state.implementation),
            modality=self.modality,
            interface=self.interface,
            domain_definition=self.domain_definition,
            progress_path=self.progress_path,
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_archive_location,
            objective=self.objective,
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
            gate_approved_metrics=hypothesis.gate_approved_metrics,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
            framework_benchmark_enabled=self.framework_benchmark_configured,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            pareto_archive_conflict=conflict,
        )

    def combined(self, request: AttemptRequest, state: AttemptState) -> SingleAgentRoundResponse:
        self.ctx.reselect_gpu()
        return _run_single_agent_round(
            self.ctx,
            agent=self.agents.implementer,
            round_number=request.round_number,
            retry=state.retry,
            plan=request.plan,
            modality=self.modality,
            interface=self.interface,
            domain_definition=self.domain_definition,
            feedback=state.feedback,
            progress_path=self.progress_path,
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_archive_location,
            objective=self.objective,
            profile_focus=request.last_profile_focus,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            framework_benchmark_enabled=self.framework_benchmark_configured,
            pareto_records=request.records,
            space=state.agent_run_state.metrics,
        )

    def prepare_profile(
        self,
        engine: HypothesisEngine,
        state: AgentRunState,
        settings: ProfileGuidedInput,
        round_number: int,
    ) -> tuple[HypothesisEngine, AgentRunState]:
        prepared = engine.replace_state(state).prepare_profile(
            self.ctx, settings, round_number=round_number
        )
        persist_agent_run_state(
            self.ctx,
            self.state_store,
            prepared.state,
            label=f"profile-guided: prepare round {round_number}",
        )
        return prepared, prepared.state

    def checkpoint(self, request: AttemptRequest, state: AttemptState) -> None:
        state.agent_run_state = persist_active_hypothesis(
            self.ctx,
            self.state_store,
            state.agent_run_state,
            request.active_hypothesis,
            label=f"agent: checkpoint hypothesis {request.plan.hypothesis_id}",
        )

    def record_official_decision(
        self,
        request: AttemptRequest,
        state: AttemptState,
        *,
        run: bool,
        reason: str,
        provisional_candidates: int,
    ) -> None:
        issue_board.append_official_evaluation_decision(
            self.progress_path,
            request.round_number,
            state.retry,
            run=run,
            reason=reason,
            official_eval_every=self.official_eval_every,
            provisional_candidates=provisional_candidates,
        )

    def record_judge_skipped(self, request: AttemptRequest, outcome: str) -> None:
        issue_board.append_judge_skipped(
            self.progress_path,
            request.round_number,
            outcome=outcome,
            judge_every=self.judge_every,
        )

    def validate(
        self, request: AttemptRequest, state: AttemptState, recipe: str | None
    ) -> str | None:
        return _run_framework_validation_gate(
            self.ctx,
            recipe_artifact=recipe,
            round_number=request.round_number,
            retry=state.retry,
            progress_path=self.progress_path,
        )

    def current_commit(self) -> str | None:
        return self.ctx.git.current_sha()

    def official_gates(
        self,
        request: AttemptRequest,
        state: AttemptState,
        *,
        reuse_accuracy_pass: bool,
        candidate_commit: str | None,
    ) -> tuple[str | None, FrameworkBenchmarkOutcome, bool]:
        return _run_framework_gates(
            self.ctx,
            benchmark_result=self.benchmark_result,
            benchmark_result_protocol=self.benchmark_result_protocol,
            objectives=self.objectives,
            round_number=request.round_number,
            retry=state.retry,
            progress_path=self.progress_path,
            accuracy_timeout_seconds=self.accuracy_timeout_seconds,
            benchmark_timeout_seconds=self.benchmark_timeout_seconds,
            reuse_accuracy_pass=reuse_accuracy_pass,
            candidate_revision=candidate_commit,
        )

    def next_attempt(self, round_number: int) -> int:
        return issue_board.next_implementer_attempt(self.progress_path, round_number)

    def log(self, message: str) -> None:
        self.ctx.lprint(message)
