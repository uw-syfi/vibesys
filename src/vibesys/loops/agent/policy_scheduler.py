"""Outer hypothesis scheduling for the built-in agent policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.loops.agent.policy_ports import ProfileEffect
from vibesys.loops.agent.policy_profile import ProfileOutcomeInput, ProfilePreparation
from vibesys.loops.agent.policy_rounds import RoundPreparationRequest
from vibesys.loops.agent.policy_support import (
    _FAILED_HYPOTHESIS_OUTCOMES,
    _MAX_CONTINUATION_ROUNDS_WITHOUT_DESIGN_REVIEW,
    _CarryOver,
    _detect_plateau,
    _implementation_keeps_hypothesis_active,
    _implementation_requests_continuation,
    _official_evaluation_reason,
    _provisional_candidates_since_official,
    _terminal_workspace_notice,
)

if TYPE_CHECKING:
    from vibesys.loops.agent.hypothesis_controller import (
        HypothesisEngine,
        ProfileGuidanceOutcome,
        ProfileGuidanceView,
    )
    from vibesys.loops.agent.model import AgentRunState, Hypothesis
    from vibesys.loops.agent.policy_attempts import AttemptState
    from vibesys.schemas import OrchestratorPlan, ProfilerSummary, SingleAgentRoundResponse
    from vs_loop_state.api import RoundHistory, RoundRecord


@dataclass(frozen=True)
class PlanRequest:
    """Policy evidence available to the designer before a new hypothesis."""

    round_number: int
    state: AgentRunState
    records: list[RoundRecord]
    carry: _CarryOver
    profiler_summary: ProfilerSummary | None
    plateau_warning: str | None
    provisional_candidates: int
    profile_guidance: ProfileGuidanceView


class SchedulerEffects(ProfileEffect, Protocol):
    """Plan turn and durable writes needed by hypothesis selection."""

    def plan(self, request: PlanRequest, /) -> OrchestratorPlan:
        """Run one designer turn and return its parsed plan."""
        ...

    def current_commit(self) -> str | None:
        """Return the current workspace revision for parent selection."""
        ...

    def persist_started(self, state: AgentRunState, plan: OrchestratorPlan, /) -> None:
        """Commit and publish a newly active hypothesis."""
        ...

    def record_continuation(self, round_number: int, hypothesis: Hypothesis, /) -> None:
        """Journal a round that reuses an active hypothesis."""
        ...

    def checkout_rollback(
        self, commit: str, parent_round: int, failed_child_round: int | None, /
    ) -> bool:
        """Restore the requested candidate tree while preserving run memory."""
        ...

    def persist_rollback(self, state: AgentRunState, hypothesis: Hypothesis, /) -> AgentRunState:
        """Commit the active hypothesis's new parent checkpoint."""
        ...

    def warn(self, message: str, /) -> None:
        """Report an unavailable or unapplied rollback."""
        ...


class RoundSelectionPolicy(Protocol):
    """Flow decisions used before an attempt begins."""

    def prepare_profile(
        self, request: ProfilePreparation, /
    ) -> tuple[HypothesisEngine, AgentRunState]:
        """Prepare outer profile guidance before planning."""
        ...

    def profiler_summary(self, request: RoundPreparationRequest, /) -> ProfilerSummary | None:
        """Return fresh or carried profiler evidence."""
        ...

    def official_reason(self, reason: str | None, engine: HypothesisEngine, /) -> str | None:
        """Adjust the planned official evaluation reason."""
        ...


class TerminalPolicy(Protocol):
    """Flow decisions used when a completed round advances its hypothesis."""

    def keeps_hypothesis_active(self, state: AttemptState, continuation_rounds: int, /) -> bool:
        """Decide whether the implementer keeps its current claim."""
        ...

    def terminal_success_needs_parent_choice(
        self, state: AttemptState, continuation_rounds: int, /
    ) -> bool:
        """Decide whether the next designer must select a parent."""
        ...

    def profile_outcome(self, request: ProfileOutcomeInput, /) -> ProfileGuidanceOutcome | None:
        """Return profile controller evidence for this completed round."""
        ...


@dataclass(frozen=True)
class RoundSelection:
    """State and plan selected for one framework round."""

    engine: HypothesisEngine
    state: AgentRunState
    hypothesis: Hypothesis
    plan: OrchestratorPlan
    planned_official_reason: str | None


@dataclass(frozen=True)
class RoundSelectionRequest:
    """Current hypothesis state and limits for one round selection."""

    engine: HypothesisEngine
    state: AgentRunState
    records: list[RoundRecord]
    carry: _CarryOver
    round_number: int
    max_rounds: int
    official_eval_every: int
    previous_single_response: SingleAgentRoundResponse | None


def select_round(
    flow: RoundSelectionPolicy,
    effects: SchedulerEffects,
    request: RoundSelectionRequest,
) -> RoundSelection:
    """Choose a new designer claim or continue the current implementer lease."""
    engine = request.engine
    state = request.state
    records = request.records
    carry = request.carry
    round_number = request.round_number
    hypothesis = state.active_hypothesis
    if hypothesis is None:
        engine, state = flow.prepare_profile(
            ProfilePreparation(
                effects=effects,
                engine=engine,
                state=state,
                round_number=round_number,
            )
        )
        profiler_summary = flow.profiler_summary(
            RoundPreparationRequest(
                round_number=round_number,
                records=records,
                carry=carry,
                previous_single_response=request.previous_single_response,
            )
        )
        provisional_candidates = _provisional_candidates_since_official(records)
        plan = effects.plan(
            PlanRequest(
                round_number=round_number,
                state=state,
                records=records,
                carry=carry,
                profiler_summary=profiler_summary,
                plateau_warning=_detect_plateau(records),
                provisional_candidates=provisional_candidates,
                profile_guidance=engine.controller.guidance,
            )
        )
        parent_round = (
            plan.revert_to_round
            if plan.revert_to_round is not None
            else round_number - 1
            if round_number > 1
            else None
        )
        parent_record = next(
            (record for record in reversed(records) if record.round_number == parent_round),
            None,
        )
        engine = engine.replace_state(state).start(
            plan,
            started_round=round_number,
            parent_round=parent_round,
            parent_commit=(
                parent_record.commit
                if parent_record is not None and parent_record.commit is not None
                else effects.current_commit()
            ),
        )
        state = engine.state
        hypothesis = state.active_hypothesis
        assert hypothesis is not None  # noqa: S101  # started above
        plan = hypothesis.plan
        effects.persist_started(state, plan)
    else:
        plan = hypothesis.plan
        effects.record_continuation(round_number, hypothesis)
    planned_official_reason = _official_evaluation_reason(
        records=records,
        round_number=round_number,
        max_rounds=request.max_rounds,
        official_eval_every=request.official_eval_every,
        requested=plan.request_official_evaluation,
        candidate_ready=True,
    )
    return RoundSelection(
        engine=engine,
        state=state,
        hypothesis=hypothesis,
        plan=plan,
        planned_official_reason=flow.official_reason(planned_official_reason, engine),
    )


def apply_requested_rollback(
    effects: SchedulerEffects,
    selection: RoundSelection,
    history: RoundHistory,
    records: list[RoundRecord],
) -> AgentRunState:
    """Apply a designer-selected parent only after its tree checkout succeeds."""
    plan = selection.plan
    hypothesis = selection.hypothesis
    parent_round = plan.revert_to_round
    if parent_round is None or hypothesis.revert_applied:
        return selection.state
    target = next((record for record in records if record.round_number == parent_round), None)
    if target is None or not target.commit:
        effects.warn(f"cannot revert: no commit recorded for round {parent_round}")
        return selection.state
    rollback_commit, failed_child_round = history.resolve_rollback_commit(
        target, _FAILED_HYPOTHESIS_OUTCOMES
    )
    assert rollback_commit is not None  # noqa: S101  # resolved from committed target
    if not effects.checkout_rollback(rollback_commit, parent_round, failed_child_round):
        effects.warn(
            f"rollback was not applied; will retry round {parent_round} on the next continuation"
        )
        return selection.state
    hypothesis.revert_applied = True
    hypothesis.revert_commit = rollback_commit
    hypothesis.parent_commit = rollback_commit
    return effects.persist_rollback(selection.state, hypothesis)


@dataclass(frozen=True)
class TerminalRequest:
    """Completed round evidence needed to choose the next hypothesis state."""

    engine: HypothesisEngine
    state: AgentRunState
    hypothesis: Hypothesis
    attempt: AttemptState
    record: RoundRecord
    records: list[RoundRecord]
    carry: _CarryOver
    reviewed: bool
    max_retries_per_round: int


@dataclass(frozen=True)
class TerminalTransition:
    """Pure next state and carry to commit after the write-ahead record."""

    engine: HypothesisEngine
    state: AgentRunState
    carry: _CarryOver
    exhaustion_feedback: str | None


def transition_round(flow: TerminalPolicy, request: TerminalRequest) -> TerminalTransition:
    """Advance the active claim and derive the next designer handoff."""
    attempt = request.attempt
    passed = attempt.passed
    feedback = attempt.feedback
    implementation = attempt.implementation
    next_active = request.hypothesis.clone()
    if flow.keeps_hypothesis_active(attempt, next_active.continuation_rounds):
        next_active.feedback = feedback if request.reviewed and not passed else None
        assert implementation is not None  # noqa: S101  # policy retains implementer lease
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
            if implementation is not None and _implementation_requests_continuation(implementation)
            else None
        )
        next_active.continuation_rounds += 1
    elif request.reviewed or (
        implementation is not None
        and not _implementation_keeps_hypothesis_active(
            implementation, continuation_rounds=next_active.continuation_rounds
        )
    ):
        next_active = None
    else:
        next_active.feedback = None
        next_active.next_step = implementation.next_step if implementation is not None else None
    profile_outcome = flow.profile_outcome(
        ProfileOutcomeInput(
            request.record.round_number,
            passed,
            request.record.official_evaluation,
            request.record.perf_delta_pct,
        )
    )
    engine = request.engine.replace_state(request.state).complete_round(
        request.record,
        next_active=next_active,
        profile_outcome=profile_outcome,
    )
    records = [*request.records, request.record]
    carry = _CarryOver(
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
        if flow.terminal_success_needs_parent_choice(
            attempt, request.hypothesis.continuation_rounds
        ):
            carry.regression_info = _terminal_workspace_notice(records)
        elif request.record.official_evaluation and request.record.candidate_retained is False:
            carry.regression_info = (
                f"Round {request.record.round_number}'s official candidate was not retained: "
                f"{request.record.perf_metric}"
                f"{(' ' + request.record.perf_unit) if request.record.perf_unit else ''}. "
                "Use its recorded parent and objective directions when choosing "
                "the next checkpoint."
            )
        else:
            carry.regression_info = None
    else:
        carry.exhaustion_info = None
        carry.regression_info = (
            None
            if _implementation_keeps_hypothesis_active(
                implementation,
                continuation_rounds=request.hypothesis.continuation_rounds,
            )
            else _terminal_workspace_notice(records)
        )
    return TerminalTransition(engine, engine.state, carry, exhaustion_feedback)
