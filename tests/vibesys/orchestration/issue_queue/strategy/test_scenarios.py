"""Whole runs on the production shell: bootstrap, work an issue, review, evaluate, adopt.

Each run goes through `vs_runtime`'s real run loop and shell, with only the executors
scripted, so what core is handed is what production hands it. The board is a real owner over a
fake tracker, so a test reads the outcome off the board as well as off the decisions.
"""

from __future__ import annotations

from tests.vibesys.orchestration.issue_queue.strategy.world import (
    IssueQueueWorld,
    evaluated,
    generic_evaluated,
    implemented,
    judged,
)

from vs_core.api import ProposeWinner, RequestTurn, Stop
from vs_dst.api import SimulatedRun, simulate
from vs_issue_tracker.api import IssueStatus


def _one_issue(
    *, accurate: bool = True, silent_official: bool = False, unchanged: tuple[str, ...] = ()
) -> IssueQueueWorld:
    return IssueQueueWorld(
        implementer=[implemented()],
        judge=[judged()],
        perf=[evaluated()],
        accurate=accurate,
        silent_official=silent_official,
        unchanged=unchanged,
    )


def _ran(world: IssueQueueWorld) -> SimulatedRun:
    """The run of ``world`` to its end: live, and not given up on by the loop."""
    run = simulate(world)
    assert run.finished
    assert run.halted is None
    assert not run.liveness_violations
    return run


def _kinds(run: SimulatedRun) -> list[str]:
    return [type(item).__name__ for item in run.decisions]


def _stop(run: SimulatedRun) -> Stop:
    final = run.decisions[-1]
    assert isinstance(final, Stop)
    return final


def test_a_quiet_board_ends_in_success_with_the_work_adopted() -> None:
    world = _one_issue()
    run = _ran(world)
    assert _stop(run).result.outcome == "success"
    assert any(isinstance(item, ProposeWinner) for item in run.decisions)
    assert _kinds(run).count("StartAttempt") == 2  # the issue, then the evaluation
    assert world.board() == {1: ("closed", 1)}


def test_a_generic_domain_run_accepts_domain_neutral_performance_evidence() -> None:
    world = IssueQueueWorld(
        implementer=[implemented()],
        judge=[judged()],
        perf=[generic_evaluated()],
        domain_id="generic",
        record_requests=True,
    )

    run = _ran(world)

    assert _stop(run).result.outcome == "success"
    assert world.board() == {1: ("closed", 1)}
    assert world.titles() == ["serve the model"]
    assert world.requests
    assert all("legacy_load_levels" not in request.model_dump_json() for request in world.requests)


def test_a_failed_review_is_the_next_paid_turn_of_the_same_attempt() -> None:
    world = IssueQueueWorld(
        implementer=[implemented("first"), implemented("second")],
        judge=[judged(passed=False), judged()],
        perf=[evaluated()],
    )
    run = _ran(world)
    assert _stop(run).result.outcome == "success"
    assert _kinds(run).count("StartAttempt") == 2
    assert world.board() == {1: ("closed", 2)}
    charges = [item.turn.charge_class for item in run.decisions if isinstance(item, RequestTurn)]
    assert charges.count("paid") == 2


def test_an_issue_whose_attempts_are_spent_is_blocked_and_a_blocked_queue_fails_the_run() -> None:
    world = IssueQueueWorld(
        implementer=[implemented(), implemented()],
        judge=[judged(passed=False), judged(passed=False)],
        perf=[evaluated()],
    )
    run = _ran(world)
    result = _stop(run).result
    assert result.outcome == "failure"
    assert "blocked" in result.reason
    assert world.board() == {1: ("blocked", 2)}
    # Nothing closed, so nothing is adopted.
    assert not any(isinstance(item, ProposeWinner) for item in run.decisions)
    assert _kinds(run).count("StartAttempt") == 1


def test_a_bug_the_judge_found_is_worked_before_the_round_ends() -> None:
    world = IssueQueueWorld(
        implementer=[implemented(), implemented()],
        judge=[judged(bugs=("streaming drops tokens",)), judged()],
        perf=[evaluated()],
    )
    run = _ran(world)
    assert _stop(run).result.outcome == "success"
    assert world.titles() == ["serve the model", "streaming drops tokens"]
    assert world.board() == {1: ("closed", 1), 2: ("closed", 1)}


def test_issues_the_evaluator_filed_are_worked_in_the_next_round() -> None:
    world = IssueQueueWorld(
        implementer=[implemented(), implemented()],
        judge=[judged(), judged()],
        perf=[evaluated("faster attention", kinds=("perf",)), evaluated()],
    )
    run = _ran(world)
    assert _stop(run).result.outcome == "success"
    assert world.board() == {1: ("closed", 1), 2: ("closed", 1)}
    assert _kinds(run).count("StartAttempt") == 4


def test_the_round_budget_ends_a_run_that_keeps_finding_work() -> None:
    world = IssueQueueWorld(
        implementer=[implemented() for _ in range(4)],
        judge=[judged() for _ in range(4)],
        perf=[evaluated("more"), evaluated("again")],
        max_rounds=2,
    )
    run = _ran(world)
    result = _stop(run).result
    assert result.outcome == "failure"
    assert "round budget" in result.reason
    # The work done before the budget stop is still adopted.
    assert any(isinstance(item, ProposeWinner) for item in run.decisions)
    assert [status for status, _ in world.board().values()].count("open") == 1


def test_no_more_issues_than_the_round_cap_are_worked_before_the_evaluation() -> None:
    world = IssueQueueWorld(
        implementer=[implemented() for _ in range(4)],
        judge=[judged(bugs=("one",)) if n == 0 else judged() for n in range(4)],
        perf=[evaluated(), evaluated()],
        max_issues_per_round=1,
    )
    run = _ran(world)
    assert _stop(run).result.outcome == "success"
    assert _kinds(run).count("StartAttempt") == 4  # issue, evaluation, the filed bug, evaluation
    assert world.board() == {1: ("closed", 1), 2: ("closed", 1)}


def test_the_work_is_not_adopted_when_the_final_candidate_fails_the_accuracy_gate() -> None:
    world = _one_issue(accurate=False)
    run = _ran(world)
    result = _stop(run).result
    assert result.outcome == "failure"
    assert "not adopted" in result.reason
    assert not any(
        status is IssueStatus.OPEN for status in (i.status for i in world.tracker.list())
    )


def test_a_final_accuracy_measurement_core_blocks_ends_the_run_without_an_adoption() -> None:
    """The trusted accuracy measurement of the final candidate never reports."""
    run = _ran(_one_issue(silent_official=True))
    result = _stop(run).result
    assert result.outcome == "failure"
    assert "not adopted" in result.reason


def test_an_invalid_reply_is_corrected_once_and_then_accepted() -> None:
    world = IssueQueueWorld(
        implementer=["not json", implemented()], judge=[judged()], perf=[evaluated()]
    )
    run = _ran(world)
    assert _stop(run).result.outcome == "success"
    turns = [item.turn for item in run.decisions if isinstance(item, RequestTurn)]
    corrections = [turn for turn in turns if turn.charge_class == "correction"]
    assert len(corrections) == 1
    assert corrections[0].predecessor is not None
    # A correction speaks in its predecessor's session.
    assert (
        len({turn.session for turn in turns if turn.session.role_id.root.endswith("implementer")})
        == 1
    )


def test_a_reply_that_stays_invalid_ends_the_run_in_failure_with_nothing_adopted() -> None:
    world = IssueQueueWorld(
        implementer=["not json", "still not json", implemented()],
        judge=[judged()],
        perf=[evaluated()],
    )
    run = _ran(world)
    result = _stop(run).result
    assert result.outcome == "failure"
    assert "implementer turn did not complete" in result.reason
    assert not any(isinstance(item, ProposeWinner) for item in run.decisions)


def test_a_run_that_spends_every_budget_stays_within_the_derived_limits() -> None:
    """Every issue fails review to its last attempt, with a broken reply to correct each time."""
    world = IssueQueueWorld(
        implementer=[reply for _ in range(12) for reply in ("not json", implemented())],
        judge=[judged(passed=False) for _ in range(12)],
        perf=[evaluated("again") for _ in range(4)],
        max_rounds=2,
        max_attempts_per_issue=2,
        max_issues_per_round=2,
    )
    run = _ran(world)
    core = run.core
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
    world = _one_issue(unchanged=("attempt:issue:1",))
    run = _ran(world)
    result = _stop(run).result
    assert result.outcome == "success"
    assert not any(isinstance(item, ProposeWinner) for item in run.decisions)
    assert world.board() == {1: ("closed", 1)}
