"""Concrete turn and effect adapters for the built-in agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from vibesys.loops.agent import issue_board
from vibesys.loops.agent.hypothesis_controller import (
    HypothesisEngine,
    persist_active_hypothesis,
    persist_agent_run_state,
)
from vibesys.loops.agent.policy_gates import (
    _run_framework_gates,
    _run_framework_validation_gate,
)
from vibesys.loops.agent.policy_support import (
    _run_implementer,
    _run_judge,
    _run_pre_round_decision,
    _run_profiler,
    _run_single_agent_round,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.domains.base import DomainDefinition
    from vibesys.evaluators.input_manifest import BenchmarkResult, ProfileGuidedInput
    from vibesys.loops.agent.model import AgentRunState
    from vibesys.loops.agent.policy_attempts import AttemptRequest, AttemptState
    from vibesys.loops.agent.policy_rounds import RoundPreparationRequest
    from vibesys.loops.agent.policy_support import _ImplementerAttempt
    from vibesys.loops.agent.roles import BuiltInAgentRoles
    from vibesys.loops.agent.state import AgentRunStateStore
    from vibesys.loops.gates import FrameworkBenchmarkOutcome
    from vibesys.loops.metrics import Objective
    from vibesys.run import LoopContext
    from vibesys.schemas import (
        ImplementerResponse,
        JudgeResponse,
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
    pareto_archive_location: str
    framework_benchmark_configured: bool
    benchmark_result: BenchmarkResult | None
    benchmark_result_protocol: Literal[2] | None
    objectives: list[Objective]
    accuracy_timeout_seconds: int | None
    benchmark_timeout_seconds: int | None
    judge_every: int
    official_eval_every: int

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
