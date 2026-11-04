"""The serial hypothesis round machine: the glue between a strategy and `HypothesisSearch`.

`HypothesisSearch` and the round-record builder are pure but take mutable objects. A strategy on
the core lifecycle keeps its state as frozen values, so it stores the ledger as two JSON strings
(`search_json`, `carry_json`) and calls the functions below, which decode them, apply one
transition and return the encoded result. Every function is a pure function of its arguments,
so a replay after a crash repeats it.

What it owns: choosing the next round (finished, continue, new hypothesis), starting a
hypothesis on its base, when the trusted gates measure, the Pareto
guard on a verdict, noting a failed attempt on the active hypothesis, and closing a round into a
`RoundRecord`. What it does not own: turns, measurements or any workspace operation; the caller
supplies plain facts (`ClosingFacts`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from vibesys.hypothesis import cadence
from vibesys.hypothesis.attempts import (
    AttemptState,
    CandidateReply,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    PerformanceProjection,
    SingleAgentReply,
)
from vibesys.hypothesis.config import HypothesisConfig
from vibesys.hypothesis.record import AgentAttribution, RecordInput, build_round_record
from vibesys.hypothesis.results import Finished
from vibesys.hypothesis.search import HypothesisSearch
from vibesys.hypothesis.state import HypothesisState
from vibesys.hypothesis.transitions import CarryOver

if TYPE_CHECKING:
    from vibesys.hypothesis.history import RoundRecord
    from vibesys.hypothesis.notices import ArchiveConflict, ParetoArchiveView
    from vibesys.hypothesis.plan import OrchestratorPlan
    from vibesys.hypothesis.results import NextRoundDecision, PlanningContext, StartedHypothesis
    from vibesys.metrics import FrameworkBenchmarkOutcome, MetricSpace

_CARRY = TypeAdapter(CarryOver)


@dataclass(frozen=True)
class Ledger:
    """The decoded hypothesis ledger: every hypothesis and round, and the next turn's carry-over."""

    search: HypothesisState
    carry: CarryOver = field(default_factory=CarryOver)

    @property
    def records(self) -> list[RoundRecord]:
        """Completed round records in order."""
        return self.search.rounds


@dataclass(frozen=True)
class EncodedLedger:
    """The ledger as the strategy state stores it."""

    search_json: str = ""
    carry_json: str = ""


def decode(encoded: EncodedLedger, space: MetricSpace | None = None) -> Ledger:
    """The ledger of ``encoded``, projected onto the run's metric space when one is given.

    Empty text is a new run. A read-only view (the run's projection) passes no space and sees
    the ledger exactly as it was saved.
    """
    search = (
        HypothesisState.model_validate_json(encoded.search_json)
        if encoded.search_json
        else HypothesisState()
    )
    if space is not None:
        search = HypothesisSearch(HypothesisConfig(max_rounds=1)).resume(search, space)
    carry = _CARRY.validate_json(encoded.carry_json) if encoded.carry_json else CarryOver()
    return Ledger(search, carry)


def encode(ledger: Ledger) -> EncodedLedger:
    """The ledger's persisted form."""
    return EncodedLedger(
        search_json=ledger.search.model_dump_json(),
        carry_json=_CARRY.dump_json(ledger.carry).decode(),
    )


def next_round(config: HypothesisConfig, ledger: Ledger, round_number: int) -> NextRoundDecision:
    """Finished, continue the active hypothesis, or plan a new one."""
    return HypothesisSearch(config).next_round(
        ledger.search, round_number=round_number, records=ledger.records, carry=ledger.carry
    )


@dataclass(frozen=True)
class Parent:
    """The base a hypothesis starts from, as the ledger records it."""

    round_number: int | None
    """The round of the idea that made the base; None when the base is the input."""
    commit: str | None


def start(
    config: HypothesisConfig,
    ledger: Ledger,
    plan: OrchestratorPlan,
    *,
    round_number: int,
    parent: Parent,
) -> tuple[Ledger, StartedHypothesis]:
    """Start the designer's hypothesis on the base the caller resolved."""
    started = HypothesisSearch(config).start(
        ledger.search,
        plan,
        round_number=round_number,
        parent_round=parent.round_number,
        parent_commit=parent.commit,
    )
    return Ledger(started.state, ledger.carry), started


def validate_plan(config: HypothesisConfig, ledger: Ledger, updates: list) -> None:  # type: ignore[type-arg]
    """Raise `ValueError` for strategy updates the ledger cannot accept."""
    HypothesisSearch(config).validate_updates(ledger.search, updates)


def official_reason(*, candidate_ready: bool = True) -> str | None:
    """Why the trusted gates measure the reviewed candidate, or None when it is not ready."""
    return cadence.official_due(candidate_ready=candidate_ready)


def evaluated_reason() -> str:
    """Why the trusted gates measure a candidate its implementer left unsettled."""
    return cadence.MEASURED_AFTER_EVALUATION


def pareto_conflict(
    config: HypothesisConfig, ledger: Ledger, reply: CandidateReply
) -> ArchiveConflict | None:
    """The live archive points that dominate the reply's claimed frontier row, if any."""
    return HypothesisSearch(config).pareto_conflict(
        disposition=reply.candidate_disposition,
        metrics=dict(reply.candidate_metrics),
        records=ledger.records,
        space=ledger.search.metrics,
    )


@dataclass(frozen=True)
class ClosingFacts:
    """The plain facts that close one round."""

    round_number: int
    plan: OrchestratorPlan
    reply: SingleAgentReply | None
    """The last valid reply, or None when no attempt produced one."""
    passed: bool
    """Every check the attempts reached passed, and the candidate was reviewed."""
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
    verdict_pass: bool | None = None
    profile_guidance: object | None = None
    """The profile-guidance state after this round, when the run is profile-guided."""


def close_round(config: HypothesisConfig, ledger: Ledger, facts: ClosingFacts) -> Ledger:
    """Record the round's `RoundRecord` and choose the next designer handoff."""
    search = HypothesisSearch(config)
    hypothesis = ledger.search.active_hypothesis
    if hypothesis is None:
        message = "no active hypothesis to close"
        raise ValueError(message)
    reply = facts.reply
    official = facts.passed and facts.official_reason is not None
    metric = facts.benchmark_metric if official else None
    unit = facts.benchmark.metric_name if official else None
    attempt = AttemptState(
        agent_run_state=ledger.search,
        feedback=facts.feedback,
        single_agent_response=reply,
        judge=(
            JudgeSkipped(JudgeSkipReason.UNPARSEABLE_IMPLEMENTATION)
            if reply is None
            else JudgeReviewed("pass" if facts.verdict_pass else "fail")
        ),
        passed=facts.passed,
        revalidation_required=facts.revalidation_required,
        official_reason=facts.official_reason,
        framework_benchmark=facts.benchmark,
        framework_perf_metric=facts.benchmark_metric,
        retry=facts.attempts,
    )
    projection = PerformanceProjection(
        metric=metric,
        unit=unit,
        provenance=(
            "framework"
            if metric is not None and facts.benchmark_metric is not None
            else "implementer"
            if metric is not None
            else None
        ),
        profile_skipped=reply is None or reply.perf_metric is None,
        accepted_metrics={},
        accepted_evaluation_artifact=None,
        next_single_response=reply,
    )
    record = build_round_record(
        RecordInput(
            state=ledger.search,
            records=ledger.records,
            round_number=facts.round_number,
            hypothesis=hypothesis,
            plan=facts.plan,
            attempt=attempt,
            projection=projection,
            reviewed=True,
            framework_benchmark_configured=facts.benchmark_configured,
            accuracy_configured=facts.accuracy_configured,
            candidate_commit=facts.candidate_commit,
            implementer=facts.implementer,
        )
    )
    state = ledger.search
    if facts.profile_guidance is not None:
        state = state.model_copy(update={"profile_guidance": facts.profile_guidance}, deep=True)
    closed = search.close_round(
        state,
        hypothesis=hypothesis,
        record=record,
        records=ledger.records,
        passed=facts.passed,
        reviewed=True,
        feedback=facts.feedback,
        keeps_active=False,
        requests_continuation=False,
        next_step=None,
        terminal_needs_parent_choice=False,
    )
    return Ledger(closed.state, closed.carry)


@dataclass(frozen=True)
class GateFailure:
    """A trusted gate rejected the candidate, so the hypothesis must be revalidated."""

    candidate_commit: str | None
    accuracy_passed: bool


def note_attempt_failure(
    config: HypothesisConfig, ledger: Ledger, feedback: str, gate: GateFailure | None = None
) -> Ledger:
    """Keep a failed attempt's feedback on the active hypothesis for the next attempt.

    A failure of the trusted gates also marks the hypothesis as pending revalidation.
    """
    hypothesis = ledger.search.active_hypothesis
    if hypothesis is None:
        message = "no active hypothesis to checkpoint"
        raise ValueError(message)
    updated = hypothesis.clone()
    updated.feedback = feedback
    if gate is not None:
        updated.gate_revalidation_pending = True
        updated.gate_candidate_commit = gate.candidate_commit
        updated.gate_accuracy_passed = gate.accuracy_passed
    state = HypothesisSearch(config).update_active(ledger.search, updated)
    return Ledger(state, ledger.carry)


def archive_view(config: HypothesisConfig, ledger: Ledger) -> ParetoArchiveView:
    """The Pareto archive's trusted parents and pending claims over the ledger's rounds."""
    return HypothesisSearch(config).archive_view(ledger.records, space=ledger.search.metrics)


def planning(config: HypothesisConfig, ledger: Ledger, round_number: int) -> PlanningContext:
    """The evidence a designer turn needs for ``round_number``: plateau, evaluations spent, carry."""
    decision = next_round(config, ledger, round_number)
    if isinstance(decision, Finished):
        message = "no round to plan past the round budget"
        raise TypeError(message)
    return decision.context
