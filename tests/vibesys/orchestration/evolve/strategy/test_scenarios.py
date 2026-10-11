"""Whole evolve runs on the production shell: cold start, generations, measure and adopt."""

from __future__ import annotations

from collections import deque

from tests.vibesys.orchestration.evolve.strategy._executors import Executors
from tests.vibesys.orchestration.evolve.strategy._replies import mutated, reviewed
from tests.vibesys.orchestration.evolve.strategy._run import kinds, run_shell

from vs_core.api import ProposeWinner, Stop


def _passing(children: int) -> Executors:
    return Executors(
        mutator=deque(mutated(f"m{n}") for n in range(children)),
        judge=deque(reviewed() for _ in range(children)),
    )


def test_a_run_adopts_its_best_individual() -> None:
    trace = run_shell(_passing(5), max_generations=2, children_per_generation=2)
    # one cold start and two generations of two children
    assert kinds(trace).count("StartAttempt") == 5
    final = trace.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"
    proposal = next(item for item in trace.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"
