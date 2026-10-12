"""Properties of how the strategy reads and writes the board (design properties Q2 to Q5).

The strategy decides from the last board snapshot it read, files only what its reply schemas
allow, stops by the legacy decision table, and works at most the round cap of issues between two
performance evaluations.
"""

from __future__ import annotations

import json
from itertools import groupby

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.issue_queue.strategy.world import (
    FACTS,
    IssueQueueWorld,
    evaluated,
    implemented,
    issue_queue_config,
    issue_queue_harness,
    judged,
)

from vibesys.orchestration.issue_queue.models import (
    JudgeReplyModel,
    perf_result_model,
)
from vibesys.orchestration.issue_queue.strategy.api import (
    BoardState,
    Cursor,
    IssueQueueStrategy,
    IssueQueueStrategyState,
    JobKind,
    bootstrap_title,
)
from vibesys.orchestration.issue_queue.strategy.api import PerformanceRecord as Record
from vs_core.api import Operation, Proposal, StartAttempt, project
from vs_core.testing.drive import new_run
from vs_dst.api import simulate
from vs_issue_tracker.api import IssueRow, IssueStatus, IssueType, ReadBoard

_KINDS = ["bug", "feature", "perf"]


# Q2: no reply can file more issues than its cap or an issue of a type the role may not file.


def _filed(kinds: list[str]) -> list[dict[str, str]]:
    return [{"type": kind, "title": f"t{n}", "description": "d"} for n, kind in enumerate(kinds)]


@given(kinds=st.lists(st.sampled_from(_KINDS), max_size=4))
def test_a_judge_reply_files_at_most_one_bug_and_nothing_else(kinds: list[str]) -> None:
    payload = json.dumps(
        {"analysis": "a", "feedback": "", "verdict": "pass", "new_issues": _filed(kinds)}
    )
    allowed = len(kinds) <= 1 and all(kind == "bug" for kind in kinds)
    if allowed:
        JudgeReplyModel.model_validate_json(payload)
    else:
        with pytest.raises(ValidationError):
            JudgeReplyModel.model_validate_json(payload)


@given(cap=st.integers(1, 4), kinds=st.lists(st.sampled_from([*_KINDS, "other"]), max_size=6))
def test_an_evaluator_reply_files_at_most_its_cap_of_issues_of_known_types(
    cap: int, kinds: list[str]
) -> None:
    reply = json.loads(evaluated())
    reply["new_issues"] = _filed(kinds)
    allowed = len(kinds) <= cap and all(kind in _KINDS for kind in kinds)
    if allowed:
        perf_result_model(cap).model_validate_json(json.dumps(reply))
    else:
        with pytest.raises(ValidationError):
            perf_result_model(cap).model_validate_json(json.dumps(reply))


# Q3: after a restart the first decision that depends on the board is to read it.


def _decide(state: IssueQueueStrategyState) -> list[object]:
    settings_ = issue_queue_config()
    strategy = IssueQueueStrategy(config=settings_)
    view = project(new_run(strategy, issue_queue_harness(settings_)))
    return list(strategy.bind(state).decide(view).decisions)


@pytest.mark.parametrize("cursor", [Cursor.START, Cursor.DRAIN, Cursor.ASSESS, Cursor.AFTER_PERF])
def test_a_board_that_may_be_stale_is_read_before_anything_is_decided_from_it(
    cursor: Cursor,
) -> None:
    decisions = _decide(IssueQueueStrategyState(cursor=cursor))
    assert len(decisions) == 1
    assert isinstance(decisions[0], Operation)
    assert isinstance(decisions[0].request, ReadBoard)


# Q4: the stop decision of a run is the legacy decision table, on any board.

MAX_ROUNDS = 4


def _row(number: int, status: IssueStatus) -> IssueRow:
    return IssueRow(
        id=number,
        type=IssueType.BUG,
        status=status,
        title=f"issue {number}",
        attempts=0,
        created_by="t",
        created_iter=1,
    )


def _board(statuses: list[IssueStatus]) -> BoardState:
    return BoardState(issues=tuple(_row(n + 1, s) for n, s in enumerate(statuses)), fresh=True)


def _blocked_queue(statuses: list[IssueStatus]) -> bool:
    """Legacy: work remains and every remaining issue is blocked, before the evaluation."""
    remaining = [status for status in statuses if status is not IssueStatus.CLOSED]
    return bool(remaining) and all(status is IssueStatus.BLOCKED for status in remaining)


def _proposal(state: IssueQueueStrategyState) -> Proposal[IssueQueueStrategyState]:
    settings_ = issue_queue_config(max_rounds=MAX_ROUNDS)
    strategy = IssueQueueStrategy(config=settings_)
    return strategy.bind(state).decide(project(new_run(strategy, issue_queue_harness(settings_))))


STATUSES = st.lists(st.sampled_from(list(IssueStatus)), max_size=6)


@settings(max_examples=60, deadline=None, derandomize=True, database=None)
@given(statuses=STATUSES, round_idx=st.integers(0, MAX_ROUNDS - 1))
def test_a_blocked_queue_ends_the_run_before_the_evaluation_and_any_other_board_is_evaluated(
    statuses: list[IssueStatus], round_idx: int
) -> None:
    proposal = _proposal(
        IssueQueueStrategyState(cursor=Cursor.ASSESS, round_idx=round_idx, board=_board(statuses))
    )
    starts = [item for item in proposal.decisions if isinstance(item, StartAttempt)]
    if _blocked_queue(statuses):
        assert starts == []
        assert proposal.state.ending is not None
        assert proposal.state.ending.outcome == "failure"
    else:
        assert len(starts) == 1
        assert proposal.state.ending is None


@settings(max_examples=60, deadline=None, derandomize=True, database=None)
@given(statuses=STATUSES, filed=st.integers(0, 2), round_idx=st.integers(0, MAX_ROUNDS - 1))
def test_after_an_evaluation_a_quiet_board_succeeds_and_otherwise_the_round_budget_decides(
    statuses: list[IssueStatus], filed: int, round_idx: int
) -> None:
    # The issues the evaluation filed are on the board it filed them to.
    statuses = [*statuses, *([IssueStatus.OPEN] * filed)]
    record = Record(
        iteration=round_idx + 1,
        throughput_trend="improved",
        latency_trend="mixed",
        metrics_json="{}",
        new_issues=tuple(range(100, 100 + filed)),
    )
    proposal = _proposal(
        IssueQueueStrategyState(
            cursor=Cursor.AFTER_PERF,
            round_idx=round_idx,
            board=_board(statuses),
            performance=(record,),
        )
    )
    ending = proposal.state.ending
    if IssueStatus.OPEN not in statuses and not filed:
        assert ending is not None
        assert ending.outcome == "success"
    elif round_idx + 1 >= MAX_ROUNDS:
        assert ending is not None
        assert ending.outcome == "failure"
    else:
        assert ending is None
        assert proposal.state.round_idx == round_idx + 1


# Q5: at most the round cap of issues are worked between two evaluations.


@settings(max_examples=10, deadline=None, derandomize=True, database=None)
@given(cap=st.integers(1, 3), humans=st.integers(0, 5))
def test_no_round_works_more_issues_than_the_cap(cap: int, humans: int) -> None:
    world = IssueQueueWorld(
        implementer=[implemented() for _ in range(40)],
        judge=[judged() for _ in range(40)],
        perf=[evaluated() for _ in range(8)],
        max_issues_per_round=cap,
        max_rounds=3,
    )
    for number in range(humans):
        world.tracker.create(
            type=IssueType.FEATURE,
            title=f"filed by a person {number}",
            description="d",
            created_by="human",
            iteration=0,
        )
    run = simulate(world)
    assert run.finished
    assert run.halted is None
    assert not run.liveness_violations
    kinds = [
        JobKind.PERF if decision.attempt_id.root.startswith("attempt:perf") else JobKind.ISSUE
        for decision in run.decisions
        if isinstance(decision, StartAttempt)
    ]
    for kind, group in groupby(kinds):
        if kind is JobKind.ISSUE:
            assert len(list(group)) <= cap
    assert bootstrap_title(FACTS.objective) in world.titles()
