"""Contract of the `IssueTracker` interface, run against every implementation.

The same generated operation sequences run against the in-memory fake, the file-backed
board and the GitHub tracker over a fake GitHub repository. A pure model says what each
sequence must leave behind, so an implementation cannot be more or less forgiving than the
others.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_github.api import FakeGitHubClient
from vs_issue_tracker.api import (
    FakeIssueTracker,
    GitHubIssueTracker,
    IssueBoard,
    IssueStatus,
    IssueTracker,
    IssueType,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

type Factory = Callable[[Path], IssueTracker]

IMPLEMENTATIONS: dict[str, Factory] = {
    "fake": lambda _path: FakeIssueTracker(),
    "board": lambda path: IssueBoard(path / "issues.json"),
    "github": lambda _path: GitHubIssueTracker("owner/repo", cli=FakeGitHubClient()),
}


@pytest.fixture(params=sorted(IMPLEMENTATIONS))
def make_tracker(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[], IssueTracker]:
    """A factory of empty trackers of one implementation."""
    factory = IMPLEMENTATIONS[request.param]
    counter = iter(range(10_000))

    def make() -> IssueTracker:
        directory = tmp_path / f"t{next(counter)}"
        directory.mkdir()
        return factory(directory)

    return make


@dataclass(frozen=True)
class Create:
    """Create an issue of a type, optionally keyed."""

    type: IssueType
    keyed: bool


@dataclass(frozen=True)
class AttemptStep:
    """Record an attempt on the nth created issue."""

    target: int
    keyed: bool


@dataclass(frozen=True)
class Move:
    """Set the nth created issue to a status."""

    target: int
    status: IssueStatus
    keyed: bool


type Operation = Create | AttemptStep | Move

operations = st.one_of(
    st.builds(Create, st.sampled_from(IssueType), st.booleans()),
    st.builds(AttemptStep, st.integers(0, 4), st.booleans()),
    st.builds(Move, st.integers(0, 4), st.sampled_from(IssueStatus), st.booleans()),
)
_RANK = {IssueType.BUG: 0, IssueType.FEATURE: 1, IssueType.PERF: 2}


@dataclass
class Model:
    """What a sequence of operations must leave on any tracker."""

    ids: list[int]
    types: dict[int, IssueType]
    status: dict[int, IssueStatus]
    attempts: dict[int, int]
    keys: set[str]

    @classmethod
    def empty(cls) -> Model:
        """No issues."""
        return cls([], {}, {}, {}, set())


def _apply(tracker: IssueTracker, model: Model, steps: list[Operation]) -> None:
    for index, step in enumerate(steps):
        key = f"k{index}"
        match step:
            case Create():
                issue = tracker.create(
                    type=step.type,
                    title=f"title {index}",
                    description=f"description {index}",
                    created_by="tester",
                    iteration=1,
                    key=key if step.keyed else None,
                )
                model.ids.append(issue.id)
                model.types[issue.id] = step.type
                model.status[issue.id] = IssueStatus.OPEN
                model.attempts[issue.id] = 0
                if step.keyed:
                    model.keys.add(key)
            case AttemptStep() if model.ids:
                issue_id = model.ids[step.target % len(model.ids)]
                tracker.increment_attempts(
                    issue_id, actor="tester", iteration=1, key=key if step.keyed else None
                )
                model.attempts[issue_id] += 1
                if step.keyed:
                    model.keys.add(key)
            case Move() if model.ids:
                issue_id = model.ids[step.target % len(model.ids)]
                tracker.update_status(
                    issue_id,
                    step.status,
                    actor="tester",
                    iteration=1,
                    key=key if step.keyed else None,
                )
                model.status[issue_id] = step.status
                if step.keyed:
                    model.keys.add(key)
            case _:
                continue


@given(steps=st.lists(operations, max_size=12))
# The fixture is a factory: every example builds its own tracker in a fresh directory.
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_a_sequence_leaves_what_the_model_says(
    steps: list[Operation], make_tracker: Callable[[], IssueTracker]
) -> None:
    tracker = make_tracker()
    model = Model.empty()
    _apply(tracker, model, steps)

    issues = tracker.list()
    assert [issue.id for issue in issues] == sorted(model.ids)
    for issue in issues:
        assert issue.status is model.status[issue.id]
        assert issue.attempts == model.attempts[issue.id]
        assert issue.type is model.types[issue.id]
        fetched = tracker.get(issue.id)
        assert fetched is not None
        assert fetched.id == issue.id
    present = {i.id for i in issues}
    keys = {f"k{n}" for n in range(len(steps))}
    recorded = {key for key in keys if any(issue.has_key(key) for issue in issues)}
    assert recorded == model.keys
    assert present == set(model.ids)
    open_ids = [i for i in model.ids if model.status[i] is IssueStatus.OPEN]
    expected_next = min(open_ids, key=lambda i: (_RANK[model.types[i]], i), default=None)
    chosen = tracker.next_open()
    assert (None if chosen is None else chosen.id) == expected_next


@pytest.mark.parametrize("kind", ["create", "attempt", "status"])
def test_a_keyed_write_is_found_by_its_key_and_only_by_it(
    kind: Literal["create", "attempt", "status"], make_tracker: Callable[[], IssueTracker]
) -> None:
    tracker = make_tracker()
    first = tracker.create(
        type=IssueType.BUG, title="a", description="d", created_by="t", iteration=1
    )
    if kind == "create":
        tracker.create(
            type=IssueType.BUG,
            title="b",
            description="d",
            created_by="t",
            iteration=1,
            key="the-key",
        )
    elif kind == "attempt":
        tracker.increment_attempts(first.id, actor="t", iteration=1, key="the-key")
    else:
        tracker.update_status(first.id, IssueStatus.CLOSED, actor="t", iteration=1, key="the-key")
    issues = tracker.list()
    assert [issue.id for issue in issues if issue.has_key("the-key")] != []
    assert not any(issue.has_key("another-key") for issue in issues)


def test_a_key_never_leaks_into_the_description(make_tracker: Callable[[], IssueTracker]) -> None:
    tracker = make_tracker()
    issue = tracker.create(
        type=IssueType.FEATURE,
        title="t",
        description="the body\n\nwith two paragraphs",
        created_by="t",
        iteration=1,
        key="k",
    )
    assert issue.description == "the body\n\nwith two paragraphs"
    stored = tracker.get(issue.id)
    assert stored is not None
    assert stored.description == "the body\n\nwith two paragraphs"
    assert stored.create_key == "k"


def test_the_fake_can_lose_an_acknowledgement_after_the_write_landed() -> None:
    def lose_the_answer(operation: str, phase: str) -> None:
        if (operation, phase) == ("create", "after"):
            message = "connection lost"
            raise ConnectionError(message)

    tracker = FakeIssueTracker(faults=lose_the_answer)
    with pytest.raises(ConnectionError):
        tracker.create(
            type=IssueType.BUG,
            title="t",
            description="d",
            created_by="t",
            iteration=1,
            key="k",
        )
    tracker.faults = None
    assert [issue.has_key("k") for issue in tracker.list()] == [True]


def test_the_fake_can_fail_before_the_write_lands() -> None:
    def refuse(operation: str, phase: str) -> None:
        if (operation, phase) == ("create", "before"):
            message = "unavailable"
            raise ConnectionError(message)

    tracker = FakeIssueTracker(faults=refuse)
    with pytest.raises(ConnectionError):
        tracker.create(
            type=IssueType.BUG, title="t", description="d", created_by="t", iteration=1, key="k"
        )
    tracker.faults = None
    assert tracker.list() == []
