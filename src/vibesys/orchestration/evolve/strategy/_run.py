"""Evolve's run phases: when the search is over, over the shared candidate policy.

What is chosen, proposed and stopped with is `vibesys.candidates`. This module owns what only the
strategy knows: that the best individual by the metric space is the candidate, and that a run
whose cold starts never produced a passing individual ends with no selection, as the legacy loop
returned `FAILED`.
"""

from vibesys.candidates import api as candidates
from vibesys.candidates.api import RunPhase
from vibesys.hypothesis import RunEnding
from vibesys.orchestration.evolve.strategy import _generation as generation
from vibesys.orchestration.evolve.strategy import _population as population
from vibesys.orchestration.evolve.strategy._config import EvolveConfig
from vibesys.orchestration.evolve.strategy._draft import Draft
from vibesys.orchestration.evolve.strategy._ids import IDS
from vibesys.orchestration.evolve.strategy._state import EvolveStrategyState
from vs_core.api import AdoptionFailed, AdoptionResult


def decide(draft: Draft) -> None:
    """Close the search, propose the winner, then stop after adoption."""
    if generation.search_over(draft.state, draft.config):
        draft.update(phase=RunPhase.SELECTING)
    state = draft.state
    if state.phase is RunPhase.SELECTING:
        _propose(draft)
    elif state.phase is RunPhase.STOPPING:
        _stop(draft)


def _propose(draft: Draft) -> None:
    state = draft.state
    winner = population.winner_of(state, draft.config)
    if winner is None:
        draft.update(phase=RunPhase.STOPPING, winner=None)
        _stop(draft)
        return
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
            no_winner_reason=_no_winner_reason(state, draft.config),
        )
    )
    draft.update(phase=RunPhase.FINISHED)


def _no_winner_reason(state: EvolveStrategyState, config: EvolveConfig) -> str:
    if state.winner is None and state.population is not None:
        passing = population.search_of(config).best(state.population)
        if passing is not None:
            return "the best individual has no retained settlement to adopt"
    return candidates.ending_reason(
        RunEnding.NO_TRUSTED_RESULT,
        f"no cold start passed review and the configured gates in {config.bootstrap_max_attempts}"
        " attempts",
    )


def on_adoption(state: EvolveStrategyState, event: AdoptionResult) -> EvolveStrategyState:
    """Stop after a successful adoption."""
    phase, winner = candidates.after_adoption(state.phase, state.winner, event)
    if phase is state.phase and winner is state.winner:
        return state
    return state.model_copy(update={"phase": phase, "winner": winner})


def on_adoption_failed(state: EvolveStrategyState, event: AdoptionFailed) -> EvolveStrategyState:
    """Stop without a winner when core gave up on the adoption."""
    del event
    phase, winner = candidates.after_failed_adoption(state.phase, state.winner)
    if phase is state.phase and winner is state.winner:
        return state
    return state.model_copy(update={"phase": phase, "winner": winner})
