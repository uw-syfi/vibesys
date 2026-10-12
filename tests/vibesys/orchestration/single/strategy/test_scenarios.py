"""Whole single runs on the production shell: baseline, design, implement, measure, adopt."""

from __future__ import annotations

from tests.vibesys.orchestration.single.strategy.world import SingleWorld

from vs_core.api import ProposeWinner, RequestTurn, StartAttempt, Stop
from vs_dst.api import simulate


def test_two_rounds_run_to_adoption() -> None:
    run = simulate(SingleWorld())

    assert run.finished
    assert not run.liveness_violations
    assert sum(isinstance(item, StartAttempt) for item in run.decisions) == 2
    final = run.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"
    proposal = next(item for item in run.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"
    assert any(isinstance(item, RequestTurn) for item in run.decisions)
