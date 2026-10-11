"""Whole single runs on the production shell: baseline, design, implement, measure, adopt."""

from __future__ import annotations

from collections import deque

from tests.vibesys.orchestration.single.strategy._executors import Executors
from tests.vibesys.orchestration.single.strategy._replies import combined, plan_reply
from tests.vibesys.orchestration.single.strategy._run import kinds, run_shell

from vs_core.api import ProposeWinner, RequestTurn, StartAttempt, Stop


def _two_rounds() -> Executors:
    return Executors(
        planner=deque([plan_reply("h1"), plan_reply("h2")]),
        implementer=deque([combined(), combined()]),
    )


def test_two_rounds_run_to_adoption() -> None:
    trace = run_shell(_two_rounds())
    assert kinds(trace).count("StartAttempt") == 2
    final = trace.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"
    proposal = next(item for item in trace.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"
    assert any(isinstance(item, StartAttempt) for item in trace.decisions)
    assert any(isinstance(item, RequestTurn) for item in trace.decisions)
