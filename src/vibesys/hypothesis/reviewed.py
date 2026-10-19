"""Reviewed rounds: the judge, the approved claims and the terminal policy of a multi-role round.

A round of a strategy with an independent judge records more than a self-reviewing one: the
implementer's declared outcome and continuation, the judge's verdict, the claims the framework
approved before its gates ran, and whether the gates passed. These are the pure functions over
the ledger that `vibesys.hypothesis.rounds` exposes: when a review is due, approving a reply's
claims, and closing the round. Nothing here knows about turns, workspaces or measurements; the
caller supplies plain facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.hypothesis import cadence
from vibesys.hypothesis.attempts import (
    AttemptState,
    ImplementerReply,
    JudgeOutcome,
    PerformanceProjection,
    attempt_was_reviewed,
)
from vibesys.hypothesis.history import CandidateDisposition, HypothesisOutcome
from vibesys.hypothesis.record import AgentAttribution, RecordInput, build_round_record
from vibesys.hypothesis.rounds import Ledger
from vibesys.hypothesis.search import HypothesisSearch

if TYPE_CHECKING:
    from vibesys.hypothesis.config import HypothesisConfig
    from vibesys.hypothesis.plan import OrchestratorPlan
    from vibesys.hypothesis.state import Hypothesis
    from vibesys.metrics import FrameworkBenchmarkOutcome
    from vs_core.api import EffectVerdict

_CODE_OUTCOMES = frozenset({HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED})


def claims_frontier(reply: ImplementerReply) -> bool:
    """Whether the reply claims a `pareto_frontier` checkpoint."""
    return reply.candidate_disposition is CandidateDisposition.PARETO_FRONTIER


def candidate_ready(reply: ImplementerReply) -> bool:
    """Whether the reply offers a candidate the official gates may judge."""
    return reply.hypothesis_outcome in _CODE_OUTCOMES or claims_frontier(reply)


def review_due(  # noqa: PLR0913  # LW-641103 [PLR0913]; the cadence reads independent plain facts of the round and the attempt.
    config: HypothesisConfig,
    ledger: Ledger,
    reply: ImplementerReply,
    *,
    round_number: int,
    review_started: bool,
    revalidation_required: bool,
) -> bool:
    """Whether an independent review is due after this reply (cadence, overrides)."""
    return HypothesisSearch(config).review_due(
        round_number=round_number,
        outcome=reply.hypothesis_outcome,
        candidate_evidence_is_fresh=cadence.candidate_evidence_fresh(
            candidate_metrics=reply.candidate_metrics,
            candidate_evaluation_artifact=reply.candidate_evaluation_artifact,
            records=ledger.records,
        ),
        review_started=review_started,
        requests_continuation=cadence.continuation_requested(
            outcome=reply.hypothesis_outcome, next_step=reply.next_step
        ),
        pareto_frontier_claim=claims_frontier(reply),
        revalidation_required=revalidation_required,
    )


def _with_active(config: HypothesisConfig, ledger: Ledger, updated: Hypothesis) -> Ledger:
    return Ledger(HypothesisSearch(config).update_active(ledger.search, updated), ledger.carry)


def approve_candidate(config: HypothesisConfig, ledger: Ledger, reply: ImplementerReply) -> Ledger:
    """Keep the approved frontier claim on the active hypothesis for a gate retry."""
    hypothesis = ledger.search.active_hypothesis
    if hypothesis is None or not claims_frontier(reply):
        return ledger
    updated = hypothesis.clone()
    updated.gate_approved_candidate_disposition = reply.candidate_disposition.value
    updated.gate_approved_candidate_metrics = dict(reply.candidate_metrics)
    updated.gate_approved_candidate_evaluation_artifact = reply.candidate_evaluation_artifact
    updated.gate_approved_candidate_operating_point = reply.candidate_operating_point
    updated.gate_approved_candidate_retention_reason = reply.candidate_retention_reason
    return _with_active(config, ledger, updated)


def approve_performance(
    config: HypothesisConfig, ledger: Ledger, reply: ImplementerReply
) -> Ledger:
    """Keep the approved performance claim on the active hypothesis for a gate retry."""
    hypothesis = ledger.search.active_hypothesis
    if hypothesis is None or reply.perf_metric is None:
        return ledger
    updated = hypothesis.clone()
    updated.gate_approved_perf_metric = reply.perf_metric
    updated.gate_approved_perf_unit = reply.perf_unit
    updated.gate_approved_metrics = dict(reply.metrics)
    updated.gate_approved_evaluation_artifact = reply.evaluation_artifact
    return _with_active(config, ledger, updated)


@dataclass(frozen=True)
class ReviewedClosing:
    """The plain facts that close one reviewed round."""

    round_number: int
    plan: OrchestratorPlan
    reply: ImplementerReply | None
    """The last valid implementer reply, or None when no attempt produced one."""
    judge: JudgeOutcome
    passed: bool
    feedback: str | None
    official_reason: str | None
    benchmark: FrameworkBenchmarkOutcome
    benchmark_metric: float | None
    candidate_commit: str | None
    attempts: int
    accuracy_configured: bool
    benchmark_configured: bool
    implementer: AgentAttribution | None
    revalidation_required: bool = False
    evaluated_only: bool = False
    """The gates measured a revision the agent evaluated, with no ready candidate (`AttemptState`)."""
    effect: EffectVerdict | None = None
    """The framework's verdict on the plan's typed bar, derived from its own comparison."""


def _projection(hypothesis: Hypothesis, state: AttemptState) -> PerformanceProjection:
    reply = state.implementation
    official = (state.passed or state.evaluated_only) and state.official_reason is not None
    implementation_metric = reply.perf_metric if reply is not None and official else None
    if (
        implementation_metric is None
        and official
        and reply is not None
        and reply.hypothesis_outcome in _CODE_OUTCOMES
        and hypothesis.gate_revalidation_pending
    ):
        implementation_metric = hypothesis.gate_approved_perf_metric
    profile_skipped = state.framework_perf_metric is None and implementation_metric is None
    if state.framework_perf_metric is not None and official:
        return PerformanceProjection(
            metric=state.framework_perf_metric,
            unit=state.framework_benchmark.metric_name,
            provenance="framework",
            profile_skipped=profile_skipped,
            accepted_metrics={},
            accepted_evaluation_artifact=None,
            next_single_response=None,
        )
    if implementation_metric is None:
        return PerformanceProjection(None, None, None, profile_skipped, {}, None, None)
    if reply is not None and reply.perf_metric is not None:
        return PerformanceProjection(
            implementation_metric,
            reply.perf_unit,
            "implementer",
            profile_skipped,
            dict(reply.metrics),
            reply.evaluation_artifact,
            None,
        )
    return PerformanceProjection(
        implementation_metric,
        hypothesis.gate_approved_perf_unit,
        "implementer",
        profile_skipped,
        dict(hypothesis.gate_approved_metrics),
        hypothesis.gate_approved_evaluation_artifact,
        None,
    )


def keeps_active(
    config: HypothesisConfig, hypothesis: Hypothesis, reply: ImplementerReply | None
) -> bool:
    """Whether the implementer's bounded continuation lease keeps the hypothesis active."""
    return cadence.keeps_hypothesis_active(
        outcome=None if reply is None else reply.hypothesis_outcome,
        next_step="" if reply is None else reply.next_step,
        continuation_rounds=hypothesis.continuation_rounds,
        max_continuation_rounds=config.max_continuation_rounds,
    )


def close_reviewed_round(
    config: HypothesisConfig, ledger: Ledger, facts: ReviewedClosing
) -> Ledger:
    """Record the round's `RoundRecord` and choose the next designer handoff."""
    hypothesis = ledger.search.active_hypothesis
    if hypothesis is None:
        message = "no active hypothesis to close"
        raise ValueError(message)
    reply = facts.reply
    state = AttemptState(
        agent_run_state=ledger.search,
        feedback=facts.feedback,
        implementation=reply,
        judge=facts.judge,
        passed=facts.passed,
        revalidation_required=facts.revalidation_required,
        official_reason=facts.official_reason,
        evaluated_only=facts.evaluated_only,
        framework_benchmark=facts.benchmark,
        framework_perf_metric=facts.benchmark_metric,
        retry=facts.attempts,
    )
    reviewed = attempt_was_reviewed(facts.judge)
    record = build_round_record(
        RecordInput(
            state=ledger.search,
            records=ledger.records,
            round_number=facts.round_number,
            hypothesis=hypothesis,
            plan=facts.plan,
            attempt=state,
            projection=_projection(hypothesis, state),
            reviewed=reviewed,
            framework_benchmark_configured=facts.benchmark_configured,
            accuracy_configured=facts.accuracy_configured,
            candidate_commit=facts.candidate_commit,
            implementer=facts.implementer,
            effect=facts.effect,
        )
    )
    keeps = keeps_active(config, hypothesis, reply)
    continuation = reply is not None and cadence.continuation_requested(
        outcome=reply.hypothesis_outcome, next_step=reply.next_step
    )
    closed = HypothesisSearch(config).close_round(
        ledger.search,
        hypothesis=hypothesis,
        record=record,
        records=ledger.records,
        passed=facts.passed,
        reviewed=reviewed,
        feedback=facts.feedback,
        keeps_active=keeps,
        requests_continuation=continuation,
        next_step=None if reply is None else reply.next_step,
        terminal_needs_parent_choice=(
            reply is not None
            and not keeps
            and reply.hypothesis_outcome is not HypothesisOutcome.NOMINATED
        ),
        has_implementation=reply is not None,
    )
    return Ledger(closed.state, closed.carry)


__all__ = [
    "ReviewedClosing",
    "approve_candidate",
    "approve_performance",
    "candidate_ready",
    "claims_frontier",
    "close_reviewed_round",
    "keeps_active",
    "review_due",
]
