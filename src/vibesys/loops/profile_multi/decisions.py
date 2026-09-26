"""Hypothesis scheduling and review cadence owned by the profile guided multi strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.agent_run.evidence import (
    CarryOver,
    _provisional_candidates_since_official,
    _terminal_workspace_notice,
)
from vibesys.loops.profile_multi.controller import (
    HypothesisEngine,
    ProfileGuidanceOutcome,
    ProfileGuidanceView,
)
from vibesys.schemas import HypothesisOutcome

if TYPE_CHECKING:
    from vibesys.agent_run.attempts import AttemptState
    from vibesys.agent_run.state import AgentRunState, Hypothesis
    from vibesys.evaluators.perf_reply import ProfilerSummary
    from vibesys.search.hypothesis import OrchestratorPlan

    # ImplementerReply is the structural protocol AttemptState.implementation
    # is typed against (vibesys.search.hypothesis.attempts); accepting it here
    # (rather than the concrete vibesys.schemas.ImplementerResponse) covers
    # exactly the fields these functions read.
    from vibesys.search.hypothesis.attempts import ImplementerReply
    from vs_loop_state.api import RoundRecord

_MAX_CONTINUATION_ROUNDS_WITHOUT_DESIGN_REVIEW = 2


@dataclass(frozen=True)
class PlanRequest:
    """Evidence supplied to the multi designer."""

    round_number: int
    state: AgentRunState
    records: list[RoundRecord]
    carry: CarryOver
    profiler_summary: ProfilerSummary | None
    plateau_warning: str | None
    provisional_candidates: int
    profile_guidance: ProfileGuidanceView


@dataclass(frozen=True)
class RoundSelection:
    """Selected designer claim and planned official cadence."""

    engine: HypothesisEngine
    state: AgentRunState
    hypothesis: Hypothesis
    plan: OrchestratorPlan
    planned_official_reason: str | None


@dataclass(frozen=True)
class AttemptRequest:
    """Multi strategy facts for one bounded implementer attempt."""

    round_number: int
    plan: OrchestratorPlan
    planned_official_reason: str | None
    records: list[RoundRecord]
    active_hypothesis: Hypothesis
    engine: HypothesisEngine
    last_profile_focus: str


@dataclass(frozen=True)
class TerminalRequest:
    """Completed evidence for the next profile guided multi hypothesis decision."""

    engine: HypothesisEngine
    state: AgentRunState
    hypothesis: Hypothesis
    attempt: AttemptState
    record: RoundRecord
    records: list[RoundRecord]
    carry: CarryOver
    reviewed: bool
    max_retries_per_round: int


@dataclass(frozen=True)
class TerminalTransition:
    """State and carry committed after a multi round."""

    engine: HypothesisEngine
    state: AgentRunState
    carry: CarryOver
    exhaustion_feedback: str | None


class TerminalPolicy(Protocol):
    """Multi-specific terminal facts consumed by its scheduler."""

    def keeps_hypothesis_active(self, state: AttemptState, continuation_rounds: int) -> bool:
        """Report whether the implementer retains its lease."""
        ...

    def terminal_success_needs_parent_choice(
        self, state: AttemptState, continuation_rounds: int
    ) -> bool:
        """Report whether the next designer must choose a parent."""
        ...


def implementation_requests_continuation(implementation: ImplementerReply | None) -> bool:
    """Require a concrete unfinished same-hypothesis step."""
    return bool(
        implementation is not None
        and implementation.hypothesis_outcome
        in {
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.IMPLEMENTATION_FAILED,
            HypothesisOutcome.INCONCLUSIVE,
        }
        and implementation.next_step.strip()
    )


def implementation_keeps_hypothesis_active(
    implementation: ImplementerReply | None, *, continuation_rounds: int = 0
) -> bool:
    """Bound the implementer's continuation lease to two rounds."""
    return implementation_requests_continuation(implementation) and (
        continuation_rounds < _MAX_CONTINUATION_ROUNDS_WITHOUT_DESIGN_REVIEW
    )


def review_due(
    *,
    round_number: int,
    max_rounds: int,
    judge_every: int,
    outcome: HypothesisOutcome,
    candidate_evidence_fresh: bool = False,
) -> bool:
    """Require review at cadence, final round, or on a new candidate claim."""
    return (
        round_number == max_rounds
        or round_number % judge_every == 0
        or outcome in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
        or candidate_evidence_fresh
    )


def candidate_evidence_is_fresh(
    implementation: ImplementerReply, records: list[RoundRecord]
) -> bool:
    """Detect a previously unseen objective row requiring review."""
    if not implementation.candidate_metrics:
        return False
    artifact = implementation.candidate_evaluation_artifact
    if not artifact:
        return True
    metrics = dict(implementation.candidate_metrics)
    return not any(
        (record.candidate_evaluation_artifact or record.evaluation_artifact) == artifact
        and record.candidate_metrics == metrics
        for record in records
    )


def official_evaluation_reason(  # noqa: PLR0913  # lint-waiver: LW-020022 [PLR0913]; the scheduling inputs are independent facts read from records and options, and a wrapper object would only repack them.
    *,
    records: list[RoundRecord],
    round_number: int,
    max_rounds: int,
    official_eval_every: int,
    requested: bool,
    candidate_ready: bool,
) -> str | None:
    """Schedule gates by accepted candidates and the final round."""
    if round_number == max_rounds:
        return "final_round"
    if not candidate_ready:
        return None
    if requested:
        return "orchestrator_request"
    if _provisional_candidates_since_official(records) + 1 >= official_eval_every:
        return "cadence"
    return None


def transition_round(policy: TerminalPolicy, request: TerminalRequest) -> TerminalTransition:
    """Advance one profile guided multi hypothesis and choose the next designer handoff."""
    attempt = request.attempt
    passed = attempt.passed
    feedback = attempt.feedback
    implementation = attempt.implementation
    next_active = request.hypothesis.clone()
    if policy.keeps_hypothesis_active(attempt, next_active.continuation_rounds):
        next_active.feedback = feedback if request.reviewed and not passed else None
        if implementation is None:
            message = "a kept-active hypothesis requires an implementation result"
            raise RuntimeError(message)
        next_active.next_step = implementation.next_step
        next_active.continuation_rounds += 1
    elif passed:
        next_active = None
    elif (
        request.reviewed
        and next_active.continuation_rounds < _MAX_CONTINUATION_ROUNDS_WITHOUT_DESIGN_REVIEW
    ):
        next_active.feedback = feedback
        next_active.next_step = (
            implementation.next_step
            if implementation is not None and implementation_requests_continuation(implementation)
            else None
        )
        next_active.continuation_rounds += 1
    elif request.reviewed or (
        implementation is not None
        and not implementation_keeps_hypothesis_active(
            implementation, continuation_rounds=next_active.continuation_rounds
        )
    ):
        next_active = None
    else:
        next_active.feedback = None
        next_active.next_step = implementation.next_step if implementation is not None else None
    profile_outcome = ProfileGuidanceOutcome.from_round(
        request.record.round_number,
        passed=passed,
        official=request.record.official_evaluation,
        delta_pct=request.record.perf_delta_pct,
    )
    engine = request.engine.replace_state(request.state).complete_round(
        request.record,
        next_active=next_active,
        profile_outcome=profile_outcome,
    )
    records = [*request.records, request.record]
    carry = CarryOver(
        regression_info=request.carry.regression_info,
        exhaustion_info=request.carry.exhaustion_info,
    )
    exhaustion_feedback: str | None = None
    if not passed and request.record.reviewed:
        exhaustion_feedback = feedback or ""
        carry.exhaustion_info = (
            f"Round {request.record.round_number} did not pass after "
            f"{request.max_retries_per_round} attempts. Last judge feedback: "
            f"{feedback or '(empty)'}"
        )
        carry.regression_info = None
    elif passed:
        carry.exhaustion_info = None
        if policy.terminal_success_needs_parent_choice(
            attempt, request.hypothesis.continuation_rounds
        ):
            carry.regression_info = _terminal_workspace_notice(records)
        elif request.record.official_evaluation and request.record.candidate_retained is False:
            carry.regression_info = (
                f"Round {request.record.round_number}'s official candidate was not retained: "
                f"{request.record.perf_metric}"
                f"{(' ' + request.record.perf_unit) if request.record.perf_unit else ''}. "
                "Use its recorded parent and objective directions when choosing the next checkpoint."
            )
        else:
            carry.regression_info = None
    else:
        carry.exhaustion_info = None
        carry.regression_info = (
            None
            if implementation_keeps_hypothesis_active(
                implementation, continuation_rounds=request.hypothesis.continuation_rounds
            )
            else _terminal_workspace_notice(records)
        )
    return TerminalTransition(engine, engine.state, carry, exhaustion_feedback)
