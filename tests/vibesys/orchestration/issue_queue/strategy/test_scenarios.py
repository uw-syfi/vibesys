"""Whole runs on the production shell: bootstrap, work an issue, review, evaluate, adopt."""

from __future__ import annotations

from collections import deque

from tests.vibesys.orchestration.issue_queue.strategy._executors import Executors
from tests.vibesys.orchestration.issue_queue.strategy._replies import evaluated, implemented, judged
from tests.vibesys.orchestration.issue_queue.strategy._run import kinds, run_shell

from vs_core.api import ProposeWinner, Stop


def _one_issue() -> Executors:
    return Executors(
        implementer=deque([implemented()]),
        judge=deque([judged()]),
        perf=deque([evaluated()]),
    )


def test_a_quiet_board_ends_in_success_with_the_work_adopted() -> None:
    executors = _one_issue()
    trace = run_shell(executors)
    final = trace.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"
    assert any(isinstance(item, ProposeWinner) for item in trace.decisions)
    assert kinds(trace).count("StartAttempt") == 2
