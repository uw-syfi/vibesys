"""Whole runs on the production shell: bootstrap, work an issue, review, evaluate, adopt.

Each run goes through `vs_runtime`'s real run loop and shell, with only the executors
scripted, so what core is handed is what production hands it. The board is a real owner over a
fake tracker, so a test reads the outcome off the board as well as off the decisions.
"""

from __future__ import annotations

from collections import deque

from tests.vibesys.orchestration.issue_queue.strategy._executors import Executors
from tests.vibesys.orchestration.issue_queue.strategy._replies import evaluated, implemented, judged
from tests.vibesys.orchestration.issue_queue.strategy._run import kinds, run_shell

from vs_core.api import ProposeWinner, RequestTurn, Stop
from vs_issue_tracker.api import IssueStatus


def _board(executors: Executors) -> dict[int, tuple[str, int]]:
    return {issue.id: (issue.status.value, issue.attempts) for issue in executors.tracker.list()}


def _titles(executors: Executors) -> list[str]:
    return [issue.title for issue in executors.tracker.list()]


def _one_issue() -> Executors:
    return Executors(
        implementer=deque([implemented()]),
        judge=deque([judged()]),
        perf=deque([evaluated()]),
    )


def _stop(trace: object) -> Stop:
    final = trace.decisions[-1]  # type: ignore[attr-defined]
    assert isinstance(final, Stop)
    return final


def test_a_quiet_board_ends_in_success_with_the_work_adopted() -> None:
    executors = _one_issue()
    trace = run_shell(executors)
    assert _stop(trace).result.outcome == "success"
    assert any(isinstance(item, ProposeWinner) for item in trace.decisions)
    assert kinds(trace).count("StartAttempt") == 2  # the issue, then the evaluation
    assert _board(executors) == {1: ("closed", 1)}


def test_a_failed_review_is_the_next_paid_turn_of_the_same_attempt() -> None:
    executors = Executors(
        implementer=deque([implemented("first"), implemented("second")]),
        judge=deque([judged(passed=False), judged()]),
        perf=deque([evaluated()]),
    )
    trace = run_shell(executors)
    assert _stop(trace).result.outcome == "success"
    assert kinds(trace).count("StartAttempt") == 2
    assert _board(executors) == {1: ("closed", 2)}
    charges = [item.turn.charge_class for item in trace.decisions if isinstance(item, RequestTurn)]
    assert charges.count("paid") == 2


def test_an_issue_whose_attempts_are_spent_is_blocked_and_a_blocked_queue_fails_the_run() -> None:
    executors = Executors(
        implementer=deque([implemented(), implemented()]),
        judge=deque([judged(passed=False), judged(passed=False)]),
        perf=deque([evaluated()]),
    )
    trace = run_shell(executors)
    result = _stop(trace).result
    assert result.outcome == "failure"
    assert "blocked" in result.reason
    assert _board(executors) == {1: ("blocked", 2)}
    # Nothing closed, so nothing is adopted.
    assert not any(isinstance(item, ProposeWinner) for item in trace.decisions)
    assert kinds(trace).count("StartAttempt") == 1


def test_a_bug_the_judge_found_is_worked_before_the_round_ends() -> None:
    executors = Executors(
        implementer=deque([implemented(), implemented()]),
        judge=deque([judged(bugs=("streaming drops tokens",)), judged()]),
        perf=deque([evaluated()]),
    )
    trace = run_shell(executors)
    assert _stop(trace).result.outcome == "success"
    assert _titles(executors) == [
        "Build inference server for the reference model",
        "streaming drops tokens",
    ]
    assert _board(executors) == {1: ("closed", 1), 2: ("closed", 1)}


def test_issues_the_evaluator_filed_are_worked_in_the_next_round() -> None:
    executors = Executors(
        implementer=deque([implemented(), implemented()]),
        judge=deque([judged(), judged()]),
        perf=deque([evaluated("faster attention", kinds=("perf",)), evaluated()]),
    )
    trace = run_shell(executors)
    assert _stop(trace).result.outcome == "success"
    assert _board(executors) == {1: ("closed", 1), 2: ("closed", 1)}
    assert kinds(trace).count("StartAttempt") == 4


def test_the_round_budget_ends_a_run_that_keeps_finding_work() -> None:
    executors = Executors(
        implementer=deque(implemented() for _ in range(4)),
        judge=deque(judged() for _ in range(4)),
        perf=deque([evaluated("more"), evaluated("again")]),
    )
    trace = run_shell(executors, max_rounds=2)
    result = _stop(trace).result
    assert result.outcome == "failure"
    assert "round budget" in result.reason
    # The work done before the budget stop is still adopted.
    assert any(isinstance(item, ProposeWinner) for item in trace.decisions)
    assert [status for status, _ in _board(executors).values()].count("open") == 1


def test_no_more_issues_than_the_round_cap_are_worked_before_the_evaluation() -> None:
    executors = Executors(
        implementer=deque(implemented() for _ in range(4)),
        judge=deque(judged(bugs=("one",)) if n == 0 else judged() for n in range(4)),
        perf=deque([evaluated(), evaluated()]),
    )
    trace = run_shell(executors, max_issues_per_round=1)
    assert _stop(trace).result.outcome == "success"
    starts = [i for i, name in enumerate(kinds(trace)) if name == "StartAttempt"]
    assert len(starts) == 4  # issue, evaluation, the filed bug, evaluation
    assert _board(executors) == {1: ("closed", 1), 2: ("closed", 1)}


def test_the_work_is_not_adopted_when_the_final_candidate_fails_the_accuracy_gate() -> None:
    executors = _one_issue()
    executors.accuracy = lambda _commit: False
    trace = run_shell(executors)
    result = _stop(trace).result
    assert result.outcome == "failure"
    assert "not adopted" in result.reason
    assert not any(
        status is IssueStatus.OPEN for status in (i.status for i in executors.tracker.list())
    )
