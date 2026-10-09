"""A workstream whose implementer ends without a candidate is asked again only with news.

A turn that ends `implementation_failed`, times out, or has its candidate rejected costs
agent time and tokens and measures nothing. The first such turn always earns one more turn.
Further turns need something the framework observed (a retained revision no earlier turn
had, or a failure output not seen before), never the agent's own wording, and at most
`max_unmeasured_turns` turns in all; otherwise the workstream settles as failed and its slot
returns to the planner. These tests drive whole runs on the production shell with scripted
agents and count the implementer turns the run requests.
"""

from __future__ import annotations

from collections import Counter, deque
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import config, run_shell

from vs_core.api import ProposeWinner, RequestTurn, Withdraw
from vs_core.testing.drive import Failed

if TYPE_CHECKING:
    from tests.vibesys.orchestration.dynamic.strategy._shell import Run

    from vs_core.testing.drive import Answer


def _failed(next_step: str, *cited: str) -> str:
    """An implementer reply that ends without a candidate."""
    return implemented(
        "implementation_failed",
        summary=f"blocked: {next_step}",
        next_step=next_step,
        evidence=[{"location": item, "purpose": "what the turn found"} for item in cited],
    )


def _implementer_turns(finished: Run) -> Counter[str]:
    """Paid and corrected implementer turns the run requested, by agent session."""
    return Counter(
        item.turn.session.session_id.root
        for item in finished.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("implementer")
    )


def _planner_turns(finished: Run) -> int:
    return sum(
        1
        for item in finished.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("orchestrator")
    )


def test_a_repeating_failure_costs_at_most_the_bound_then_the_planner_is_asked_again() -> None:
    executors = Executors(
        planner=deque([plan_reply(implement("h1")), plan_reply(implement("h2"))]),
        implementer=deque([_failed("same"), _failed("same"), _failed("same"), implemented()]),
        judge=deque([reviewed()]),
    )
    finished = run_shell(executors, max_rounds=2, max_retries_per_round=5)
    turns = _implementer_turns(finished)
    # h1 settles as failed after the default two unmeasured turns; h2 then takes the third
    # failing reply, and ends on the next.
    assert sorted(turns.values()) == [2, 2]
    assert not executors.planner
    assert not executors.implementer


def test_the_first_failure_without_a_next_step_or_evidence_still_gets_one_more_turn() -> None:
    executors = Executors(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented("implementation_failed"), implemented()]),
        judge=deque([reviewed()]),
    )
    finished = run_shell(executors, max_rounds=1, max_retries_per_round=5)
    assert list(_implementer_turns(finished).values()) == [2]
    proposal = next(item for item in finished.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"


def test_rewording_the_step_or_citing_more_files_does_not_earn_a_turn_under_a_larger_bound() -> (
    None
):
    executors = Executors(
        planner=deque([plan_reply(implement("h1")), plan_reply(implement("h2"))]),
        implementer=deque(
            [_failed("split it", "a.py"), _failed("break it up", "b.py"), implemented()]
        ),
        judge=deque([reviewed()]),
    )
    finished = run_shell(executors, max_rounds=2, max_retries_per_round=5, max_unmeasured_turns=5)
    # The worktree retained the same revision after both turns, so the second repeated the first.
    assert sorted(_implementer_turns(finished).values()) == [1, 2]
    assert not executors.planner


def test_a_turn_that_retains_a_new_revision_is_asked_again_and_can_be_measured() -> None:
    executors = Executors(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([_failed("same"), _failed("same"), implemented()]),
        judge=deque([reviewed()]),
        retain_per_turn=True,
    )
    finished = run_shell(executors, max_rounds=1, max_retries_per_round=5, max_unmeasured_turns=3)
    assert list(_implementer_turns(finished).values()) == [3]
    proposal = next(item for item in finished.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"


def test_turns_that_end_without_a_reply_count_toward_the_bound() -> None:
    executors = Executors(
        planner=deque([plan_reply(implement("h1")), plan_reply(implement("h2"))]),
        implementer=deque([Failed(), Failed(), implemented()]),
        judge=deque([reviewed()]),
    )
    finished = run_shell(executors, max_rounds=2, max_retries_per_round=5)
    # h1 settles as failed after two lost turns, so h2 gets the third reply and is measured.
    assert sorted(_implementer_turns(finished).values()) == [1, 2]
    assert not executors.implementer


def test_an_unknown_option_is_rejected_by_name() -> None:
    with pytest.raises(ValidationError, match="max_unmeasured_turn"):
        config(max_unmeasured_turn=2)


def test_the_bound_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="max_unmeasured_turns"):
        config(max_unmeasured_turns=0)


class _Turn(StrEnum):
    """How a turn of a workstream can end without a measured candidate."""

    FAILED_SAME = "failed-same"
    FAILED_WORDING_1 = "failed-wording-1"
    FAILED_WORDING_2 = "failed-wording-2"
    NO_REPLY = "no-reply"
    REJECTED = "rejected"


def _script(turns: list[_Turn]) -> tuple[deque[str | Answer], int]:
    """The implementer replies for a sequence of outcomes, and how many judge rejections."""
    replies: deque[str | Answer] = deque()
    for turn in turns:
        match turn:
            case _Turn.NO_REPLY:
                replies.append(Failed())
            case _Turn.REJECTED:
                replies.append(implemented())
            case _:
                replies.append(_failed(turn.value))
    return replies, sum(turn is _Turn.REJECTED for turn in turns)


@settings(max_examples=5, deadline=None, derandomize=True, database=None)
@given(
    turns=st.lists(st.sampled_from(_Turn), min_size=8, max_size=8),
    bound=st.integers(min_value=1, max_value=4),
    new_revision_each_turn=st.booleans(),
)
def test_unmeasured_turns_follow_the_bound_whatever_the_outcomes(
    turns: list[_Turn], bound: int, *, new_revision_each_turn: bool
) -> None:
    """Whatever the outcomes, a workstream costs at most `bound` turns and at least two.

    Judge rejections, failed turns and lost turns all count. The first one always earns
    another turn (when the bound allows). With one retained revision for all turns nothing
    framework-observed is new after the first retry except a lost turn's error text (once), so
    the agent's wording cannot buy more.
    """
    replies, rejections = _script(turns)
    executors = Executors(
        planner=deque([plan_reply(implement("h1")), plan_reply(implement("h2"))]),
        implementer=replies,
        judge=deque(reviewed(passed=False) for _ in range(rejections + 8)),
        retain_per_turn=new_revision_each_turn,
    )
    finished = run_shell(
        executors, max_rounds=2, max_retries_per_round=12, max_unmeasured_turns=bound
    )
    counts = _implementer_turns(finished)
    assert len(counts) == 2
    assert _planner_turns(finished) == 2
    for count in counts.values():
        assert min(bound, 2) <= count <= bound
        if not new_revision_each_turn:
            # A lost turn's error text is a failure output seen once, which earns one more.
            assert count <= 2 + (_Turn.NO_REPLY in turns)


def test_the_planner_is_not_re_asked_while_nothing_it_sees_has_changed() -> None:
    """A free slot beside a running workstream costs one planning call, not a retry loop.

    live-2: four planner turns in 45 s for one free slot, each repeating a plan the run
    could not accept. One planning call is its first reply plus `max_corrections`
    corrections; another needs a workstream to finish, which changes what the planner sees.
    h1 fails at once and h2 stays in flight until its judge answers, then finishes; the
    planner has enough replies that running out of them cannot end the run early, and the
    run is not held to the liveness invariants, so the asserted turn count is what fails
    without the fix.
    """
    executors = Executors(
        planner=deque(
            [plan_reply(implement("h1"), implement("h2")), *[plan_reply(implement("h1"))] * 40]
        ),
        implementer=deque([_failed("a"), implemented()]),
        judge=deque([reviewed()]),
    )
    finished = run_shell(
        executors,
        live=False,
        max_rounds=3,
        max_in_flight=2,
        max_retries_per_round=3,
        max_unmeasured_turns=1,
    )
    ends = [i for i, item in enumerate(finished.decisions) if isinstance(item, Withdraw)]
    assert len(ends) >= 2
    between = [
        item
        for item in finished.decisions[ends[0] : ends[1]]
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("orchestrator")
    ]
    # From h1's end to h2's end: one planning call, whatever its replies.
    assert len(between) <= 1 + config().max_corrections
