"""Dynamic's run phases: when the search is over, over the shared candidate policy.

What is chosen, proposed and stopped with is `vibesys.candidates`. This module owns what only
the strategy knows: when nothing is in flight and nothing more will be scheduled, and which
finished rounds are eligible.
"""

from vibesys.candidates import api as candidates
from vibesys.hypothesis import RunEnding
from vibesys.orchestration.dynamic.strategy import _context as context
from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._draft import Draft
from vibesys.orchestration.dynamic.strategy._ids import IDS
from vibesys.orchestration.dynamic.strategy._state import DynamicStrategyState, RunPhase, Winner
from vs_core.api import AdoptionResult, RevisionRef


def search_over(state: DynamicStrategyState, config: DynamicConfig) -> bool:
    """Whether nothing is in flight and nothing more will be scheduled."""
    if state.phase is not RunPhase.SEARCHING or context.active(state) or state.planner.active:
        return False
    return (
        state.stopping
        or state.planner.failed
        or (context.baseline_resolved(state) and context.remaining(state, config) == 0)
    )


def _eligible(state: DynamicStrategyState) -> tuple[candidates.EligibleRound, ...]:
    return tuple(
        candidates.EligibleRound(
            hypothesis_id=hypothesis.hypothesis_id,
            sequence=row.sequence,
            settlement=row.settlement,
            candidate=row.candidate,
            metrics=row.metrics,
        )
        for hypothesis in state.hypotheses
        for row in hypothesis.rounds
        if row.eligible and row.candidate is not None and row.settlement is not None
    )


def choose_winner(
    state: DynamicStrategyState, config: DynamicConfig, baseline: RevisionRef
) -> Winner:
    """The best eligible candidate by the metric space; earlier rounds win ties."""
    return candidates.choose_winner(_eligible(state), config, baseline)


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
    nothing_to_keep = not _eligible(state) and not context.input_trusted(state)
    if (state.planner.failed and not _eligible(state)) or nothing_to_keep:
        draft.update(phase=RunPhase.STOPPING, winner=None)
        _stop(draft)
        return
    winner = choose_winner(state, draft.config, draft.view.facts.baseline)
    draft.emit(candidates.proposal(IDS, draft.view, winner))
    draft.update(phase=RunPhase.ADOPTING, winner=winner)


def _stop(draft: Draft) -> None:
    state = draft.state
    draft.emit(
        candidates.stop(
            IDS,
            draft.view,
            state.winner,
            stopping=state.stopping,
            no_winner_reason=_no_winner_reason(state),
        )
    )
    draft.update(phase=RunPhase.FINISHED)


def _no_winner_reason(state: DynamicStrategyState) -> str:
    if context.input_trusted(state) or _eligible(state):
        return state.planner.last_error or "no candidate could be adopted"
    detail = state.baseline.failure or "the input did not pass its measurement"
    if state.planner.last_error:
        detail = f"{detail}; {state.planner.last_error}"
    return candidates.ending_reason(RunEnding.NO_TRUSTED_RESULT, detail)


def on_adoption(state: DynamicStrategyState, event: AdoptionResult) -> DynamicStrategyState:
    """Stop after a successful adoption; a failed one stops without a winner."""
    phase, winner = candidates.after_adoption(state.phase, state.winner, event)
    if phase is state.phase and winner is state.winner:
        return state
    return state.model_copy(update={"phase": phase, "winner": winner})
