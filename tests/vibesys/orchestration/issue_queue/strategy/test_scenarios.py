"""Whole runs on the production shell: bootstrap, work an issue, review, evaluate, adopt.

Each run goes through `vs_runtime`'s real run loop and shell, with only the executors
scripted, so what core is handed is what production hands it. The board is a real owner over a
fake tracker, so a test reads the outcome off the board as well as off the decisions.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.issue_queue.strategy._executors import Executors
from tests.vibesys.orchestration.issue_queue.strategy._replies import evaluated, implemented, judged
from tests.vibesys.orchestration.issue_queue.strategy._run import kinds, run_shell

from vs_core.api import ProposeWinner, RequestTurn, Stop, SubmitMeasurement
from vs_core.testing.drive import Unknown
from vs_issue_tracker.api import IssueStatus

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.testing.drive import Answer
    from vs_runtime.api.testing import Run


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


def _stop(trace: Run) -> Stop:
    final = trace.decisions[-1]
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
        "serve the model",
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


class _SilentVerification(Executors):
    """The trusted accuracy measurement of the final candidate never reports."""

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, SubmitMeasurement) and request.plan.purpose == "official":
            return Unknown()
        return super().__call__(request, core)


def test_a_final_accuracy_measurement_core_blocks_ends_the_run_without_an_adoption() -> None:
    executors = _SilentVerification(
        implementer=deque([implemented()]), judge=deque([judged()]), perf=deque([evaluated()])
    )
    trace = run_shell(executors)
    result = _stop(trace).result
    assert result.outcome == "failure"
    assert "not adopted" in result.reason


def test_an_invalid_reply_is_corrected_once_and_then_accepted() -> None:
    executors = _one_issue()
    executors.implementer.appendleft("not json")
    trace = run_shell(executors)
    assert _stop(trace).result.outcome == "success"
    turns = [item.turn for item in trace.decisions if isinstance(item, RequestTurn)]
    corrections = [turn for turn in turns if turn.charge_class == "correction"]
    assert len(corrections) == 1
    assert corrections[0].predecessor is not None
    # A correction speaks in its predecessor's session.
    assert (
        len({turn.session for turn in turns if turn.session.role_id.root.endswith("implementer")})
        == 1
    )


def test_a_reply_that_stays_invalid_ends_the_run_in_failure_with_nothing_adopted() -> None:
    executors = _one_issue()
    executors.implementer.extendleft(["not json", "still not json"])
    trace = run_shell(executors)
    result = _stop(trace).result
    assert result.outcome == "failure"
    assert "implementer turn did not complete" in result.reason
    assert not any(isinstance(item, ProposeWinner) for item in trace.decisions)


def test_a_run_that_spends_every_budget_stays_within_the_derived_limits() -> None:
    """Every issue fails review to its last attempt, with a broken reply to correct each time."""
    executors = Executors(
        implementer=deque(reply for _ in range(12) for reply in ("not json", implemented())),
        judge=deque(judged(passed=False) for _ in range(12)),
        perf=deque(evaluated("again") for _ in range(4)),
    )
    trace = run_shell(executors, max_rounds=2, max_attempts_per_issue=2, max_issues_per_round=2)
    core = trace.core
    limits = core.run.limits
    turns = sum(
        charge.charged
        for attempt in core.attempts.attempts
        for charge in attempt.charges
        if charge.kind.value == "turn"
    )
    assert len(core.attempts.attempts) <= limits.max_attempts
    assert turns <= limits.max_turns


def test_an_issue_closed_by_a_turn_that_changed_nothing_keeps_the_tip_and_adopts_nothing() -> None:
    """The work was already there: the issue closes, no candidate is retained, nothing is adopted."""
    executors = _one_issue()
    executors.unchanged.add("attempt:issue:1")
    trace = run_shell(executors)
    result = _stop(trace).result
    assert result.outcome == "success"
    assert not any(isinstance(item, ProposeWinner) for item in trace.decisions)
    assert _board(executors) == {1: ("closed", 1)}
