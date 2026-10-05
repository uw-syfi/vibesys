"""A workstream whose implementer ends without a candidate is asked again only with news.

A turn that ends `implementation_failed` costs agent time and tokens and measures nothing.
The strategy asks the same workstream again only when the turn named a narrower next step or
cited new evidence, and at most `max_unmeasured_turns` times; otherwise the workstream settles
as failed and its slot returns to the planner. These tests drive whole runs on the production
shell with scripted agents and count the implementer turns the run requests.
"""

from __future__ import annotations

from collections import Counter, deque
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

from vs_core.api import ProposeWinner, RequestTurn

if TYPE_CHECKING:
    from tests.vibesys.orchestration.dynamic.strategy._shell import Run


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


def test_a_failure_that_repeats_its_blocker_is_not_asked_again_even_under_a_larger_bound() -> None:
    executors = Executors(
        planner=deque([plan_reply(implement("h1")), plan_reply(implement("h2"))]),
        implementer=deque([_failed("same", "a.py"), _failed("same", "a.py"), implemented()]),
        judge=deque([reviewed()]),
    )
    finished = run_shell(executors, max_rounds=2, max_retries_per_round=5, max_unmeasured_turns=5)
    assert sorted(_implementer_turns(finished).values()) == [1, 2]
    assert not executors.planner


def test_a_failure_that_narrows_the_scope_is_asked_again_and_can_be_measured() -> None:
    executors = Executors(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([_failed("whole scheduler"), _failed("one decode step"), implemented()]),
        judge=deque([reviewed()]),
    )
    finished = run_shell(executors, max_rounds=1, max_retries_per_round=5, max_unmeasured_turns=3)
    assert list(_implementer_turns(finished).values()) == [3]
    proposal = next(item for item in finished.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"


def test_new_evidence_alone_justifies_another_turn() -> None:
    executors = Executors(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([_failed("same", "a.py"), _failed("same", "b.py"), implemented()]),
        judge=deque([reviewed()]),
    )
    finished = run_shell(executors, max_rounds=1, max_retries_per_round=5, max_unmeasured_turns=3)
    assert list(_implementer_turns(finished).values()) == [3]


def test_an_unknown_option_is_rejected_by_name() -> None:
    with pytest.raises(ValidationError, match="max_unmeasured_turn"):
        config(max_unmeasured_turn=2)


def test_the_bound_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="max_unmeasured_turns"):
        config(max_unmeasured_turns=0)


_SAME = "same"


@settings(max_examples=20, deadline=None, derandomize=True, database=None)
@given(
    steps=st.lists(
        st.sampled_from([_SAME, "narrow-1", "narrow-2", "narrow-3"]), min_size=6, max_size=6
    ),
    bound=st.integers(min_value=1, max_value=3),
)
def test_unmeasured_turns_never_exceed_the_bound_and_the_run_terminates(
    steps: list[str], bound: int
) -> None:
    """Whatever the failing implementer says, a workstream costs at most `bound` turns."""
    executors = Executors(
        planner=deque([plan_reply(implement("h1")), plan_reply(implement("h2"))]),
        implementer=deque(_failed(step) for step in steps),
    )
    finished = run_shell(
        executors, max_rounds=2, max_retries_per_round=6, max_unmeasured_turns=bound
    )
    turns = _implementer_turns(finished)
    assert len(turns) == 2
    assert all(count <= bound for count in turns.values())
    assert _planner_turns(finished) == 2
