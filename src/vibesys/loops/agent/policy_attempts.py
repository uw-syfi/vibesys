"""Attempt policies for the built-in single- and multi-agent loops."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, cast

from vibesys.loops.agent import issue_board
from vibesys.loops.agent.attempt import (
    JudgeOutcome,
    JudgeSkipped,
    JudgeSkipReason,
)
from vibesys.loops.agent.hypothesis_controller import HypothesisEngine, persist_active_hypothesis
from vibesys.loops.agent.policy_gates import _run_framework_gates
from vibesys.loops.agent.policy_support import (
    _official_evaluation_reason,
    _provisional_candidates_since_official,
)
from vibesys.loops.gates import FrameworkBenchmarkOutcome

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.domains.base import DomainDefinition
    from vibesys.evaluators.input_manifest import BenchmarkResult
    from vibesys.loops.agent.model import AgentRunState, Hypothesis
    from vibesys.loops.agent.roles import BuiltInAgentRoles
    from vibesys.loops.agent.state import AgentRunStateStore
    from vibesys.loops.metrics import Objective
    from vibesys.run import LoopContext
    from vibesys.schemas import (
        ImplementerResponse,
        OrchestratorPlan,
        SingleAgentRoundResponse,
    )
    from vs_loop_state.api import PerfProvenance, RoundRecord


class AttemptDecision(StrEnum):
    """What the shared retry executor should do after a policy turn."""

    RETRY = "retry"
    FINISH = "finish"
    OFFICIAL = "official"


@dataclass(frozen=True)
class AttemptServices:
    """Run resources used by both built-in attempt policies."""

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
    max_rounds: int
    max_retries_per_round: int
    judge_every: int
    official_eval_every: int

    def checkpoint(self, request: AttemptRequest, state: AttemptState) -> None:
        """Durably retain feedback and gate state before another paid turn."""
        state.agent_run_state = persist_active_hypothesis(
            self.ctx,
            self.state_store,
            state.agent_run_state,
            request.active_hypothesis,
            label=f"agent: checkpoint hypothesis {request.plan.hypothesis_id}",
        )

    def official_reason(self, request: AttemptRequest, *, candidate_ready: bool) -> str | None:
        """Apply the framework's official evaluation cadence."""
        return _official_evaluation_reason(
            records=request.records,
            round_number=request.round_number,
            max_rounds=self.max_rounds,
            official_eval_every=self.official_eval_every,
            requested=request.plan.request_official_evaluation,
            candidate_ready=candidate_ready,
        )

    def record_official_decision(
        self, request: AttemptRequest, state: AttemptState, *, run: bool, reason: str
    ) -> None:
        """Record cadence decisions beside the durable attempt marker."""
        issue_board.append_official_evaluation_decision(
            self.progress_path,
            request.round_number,
            state.retry,
            run=run,
            reason=reason,
            official_eval_every=self.official_eval_every,
            provisional_candidates=_provisional_candidates_since_official(request.records),
        )


@dataclass(frozen=True)
class AttemptRequest:
    """Round facts consumed by an attempt policy."""

    round_number: int
    plan: OrchestratorPlan
    planned_official_reason: str | None
    records: list[RoundRecord]
    active_hypothesis: Hypothesis
    engine: HypothesisEngine
    last_profile_focus: str


@dataclass
class AttemptState:
    """Mutable facts that survive retries within one framework round."""

    agent_run_state: AgentRunState
    feedback: str | None
    implementation: ImplementerResponse | None = None
    single_agent_response: SingleAgentRoundResponse | None = None
    judge: JudgeOutcome = field(default_factory=lambda: JudgeSkipped(JudgeSkipReason.NOT_REACHED))
    passed: bool = False
    review_started: bool = False
    revalidation_required: bool = False
    official_reason: str | None = None
    framework_benchmark: FrameworkBenchmarkOutcome = field(
        default_factory=FrameworkBenchmarkOutcome
    )
    framework_perf_metric: float | None = None
    retry: int = 0


@dataclass(frozen=True)
class PerformanceProjection:
    """Performance evidence produced by an attempt policy for the round record."""

    metric: float | None
    unit: str | None
    provenance: PerfProvenance | None
    profile_skipped: bool
    accepted_metrics: dict[str, float]
    accepted_evaluation_artifact: str | None
    next_single_response: SingleAgentRoundResponse | None


class AttemptPolicy(Protocol):
    """Role communication and review decisions for one built-in loop kind."""

    def run_attempt(self, request: AttemptRequest, state: AttemptState, /) -> AttemptDecision:
        """Run one attempt and tell the executor whether to retry or evaluate."""
        ...

    def project_performance(
        self, request: AttemptRequest, state: AttemptState, /
    ) -> PerformanceProjection:
        """Select trusted headline evidence and the next round's profile source."""
        ...

    def reviewed(self, state: AttemptState, /) -> bool:
        """Report whether the final attempt was independently reviewed."""
        ...

    def keeps_hypothesis_active(self, state: AttemptState, continuation_rounds: int, /) -> bool:
        """Report whether the implementation retains its bounded ownership lease."""
        ...

    def terminal_success_needs_parent_choice(
        self, state: AttemptState, continuation_rounds: int, /
    ) -> bool:
        """Report whether terminal edits need an explicit parent choice."""
        ...


def run_official_gates(
    services: AttemptServices, request: AttemptRequest, state: AttemptState
) -> bool:
    """Run framework-owned gates and retain enough state to resume a failed gate."""
    reason = cast("str", state.official_reason)
    services.record_official_decision(request, state, run=True, reason=reason)
    candidate_commit = services.ctx.git.current_sha()
    hypothesis = request.active_hypothesis
    reuse_accuracy_pass = bool(
        hypothesis.gate_revalidation_pending
        and candidate_commit is not None
        and hypothesis.gate_candidate_commit == candidate_commit
        and hypothesis.gate_accuracy_passed
    )
    gate_feedback, state.framework_benchmark, accuracy_passed = _run_framework_gates(
        services.ctx,
        benchmark_result=services.benchmark_result,
        benchmark_result_protocol=services.benchmark_result_protocol,
        objectives=services.objectives,
        round_number=request.round_number,
        retry=state.retry,
        progress_path=services.progress_path,
        accuracy_timeout_seconds=services.accuracy_timeout_seconds,
        benchmark_timeout_seconds=services.benchmark_timeout_seconds,
        reuse_accuracy_pass=reuse_accuracy_pass,
        candidate_revision=candidate_commit,
    )
    state.framework_perf_metric = state.framework_benchmark.metric_value
    if gate_feedback is None:
        state.passed = True
        return True
    state.feedback = gate_feedback
    state.revalidation_required = True
    hypothesis.gate_revalidation_pending = True
    hypothesis.gate_candidate_commit = candidate_commit
    hypothesis.gate_accuracy_passed = accuracy_passed
    hypothesis.feedback = state.feedback
    services.checkpoint(request, state)
    return False
