"""What a designer is shown about the search so far: history, frontier, notices, focus.

The evidence a file-based loop kept in progress files is rendered from these typed rows, because
a round's workspace is an isolated child of a revision and holds no progress files. A strategy's
plan prompt extends `PlanEvidence` with its own fields, so the template variables stay flat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field

from vibesys.hypothesis import PlanningContext, RoundRecord, rounds
from vs_core.api import Value

if TYPE_CHECKING:
    from vibesys.hypothesis import HypothesisConfig
    from vibesys.profile_focus import FocusView

_HISTORY_ROWS = 12


class RoundRow(Value):
    """One finished round, as the designer's history shows it."""

    round_number: int = Field(gt=0)
    hypothesis_id: str
    claim: str = ""
    passed: bool
    official: bool
    judge_verdict: str
    outcome: str | None = None
    metric: float | None = None
    unit: str | None = None
    delta_pct: float | None = None
    retained: bool | None = None
    commit: str | None = None


class FrontierRow(Value):
    """A trusted point on the noise-aware Pareto frontier."""

    round_number: int = Field(gt=0)
    commit: str
    official: bool
    metrics: tuple[tuple[str, float], ...] = ()
    operating_point: str = ""


class PendingClaim(Value):
    """A retained frontier claim that is not yet a trusted parent."""

    round_number: int = Field(gt=0)
    commit: str
    metrics: tuple[tuple[str, float], ...] = ()
    reason: str = ""


class Bottleneck(Value):
    """One ranked component of the measured attribution."""

    component: str
    cost_share_pct: float
    evidence: tuple[str, ...] = ()


class LastProfile(Value):
    """The profile the designer is shown: the previous round's, or this round's fresh capture."""

    source: Literal["implementer", "profiler"] = "implementer"
    analysis: str = ""
    bottlenecks: str = ""
    suggestions: str = ""
    perf_metric: float | None = None
    perf_unit: str | None = None
    # Where the profiler wrote its bounded evidence files, when it did.
    evidence_directory: str | None = None


class PlanEvidence(Value):
    """The fields every new-hypothesis planning request carries about the search so far."""

    round_number: int = Field(gt=0)
    max_rounds: int = Field(gt=0)
    plateau_warning: str | None = None
    provisional_candidates: int = Field(default=0, ge=0)
    official_eval_every: int = Field(default=1, gt=0)
    official_cadence_due: bool = False
    benchmark_enabled: bool = True
    history: tuple[RoundRow, ...] = ()
    older_rounds: int = Field(default=0, ge=0)
    frontier: tuple[FrontierRow, ...] = ()
    pending_claims: tuple[PendingClaim, ...] = ()
    last_profile: LastProfile | None = None
    regression_round: int | None = None
    regression_kind: str | None = None
    exhaustion_feedback: str | None = None
    exhaustion_round: int | None = None
    active_component: str | None = None
    ranked_bottlenecks: tuple[Bottleneck, ...] = ()


def _row(record: RoundRecord) -> RoundRow:
    return RoundRow(
        round_number=record.round_number,
        hypothesis_id=record.hypothesis_id or "",
        claim=(record.hypothesis_claim or "")[:400],
        passed=record.passed,
        official=record.official_evaluation,
        judge_verdict=record.judge_verdict or "deferred",
        outcome=record.hypothesis_outcome,
        metric=record.perf_metric,
        unit=record.perf_unit,
        delta_pct=record.perf_delta_pct,
        retained=record.candidate_retained,
        commit=record.commit,
    )


def plan_evidence(
    config: HypothesisConfig,
    ledger: rounds.Ledger,
    planning: PlanningContext,
    focus: FocusView | None,
) -> PlanEvidence:
    """The evidence for planning ``planning.round_number``: history, frontier, notices, focus.

    ``benchmark_enabled`` and ``last_profile`` are facts of the run and of the strategy's last
    round, so the strategy sets them on the result.
    """
    records = ledger.records
    archive = rounds.archive_view(config, ledger)
    ranked = () if focus is None else focus.ranked_bottlenecks
    regression = planning.carry.regression
    exhaustion = planning.carry.exhaustion
    return PlanEvidence(
        round_number=planning.round_number,
        max_rounds=config.max_rounds,
        plateau_warning=planning.plateau_warning,
        provisional_candidates=planning.provisional_candidates,
        official_eval_every=config.official_eval_every,
        official_cadence_due=planning.provisional_candidates + 1 >= config.official_eval_every,
        history=tuple(_row(item) for item in records[-_HISTORY_ROWS:]),
        older_rounds=max(len(records) - _HISTORY_ROWS, 0),
        frontier=tuple(
            FrontierRow(
                round_number=item.round_number,
                commit=item.commit,
                official=item.official,
                metrics=tuple((m.name, m.value) for m in item.metrics),
                operating_point=item.operating_point,
            )
            for item in archive.trusted_parents
        ),
        pending_claims=tuple(
            PendingClaim(
                round_number=item.round_number,
                commit=item.commit,
                metrics=tuple((m.name, m.value) for m in item.metrics),
                reason=item.reason,
            )
            for item in archive.pending_claims
        ),
        regression_round=getattr(regression, "round_number", None),
        regression_kind=None if regression is None else type(regression).__name__,
        exhaustion_feedback=None if exhaustion is None else exhaustion.feedback,
        exhaustion_round=None if exhaustion is None else exhaustion.round_number,
        active_component=None if focus is None else (focus.active_component or None),
        ranked_bottlenecks=tuple(
            Bottleneck(
                component=item.name, cost_share_pct=item.share * 100, evidence=tuple(item.evidence)
            )
            for item in ranked
        ),
    )
