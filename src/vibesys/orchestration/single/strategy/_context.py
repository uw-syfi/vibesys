"""Prompt data and bookkeeping derived from state, the run view and config.

Everything here is a pure function of `SingleStrategyState`, `RunView` and `SingleConfig`.
Prompt contexts are typed values; the strategy never writes prompt text.
"""

from vibesys.candidates.api import BaselineStage
from vibesys.hypothesis import OrchestratorPlan, PlanningContext, RoundRecord, rounds
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vibesys.orchestration.single.strategy._config import SingleConfig
from vibesys.orchestration.single.strategy._prompts import (
    Bottleneck,
    FrontierRow,
    LastProfile,
    PendingClaim,
    PlanPrompt,
    RoundPrompt,
    RoundRow,
)
from vibesys.orchestration.single.strategy._state import RoundState, SingleStrategyState
from vibesys.profile_focus import FocusView
from vs_core.api import RevisionRef, RunView

_HISTORY_ROWS = 12


def ledger_of(state: SingleStrategyState, config: SingleConfig) -> rounds.Ledger:
    """The decoded hypothesis ledger, projected onto the run's metric space."""
    return rounds.decode(
        rounds.EncodedLedger(search_json=state.search_json, carry_json=state.carry_json),
        config.metric_space,
    )


def baseline_resolved(state: SingleStrategyState) -> bool:
    """Whether the input measurement has reached a terminal stage."""
    return state.baseline.stage in {
        BaselineStage.MEASURED,
        BaselineStage.UNMEASURABLE,
        BaselineStage.NOT_CONFIGURED,
    }


def input_trusted(state: SingleStrategyState) -> bool:
    """Whether the input revision has a trusted reading, or no gate is configured to give one."""
    return (
        state.baseline.stage in {BaselineStage.MEASURED, BaselineStage.NOT_CONFIGURED}
        and state.baseline.accuracy_passed is not False
    )


def build_base(state: SingleStrategyState, view: RunView) -> RevisionRef:
    """The newest revision a later round may build on: a retained checkpoint, else the input."""
    return state.checkpoint or view.facts.baseline


def commit_ref(commit: str | None, fallback: RevisionRef) -> RevisionRef:
    """The revision a recorded commit names, else ``fallback``."""
    return fallback if not commit else RevisionRef.of_git_commit(commit)


def plan_of(round_state: RoundState) -> OrchestratorPlan:
    """The designer's validated plan of the open round."""
    return OrchestratorPlan.model_validate_json(round_state.plan_json)


def response_of(round_state: RoundState) -> SingleAgentRoundResponse | None:
    """The last valid combined reply of the open round, if any."""
    if round_state.response_json is None:
        return None
    return SingleAgentRoundResponse.model_validate_json(round_state.response_json)


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


def plan_prompt(
    state: SingleStrategyState,
    config: SingleConfig,
    view: RunView,
    planning: PlanningContext,
    focus: FocusView | None = None,
) -> PlanPrompt:
    """The new-hypothesis request: history, the frontier, carried notices and the last profile."""
    ledger = ledger_of(state, config)
    records = ledger.records
    archive = rounds.archive_view(config.hypotheses, ledger)
    ranked = () if focus is None else focus.ranked_bottlenecks
    regression = planning.carry.regression
    exhaustion = planning.carry.exhaustion
    return PlanPrompt(
        round_number=planning.round_number,
        max_rounds=config.max_rounds,
        plateau_warning=planning.plateau_warning,
        provisional_candidates=planning.provisional_candidates,
        official_eval_every=config.official_eval_every,
        official_cadence_due=planning.provisional_candidates + 1 >= config.official_eval_every,
        benchmark_enabled=config.benchmark_configured,
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
        last_profile=state.last_profile,
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
        base_revision=build_base(state, view),
    )


def last_profile_of(response: SingleAgentRoundResponse | None) -> LastProfile | None:
    """The profile fields the implementer reported, for the next designer prompt."""
    if response is None:
        return None
    return LastProfile(
        analysis=response.profile_analysis,
        bottlenecks=response.bottlenecks,
        suggestions=response.suggestions,
        perf_metric=response.perf_metric,
        perf_unit=response.perf_unit,
    )


def round_prompt(round_state: RoundState, config: SingleConfig) -> RoundPrompt:
    """The implementation, self-review and profile request of the open round."""
    plan = plan_of(round_state)
    return RoundPrompt(
        round_number=round_state.number,
        retry=round_state.turns_spent + 1,
        hypothesis_id=plan.hypothesis_id,
        title=plan.title,
        hypothesis=plan.hypothesis,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
        invariants=plan.invariants,
        activation_evidence=plan.activation_evidence,
        falsification_criteria=plan.falsification_criteria,
        minimum_acceptance_criteria=plan.minimum_acceptance_criteria,
        base_revision=round_state.base,
        feedback=round_state.feedback,
        no_valid_reply=round_state.no_valid_reply,
        dominated_by_rounds=round_state.dominated_by_rounds,
        official_due=round_state.official_reason is not None,
        official_reason=round_state.official_reason,
        interface=config.interface,
        profiler_id=config.profiler_id,
    )


__all__ = [
    "Bottleneck",
    "baseline_resolved",
    "build_base",
    "commit_ref",
    "input_trusted",
    "last_profile_of",
    "ledger_of",
    "plan_of",
    "plan_prompt",
    "response_of",
    "round_prompt",
]
