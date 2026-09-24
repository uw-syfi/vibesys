"""Attempt policies for the built-in single- and multi-agent loops."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

from vibesys.evaluators.gates import FrameworkBenchmarkOutcome
from vibesys.loops.agent.attempt import (
    JudgeOutcome,
    JudgeSkipped,
    JudgeSkipReason,
)
from vibesys.loops.agent.policy_support import (
    _official_evaluation_reason,
    _provisional_candidates_since_official,
)

if TYPE_CHECKING:
    from vibesys.loops.agent.hypothesis_controller import HypothesisEngine
    from vibesys.loops.agent.model import AgentRunState, Hypothesis
    from vibesys.loops.agent.policy_ports import AgentTurns, RoundEffects
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
    """Turn and effect ports plus scalar policy settings."""

    turns: AgentTurns
    effects: RoundEffects
    max_rounds: int
    max_retries_per_round: int
    judge_every: int
    official_eval_every: int

    def checkpoint(self, request: AttemptRequest, state: AttemptState) -> None:
        """Durably retain feedback and gate state before another paid turn."""
        self.effects.checkpoint(request, state)

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
        self.effects.record_official_decision(
            request,
            state,
            run=run,
            reason=reason,
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


class AttemptRunner(Protocol):
    """Minimum policy operation needed by the shared retry executor."""

    def run_attempt(self, request: AttemptRequest, state: AttemptState, /) -> AttemptDecision:
        """Execute one paid attempt and select its next framework action."""
        ...


def run_official_gates(
    services: AttemptServices, request: AttemptRequest, state: AttemptState
) -> bool:
    """Run framework-owned gates and retain enough state to resume a failed gate."""
    reason = cast("str", state.official_reason)
    services.record_official_decision(request, state, run=True, reason=reason)
    candidate_commit = services.effects.current_commit()
    hypothesis = request.active_hypothesis
    reuse_accuracy_pass = bool(
        hypothesis.gate_revalidation_pending
        and candidate_commit is not None
        and hypothesis.gate_candidate_commit == candidate_commit
        and hypothesis.gate_accuracy_passed
    )
    gate_feedback, state.framework_benchmark, accuracy_passed = services.effects.official_gates(
        request,
        state,
        reuse_accuracy_pass=reuse_accuracy_pass,
        candidate_commit=candidate_commit,
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


def execute_attempts(
    flow: AttemptRunner,
    services: AttemptServices,
    request: AttemptRequest,
    state: AttemptState,
) -> None:
    """Resume at the first unpaid attempt and run the bounded retry sequence."""
    first_retry = services.effects.next_attempt(request.round_number)
    if first_retry > services.max_retries_per_round:
        raise RuntimeError(  # noqa: TRY003  # tracked: #288
            f"Round {request.round_number} already persisted "
            f"{first_retry - 1} implementer attempts, exhausting "
            f"max_retries_per_round={services.max_retries_per_round}; refusing "
            "to overwrite or replay paid work."
        )
    if first_retry > 1:
        services.effects.log(
            f"[resume] round {request.round_number} continues at durable "
            f"attempt {first_retry}/{services.max_retries_per_round}"
        )
    for retry in range(first_retry, services.max_retries_per_round + 1):
        services.effects.log(f"\n--- attempt {retry}/{services.max_retries_per_round} ---\n")
        state.retry = retry
        state.judge = JudgeSkipped(JudgeSkipReason.NOT_REACHED)
        state.official_reason = None
        decision = flow.run_attempt(request, state)
        if decision is AttemptDecision.FINISH:
            break
        if decision is AttemptDecision.OFFICIAL and run_official_gates(services, request, state):
            break
