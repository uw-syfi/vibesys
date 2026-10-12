"""The first issue of a run comes from the task's objective, never from a fixed phrase."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.issue_queue.strategy._run import FACTS, config, harness

from vibesys.orchestration.issue_queue.strategy.api import (
    BoardState,
    Cursor,
    IssueQueueStrategy,
    IssueQueueStrategyState,
)
from vs_core.api import Operation, Stop, project
from vs_core.testing.drive import new_run
from vs_issue_tracker.api import ApplyIssueChanges, CreateIssue

_WORDS = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=0x24F),
    min_size=1,
    max_size=12,
)
OBJECTIVES = st.lists(_WORDS, min_size=1, max_size=40).map(" ".join)


def _first_decisions(objective: str) -> list[object]:
    settings_ = config()
    strategy = IssueQueueStrategy(config=settings_)
    facts = FACTS.model_copy(update={"objective": objective})
    view = project(new_run(strategy, harness(settings_, facts=facts)))
    state = IssueQueueStrategyState(cursor=Cursor.START, board=BoardState(fresh=True))
    return list(strategy.bind(state).decide(view).decisions)


def _created(decisions: list[object]) -> CreateIssue:
    assert len(decisions) == 1
    operation = decisions[0]
    assert isinstance(operation, Operation)
    assert isinstance(operation.request, ApplyIssueChanges)
    (create,) = operation.request.changes
    assert isinstance(create, CreateIssue)
    return create


@settings(max_examples=80, deadline=None, derandomize=True, database=None)
@given(objective=OBJECTIVES)
def test_the_first_issue_is_titled_from_the_objective_of_any_task(objective: str) -> None:
    title = _created(_first_decisions(objective)).title
    assert title
    assert "\n" not in title
    assert len(title) <= 80
    assert title.removesuffix("...") in " ".join(objective.split())
    assert "inference server" not in title


def test_a_rowwise_kernel_task_is_not_seeded_with_a_serving_issue() -> None:
    objective = "Write a PyTorch rowwise matmul and softmax kernel. Make it fast."
    assert _created(_first_decisions(objective)).title == (
        "Write a PyTorch rowwise matmul and softmax kernel."
    )


@given(blank=st.text(alphabet=" \t\n\r", max_size=8))
def test_a_task_without_an_objective_fails_the_run_instead_of_seeding_an_unrelated_issue(
    blank: str,
) -> None:
    (decision,) = _first_decisions(blank)
    assert isinstance(decision, Stop)
    assert decision.result.outcome == "failure"
    assert "objective" in decision.result.reason
