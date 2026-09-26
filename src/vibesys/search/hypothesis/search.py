"""``HypothesisSearch``: the public facade over hypothesis-lifecycle transitions.

Replaces ``HypothesisEngine`` (+ its ``replace_state``), ``TerminalPolicy`` /
``_TerminalPolicy`` / ``transition_round``, and the carrier types from
``loops/{multi,profile_multi}/decisions.py``. A strategy holds one
``HypothesisSearch`` built from its :class:`~vibesys.search.hypothesis.config.HypothesisConfig`
and calls its methods with plain, explicit facts instead of passing itself
(or a role-reply object) through a carrier type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.search.hypothesis import cadence, transitions
from vibesys.search.hypothesis.results import (
    AttemptBudget,
    ClosedRound,
    Continue,
    Finished,
    NewHypothesis,
    NextRoundDecision,
    PlanningContext,
    RollbackTarget,
    StartedHypothesis,
)
from vibesys.search.hypothesis.state import HypothesisState
from vibesys.search.hypothesis.transitions import (
    FAILED_HYPOTHESIS_OUTCOMES,
    CarryOver,
)
from vs_loop_state.api import RoundHistory

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.evaluators.metrics import MetricSpace
    from vibesys.schemas import (
        CandidateDisposition,
        HypothesisOutcome,
        PerfDeltaReason,
    )
    from vibesys.search.hypothesis.config import HypothesisConfig
    from vibesys.search.hypothesis.plan import HypothesisStrategyUpdate, OrchestratorPlan
    from vibesys.search.hypothesis.state import Hypothesis, RoundRecord

__all__ = ["HypothesisSearch"]


@dataclass(frozen=True, slots=True)
class HypothesisSearch:
    """Pure hypothesis-lifecycle policy bound to one strategy's configuration."""

    config: HypothesisConfig

    def initial(self) -> HypothesisState:
        """Return the empty starting state for a new run."""
        return HypothesisState()

    def resume(self, state: HypothesisState | None, metric_space: MetricSpace) -> HypothesisState:
        """Recover a run's durable state, reprojecting it onto *metric_space*.

        ``state`` is the last committed checkpoint, or ``None`` for a fresh
        run. Reprojection is a no-op when the metric space is unchanged, so
        calling this unconditionally on every open is safe and idempotent.
        """
        return transitions.adopt_metric_space(state or self.initial(), metric_space)

    def initial_carry(self, records: Sequence[RoundRecord]) -> CarryOver:
        """Return the resumed carry-over, seeded from any pending workspace notice."""
        return CarryOver(regression_info=transitions.terminal_workspace_notice(records))

    def finish(self, state: HypothesisState) -> HypothesisState:
        """Clear the active hypothesis pointer without changing the hypothesis itself."""
        return transitions.finish_hypothesis(state)

    def next_round(
        self,
        state: HypothesisState,
        *,
        round_number: int,
        records: Sequence[RoundRecord],
        carry: CarryOver,
    ) -> NextRoundDecision:
        """Decide whether to finish, continue, or plan a new hypothesis."""
        if round_number > self.config.max_rounds:
            return Finished()
        context = PlanningContext(
            round_number=round_number,
            records=tuple(records),
            carry=carry,
            plateau_warning=transitions.detect_plateau(records),
            provisional_candidates=transitions.provisional_candidates_since_official(records),
        )
        active = state.active_hypothesis
        if active is not None:
            return Continue(hypothesis=active, context=context)
        default_parent_round = round_number - 1 if round_number > 1 else None
        return NewHypothesis(default_parent_round=default_parent_round, context=context)

    def start(
        self,
        state: HypothesisState,
        plan: OrchestratorPlan,
        *,
        round_number: int,
        current_commit: str | None,
        records: Sequence[RoundRecord],
    ) -> StartedHypothesis:
        """Start a designer's new hypothesis and resolve its rollback target.

        ``current_commit`` seeds the parent commit when no earlier round
        recorded one. Rollback resolution (``RoundHistory.resolve_rollback_commit``)
        is for orchestration's workspace checkout; it never touches the
        filesystem itself.
        """
        records = list(records)
        parent_round = plan.revert_to_round or (round_number - 1 if round_number > 1 else None)
        parent = next(
            (record for record in reversed(records) if record.round_number == parent_round),
            None,
        )
        parent_commit = (
            parent.commit if parent is not None and parent.commit is not None else current_commit
        )
        rollback: RollbackTarget | None = None
        if plan.revert_to_round is not None:
            if parent is None or not parent.commit:
                rollback = RollbackTarget(commit=None, failed_child_round=None, resolved=False)
            else:
                commit, failed_child = RoundHistory(records=records).resolve_rollback_commit(
                    parent, FAILED_HYPOTHESIS_OUTCOMES
                )
                rollback = RollbackTarget(
                    commit=commit, failed_child_round=failed_child, resolved=commit is not None
                )
        new_state = transitions.start_hypothesis(
            state,
            plan,
            started_round=round_number,
            parent_round=parent_round,
            parent_commit=parent_commit,
        )
        hypothesis = new_state.active_hypothesis
        assert hypothesis is not None  # noqa: S101  # start_hypothesis always activates one  # LW-040040 [S101]; the invariant is established by the call just above, and the assert narrows the optional type for the checker.
        return StartedHypothesis(state=new_state, hypothesis=hypothesis, rollback=rollback)

    def attempts(self, *, retry: int) -> AttemptBudget:
        """Return this attempt's position within the round's retry budget."""
        return AttemptBudget(retry=retry, max_retries=self.config.max_retries_per_round)

    def review_due(  # noqa: PLR0913  # LW-040041 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
        self,
        *,
        round_number: int,
        outcome: HypothesisOutcome,
        candidate_evidence_is_fresh: bool = False,
        review_started: bool = False,
        requests_continuation: bool = False,
        pareto_frontier_claim: bool = False,
        revalidation_required: bool = False,
    ) -> bool:
        """Require an independent review at cadence, or by an override."""
        return cadence.review_due(
            self.config,
            round_number=round_number,
            outcome=outcome,
            candidate_evidence_is_fresh=candidate_evidence_is_fresh,
            review_started=review_started,
            requests_continuation=requests_continuation,
            pareto_frontier_claim=pareto_frontier_claim,
            revalidation_required=revalidation_required,
        )

    def official_due(
        self,
        *,
        records: Sequence[RoundRecord],
        round_number: int,
        requested: bool,
        candidate_ready: bool,
    ) -> str | None:
        """Return why an official evaluation is due this round, or ``None``."""
        return cadence.official_due(
            self.config,
            records=records,
            round_number=round_number,
            requested=requested,
            candidate_ready=candidate_ready,
        )

    def close_round(  # noqa: PLR0913  # LW-040042 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
        self,
        state: HypothesisState,
        *,
        hypothesis: Hypothesis,
        record: RoundRecord,
        records: Sequence[RoundRecord],
        carry: CarryOver,
        passed: bool,
        reviewed: bool,
        feedback: str | None,
        keeps_active: bool,
        requests_continuation: bool,
        next_step: str | None,
        terminal_needs_parent_choice: bool,
        has_implementation: bool = True,
    ) -> ClosedRound:
        """Commit one completed round and choose the next designer handoff.

        Ported from ``transition_round`` in ``loops/{multi,profile_multi}/decisions.py``.
        Callers evaluate their own strategy's ``keeps_hypothesis_active`` /
        ``terminal_success_needs_parent_choice`` policy (today's
        ``_TerminalPolicy``) and pass the results in as plain booleans.
        """
        next_active = _next_active(
            hypothesis,
            keeps_active=keeps_active,
            passed=passed,
            reviewed=reviewed,
            requests_continuation=requests_continuation,
            has_implementation=has_implementation,
            next_step=next_step,
            feedback=feedback,
            max_continuation_rounds=self.config.max_continuation_rounds,
        )
        updated = (
            transitions.update_active_hypothesis(state, next_active)
            if next_active is not None
            else state
        )
        updated = transitions.append_round(updated, record, keep_active=next_active is not None)
        all_records = [*records, record]
        new_carry, exhaustion_feedback = _carry_over(
            carry,
            passed=passed,
            reviewed=reviewed,
            record=record,
            all_records=all_records,
            max_retries_per_round=self.config.max_retries_per_round,
            feedback=feedback,
            terminal_needs_parent_choice=terminal_needs_parent_choice,
            keeps_active=keeps_active,
        )
        return ClosedRound(
            state=updated,
            next_active=next_active,
            carry=new_carry,
            exhaustion_feedback=exhaustion_feedback,
        )

    def frontier(self, records: Sequence[RoundRecord], *, space: MetricSpace) -> list[RoundRecord]:
        """Return the noise-aware Pareto frontier over trusted, reviewed rounds."""
        return transitions.pareto_frontier_records(records, space)

    def best(self, records: Sequence[RoundRecord], *, space: MetricSpace) -> RoundRecord | None:
        """Select the latest noise-aware winner from trusted retained records."""
        return transitions.select_final_candidate(records, space)

    def pareto_conflict(
        self,
        *,
        disposition: CandidateDisposition,
        metrics: dict[str, float],
        records: Sequence[RoundRecord],
        space: MetricSpace,
    ) -> str | None:
        """Explain why a claimed frontier row is dominated by the live archive."""
        return transitions.pareto_archive_conflict(
            candidate_disposition=disposition,
            candidate_metrics=metrics,
            records=records,
            space=space,
        )

    def validate_updates(
        self, state: HypothesisState, updates: Sequence[HypothesisStrategyUpdate]
    ) -> None:
        """Reject a designer's parked/abandoned updates the state can't accept.

        Raises ``ValueError`` for a duplicate, unknown, active, or
        incomplete-hypothesis update; otherwise returns.
        """
        transitions.apply_strategy_updates(state.clone(), updates)

    def resolve_rollback(
        self, target: RoundRecord, records: Sequence[RoundRecord]
    ) -> tuple[str | None, int | None]:
        """Resolve the Git revision (and any failed child round) for a rollback to *target*."""
        return RoundHistory(records=list(records)).resolve_rollback_commit(
            target, FAILED_HYPOTHESIS_OUTCOMES
        )

    def update_active(self, state: HypothesisState, hypothesis: Hypothesis) -> HypothesisState:
        """Replace the active hypothesis with an updated checkpoint."""
        return transitions.update_active_hypothesis(state, hypothesis)

    def provisional_since_official(self, records: Sequence[RoundRecord]) -> int:
        """Count provisional candidates recorded since the last official evaluation."""
        return transitions.provisional_candidates_since_official(records)

    def archive_summary(self, records: Sequence[RoundRecord], *, space: MetricSpace) -> str:
        """Render the Pareto archive's current summary for the progress board."""
        return transitions.pareto_archive_summary(records, space)

    def format_metric_row(self, record: RoundRecord, *, space: MetricSpace) -> str:
        """Render one round record's candidate metrics against *space*'s objectives."""
        return transitions._format_metric_row(  # noqa: SLF001  # same package  # LW-040043 [SLF001]; this test reads one private attribute to check internal wiring that has no public accessor.
            transitions.record_candidate_metrics(record), space.objectives
        )

    @staticmethod
    def delta_reason(hypothesis: Hypothesis) -> PerfDeltaReason | None:
        """Why *hypothesis*'s headline number carries no causal delta.

        A plain function of one hypothesis (no run configuration), exposed
        as a static method so read-only projectors (no bound search policy)
        can call it without constructing one.
        """
        return transitions.measurement_delta_reason(hypothesis)


def _next_active(  # noqa: PLR0913  # LW-040044 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
    hypothesis: Hypothesis,
    *,
    keeps_active: bool,
    passed: bool,
    reviewed: bool,
    requests_continuation: bool,
    has_implementation: bool,
    next_step: str | None,
    feedback: str | None,
    max_continuation_rounds: int,
) -> Hypothesis | None:
    next_active = hypothesis.clone()
    if keeps_active:
        next_active.feedback = feedback if reviewed and not passed else None
        next_active.next_step = next_step
        next_active.continuation_rounds += 1
        return next_active
    if passed:
        return None
    if reviewed and next_active.continuation_rounds < max_continuation_rounds:
        next_active.feedback = feedback
        next_active.next_step = next_step if requests_continuation else None
        next_active.continuation_rounds += 1
        return next_active
    if reviewed or has_implementation:
        return None
    next_active.feedback = None
    next_active.next_step = next_step
    return next_active


def _carry_over(  # noqa: PLR0913  # LW-040045 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
    prior: CarryOver,
    *,
    passed: bool,
    reviewed: bool,
    record: RoundRecord,
    all_records: list[RoundRecord],
    max_retries_per_round: int,
    feedback: str | None,
    terminal_needs_parent_choice: bool,
    keeps_active: bool,
) -> tuple[CarryOver, str | None]:
    carry = CarryOver(regression_info=prior.regression_info, exhaustion_info=prior.exhaustion_info)
    exhaustion_feedback: str | None = None
    if not passed and reviewed:
        exhaustion_feedback = feedback or ""
        carry.exhaustion_info = (
            f"Round {record.round_number} did not pass after "
            f"{max_retries_per_round} attempts. Last judge feedback: {feedback or '(empty)'}"
        )
        carry.regression_info = None
    elif passed:
        carry.exhaustion_info = None
        if terminal_needs_parent_choice:
            carry.regression_info = transitions.terminal_workspace_notice(all_records)
        elif record.official_evaluation and record.candidate_retained is False:
            carry.regression_info = (
                f"Round {record.round_number}'s official candidate was not retained: "
                f"{record.perf_metric}{(' ' + record.perf_unit) if record.perf_unit else ''}. "
                "Use its recorded parent and objective directions when choosing the next checkpoint."
            )
        else:
            carry.regression_info = None
    else:
        carry.exhaustion_info = None
        carry.regression_info = (
            None if keeps_active else transitions.terminal_workspace_notice(all_records)
        )
    return carry, exhaustion_feedback
