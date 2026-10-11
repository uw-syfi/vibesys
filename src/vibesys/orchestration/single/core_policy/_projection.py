"""Read model of a single run: the hypothesis ledger in the shape frontends already consume."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.hypothesis import rounds
from vibesys.hypothesis.readmodel import project_hypothesis_state

if TYPE_CHECKING:
    from vibesys.orchestration.single.strategy.api import SingleStrategyState
    from vibesys.run.contracts import PluginProjection


def project_strategy_state(
    state: SingleStrategyState, *, experiment_revision: int
) -> PluginProjection:
    """Project one strategy state; `experiment_revision` is the core revision it was read at."""
    del experiment_revision
    ledger = rounds.decode(
        rounds.EncodedLedger(search_json=state.search_json, carry_json=state.carry_json)
    )
    return project_hypothesis_state(ledger.search)
