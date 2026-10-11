"""A multi run's public projection is its hypothesis ledger, read from the strategy state."""

from __future__ import annotations

from tests.vibesys.orchestration.test_projection import _search_state

from vibesys.hypothesis import rounds
from vibesys.hypothesis.readmodel import project_hypothesis_state
from vibesys.orchestration.multi.core_policy.api import project_strategy_state
from vibesys.orchestration.multi.strategy.api import MultiStrategyState


def test_the_projection_of_the_strategy_state_is_the_projection_of_its_ledger() -> None:
    search = _search_state()
    encoded = rounds.encode(rounds.Ledger(search))
    state = MultiStrategyState(search_json=encoded.search_json, carry_json=encoded.carry_json)

    assert project_strategy_state(state, experiment_revision=3) == project_hypothesis_state(search)


def test_a_run_that_has_not_planned_yet_projects_an_empty_ledger() -> None:
    projection = project_strategy_state(MultiStrategyState(), experiment_revision=0)

    assert projection.rounds == ()
