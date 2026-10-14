"""A deep copy of the fake tracker is a board of its own holding the same issues."""

from __future__ import annotations

import copy

from vs_issue_tracker.api import FakeIssueTracker, IssueType
from vs_sim.api.testing import ManualClock, SimThreads


def _file(tracker: FakeIssueTracker, title: str) -> int:
    return tracker.create(
        type=IssueType.BUG, title=title, description="d", created_by="t", iteration=1
    ).id


def test_a_copy_holds_the_same_issues_and_then_diverges_from_the_original() -> None:
    tracker = FakeIssueTracker(clock=ManualClock(), threads=SimThreads())
    first = _file(tracker, "first")

    clone = copy.deepcopy(tracker)
    second = _file(tracker, "second")
    third = _file(clone, "third")

    assert _title(clone, first) == "first"
    assert second == third
    assert _title(clone, third) == "third"
    assert _title(tracker, second) == "second"


def _title(tracker: FakeIssueTracker, issue_id: int) -> str:
    issue = tracker.get(issue_id)
    assert issue is not None
    return issue.title
