"""Run-level policy: when the search is over, which candidate wins, and how it ends.

The winner is the best winner-eligible retained candidate by the configured metric
space, else the trusted baseline. Adoption is proposed once, and the run stops
only after core reports the adoption result.
"""

from vibesys.orchestration.dynamic.strategy import _context as context
from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._draft import Draft, run_scope
from vibesys.orchestration.dynamic.strategy._ids import decision_id
from vibesys.orchestration.dynamic.strategy._settlement import measurement
from vibesys.orchestration.dynamic.strategy._state import (
    DynamicStrategyState,
    HypothesisRecord,
    RoundRecord,
    RunPhase,
    Winner,
)
from vs_core.api import (
    AdoptionResult,
    ObservationStatus,
    ProposeWinner,
    RetainedCandidate,
    RevisionRef,
    RunResultProposal,
    Selection,
    Stop,
    TrustedBaseline,
)


def search_over(state: DynamicStrategyState, config: DynamicConfig) -> bool:
    """Whether nothing is in flight and nothing more will be scheduled."""
    if state.phase is not RunPhase.SEARCHING or context.active(state) or state.planner.active:
        return False
    return (
        state.stopping
        or state.planner.failed
        or (context.baseline_resolved(state) and context.remaining(state, config) == 0)
    )


def _eligible(state: DynamicStrategyState) -> tuple[tuple[HypothesisRecord, RoundRecord], ...]:
    return tuple(
        (hypothesis, row)
        for hypothesis in state.hypotheses
        for row in hypothesis.rounds
        if row.eligible and row.candidate is not None and row.settlement is not None
    )


def choose_winner(
    state: DynamicStrategyState, config: DynamicConfig, baseline: RevisionRef
) -> Winner:
    """The best eligible candidate by the metric space; earlier rounds win ties."""
    points = sorted(_eligible(state), key=lambda item: item[1].sequence)
    best = config.metric_space.best(
        points, lambda item: measurement(item[1].metrics[0] if item[1].metrics else None)
    )
    if best is None and points and not any(row.metrics for _, row in points):
        best = points[-1]  # no benchmark configured: the latest eligible candidate
    if best is None:
        return Winner(settlement=None, revision=baseline, hypothesis_id=None)
    hypothesis, row = best
    if row.candidate is None:
        return Winner(settlement=None, revision=baseline, hypothesis_id=None)
    return Winner(
        settlement=row.settlement, revision=row.candidate, hypothesis_id=hypothesis.hypothesis_id
    )


def selection_of(winner: Winner) -> Selection:
    """The core selection a winner proposes for adoption."""
    if winner.settlement is None:
        return TrustedBaseline(revision=winner.revision)
    return RetainedCandidate(settlement_id=winner.settlement, revision=winner.revision)


def decide(draft: Draft) -> None:
    """Close the search, propose the winner, then stop after adoption."""
    state = draft.state
    if search_over(state, draft.config):
        draft.update(phase=RunPhase.SELECTING)
        state = draft.state
    if state.phase is RunPhase.SELECTING:
        _propose(draft)
    elif state.phase is RunPhase.STOPPING:
        _stop(draft)


def _propose(draft: Draft) -> None:
    state = draft.state
    if state.planner.failed and not _eligible(state):
        draft.update(phase=RunPhase.STOPPING, winner=None)
        _stop(draft)
        return
    winner = choose_winner(state, draft.config, draft.view.facts.baseline)
    draft.emit(
        ProposeWinner(
            decision_id=decision_id("propose", "run"),
            scope=run_scope(draft.view),
            selection=selection_of(winner),
        )
    )
    draft.update(phase=RunPhase.ADOPTING, winner=winner)


def _stop(draft: Draft) -> None:
    state = draft.state
    winner = state.winner
    if winner is None:
        result = RunResultProposal(
            outcome="failure",
            reason=state.planner.last_error or "no candidate could be adopted",
        )
    else:
        label = "the trusted baseline" if winner.hypothesis_id is None else winner.hypothesis_id
        result = RunResultProposal(
            outcome="cancelled" if state.stopping else "success",
            reason=f"adopted {label}",
            selection=selection_of(winner),
        )
    draft.emit(
        Stop(
            decision_id=decision_id("stop", "run"),
            scope=run_scope(draft.view),
            mode="drain",
            result=result,
        )
    )
    draft.update(phase=RunPhase.FINISHED)


def on_adoption(state: DynamicStrategyState, event: AdoptionResult) -> DynamicStrategyState:
    """Stop after a successful adoption; a failed one stops without a winner."""
    if state.phase is not RunPhase.ADOPTING:
        return state
    if event.observation.status is ObservationStatus.SUCCEEDED:
        return state.model_copy(update={"phase": RunPhase.STOPPING})
    return state.model_copy(update={"phase": RunPhase.STOPPING, "winner": None})
