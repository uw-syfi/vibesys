"""The issue-board owner: keyed writes survive any fault schedule and replay without duplicates."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import OperationRegistry, Until
from vs_issue_tracker.api import (
    AppliedChanges,
    ApplyIssueChanges,
    BoardSnapshot,
    CreateIssue,
    FakeIssueTracker,
    IssueStatus,
    IssueType,
    LogProgress,
    MemoryProgressLog,
    ReadBoard,
    RecordAttempt,
    SetIssueStatus,
    TemplateBody,
    TextBody,
    board_operation_registrations,
)
from vs_runtime.api.core import (
    Applied,
    BoardTexts,
    ExecutionContext,
    IssueBoardOwner,
    NotApplied,
)
from vs_sim.api.testing import ManualClock, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from vs_core.api import OperationRequest
    from vs_issue_tracker.api import IssueChange


class _Texts:
    def issue_body(self, template: str) -> str:
        return f"rendered {template}"

    def progress_entry(self, request: LogProgress) -> str:
        return f"{request.step} {request.iteration}"


TEXTS: BoardTexts = _Texts()


class Crash(Exception):  # noqa: N818  # lint-waiver: LW-771100 [N818]; a stand-in for any tracker failure that leaves the effect unknown.
    """The tracker failed around an operation."""


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def execute(owner: IssueBoardOwner, request: OperationRequest) -> object:
    return run(owner.execute(request, _context(request)))


def _context(request: OperationRequest) -> ExecutionContext:
    return ExecutionContext(
        now_at=5.0,
        payload_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        until=Until(at=100.0),
    )


def owner_over(
    tracker: FakeIssueTracker, progress: MemoryProgressLog | None = None
) -> IssueBoardOwner:
    return IssueBoardOwner(tracker, progress or MemoryProgressLog(), TEXTS)


def test_the_three_operations_form_a_valid_closed_registry() -> None:
    registry = OperationRegistry(board_operation_registrations())
    assert {descriptor.kind for descriptor in registry.descriptors} == {
        "issue_board.read",
        "issue_board.apply_changes",
        "issue_board.log_progress",
    }


@dataclass(frozen=True)
class Step:
    """One change a generated batch makes: a create, or an attempt or status on an issue."""

    kind: str
    target: int
    status: IssueStatus


steps = st.builds(
    Step,
    st.sampled_from(["create", "attempt", "status"]),
    st.integers(0, 3),
    st.sampled_from(IssueStatus),
)


def batch_of(plan: list[Step], tag: str, existing: int) -> ApplyIssueChanges:
    """A batch of keyed changes; changes on issues that do not exist yet become creates."""
    changes: list[IssueChange] = []
    known = existing
    for index, step in enumerate(plan):
        key = f"{tag}.{index}"
        if step.kind == "create" or known == 0:
            changes.append(
                CreateIssue(
                    key=key,
                    type=IssueType.BUG,
                    title=f"title {key}",
                    body=TextBody(text=f"text {key}"),
                    created_by="loop",
                    iteration=1,
                )
            )
            known += 1
        elif step.kind == "attempt":
            changes.append(
                RecordAttempt(
                    key=key,
                    issue_id=1 + step.target % known,
                    actor="implementer",
                    iteration=1,
                    payload_json='{"summary": "did it"}',
                )
            )
        else:
            changes.append(
                SetIssueStatus(
                    key=key,
                    issue_id=1 + step.target % known,
                    status=step.status,
                    actor="judge",
                    iteration=1,
                )
            )
    return ApplyIssueChanges(changes=tuple(changes))


def board_of(tracker: FakeIssueTracker) -> list[tuple[object, ...]]:
    return [
        (
            issue.id,
            issue.status,
            issue.attempts,
            [(event.action, event.key) for event in issue.history],
        )
        for issue in tracker.list()
    ]


def settle(owner: IssueBoardOwner, request: ApplyIssueChanges) -> AppliedChanges:
    """What the shell does with an operation: execute, and after a failure inspect then repeat."""
    resumed = False
    for _ in range(50):
        try:
            if resumed:
                found = run(owner.inspect(request, _context(request)))
                if isinstance(found, Applied):
                    outcome = found.outcome
                    assert isinstance(outcome, AppliedChanges)
                    return outcome
            outcome = execute(owner, request)
        except Crash:
            resumed = True
        else:
            assert isinstance(outcome, AppliedChanges)
            return outcome
    message = "the fault schedule never let the operation finish"
    raise AssertionError(message)


@given(
    plans=st.lists(st.lists(steps, min_size=1, max_size=4), min_size=1, max_size=3),
    schedule=st.sets(st.integers(0, 40), max_size=8),
    when=st.sampled_from(["before", "after"]),
)
@settings(max_examples=examples(pr=5, nightly=60), deadline=None)
def test_any_fault_schedule_and_replay_lands_exactly_the_clean_board(
    plans: list[list[Step]], schedule: set[int], when: str
) -> None:
    clean = FakeIssueTracker(clock=ManualClock(), threads=SimThreads())
    clean_owner = owner_over(clean)
    faulty_calls = iter(range(10_000))
    faulty = FakeIssueTracker(clock=ManualClock(), threads=SimThreads())

    def fail_on_schedule(operation: str, phase: str) -> None:
        if operation in {"list", "get"}:
            return
        if phase == when and next(faulty_calls) in schedule:
            raise Crash

    faulty.faults = fail_on_schedule
    faulty_owner = owner_over(faulty)
    created = 0
    for number, plan in enumerate(plans):
        batch = batch_of(plan, f"b{number}", created)
        created += sum(isinstance(change, CreateIssue) for change in batch.changes)
        expected = execute(clean_owner, batch)
        landed = settle(faulty_owner, batch)
        assert landed == expected
    faulty.faults = None
    assert board_of(faulty) == board_of(clean)


def test_inspect_says_applied_only_when_every_change_landed() -> None:
    tracker = FakeIssueTracker(clock=ManualClock(), threads=SimThreads())
    owner = owner_over(tracker)
    batch = batch_of(
        [Step("create", 0, IssueStatus.OPEN), Step("create", 0, IssueStatus.OPEN)], "x", 0
    )
    assert isinstance(run(owner.inspect(batch, _context(batch))), NotApplied)
    tracker.create(
        type=IssueType.BUG,
        title="t",
        description="d",
        created_by="loop",
        iteration=1,
        key="x.0",
    )
    assert isinstance(run(owner.inspect(batch, _context(batch))), NotApplied)
    execute(owner, batch)
    assert isinstance(run(owner.inspect(batch, _context(batch))), Applied)
    assert len(tracker.list()) == 2


def test_a_change_on_a_missing_issue_is_rejected_by_name_without_raising() -> None:
    owner = owner_over(FakeIssueTracker(clock=ManualClock(), threads=SimThreads()))
    batch = ApplyIssueChanges(
        changes=(
            SetIssueStatus(
                key="k", issue_id=9, status=IssueStatus.CLOSED, actor="judge", iteration=1
            ),
        )
    )
    outcome = execute(owner, batch)
    assert isinstance(outcome, AppliedChanges)
    assert outcome.status == "rejected"
    assert "'k'" in outcome.detail


def test_a_template_body_is_rendered_by_the_strategys_texts() -> None:
    tracker = FakeIssueTracker(clock=ManualClock(), threads=SimThreads())
    batch = ApplyIssueChanges(
        changes=(
            CreateIssue(
                key="boot",
                type=IssueType.FEATURE,
                title="Build",
                body=TemplateBody(template="bootstrap_description"),
                created_by="loop:bootstrap",
                iteration=1,
            ),
        )
    )
    execute(owner_over(tracker), batch)
    assert [issue.description for issue in tracker.list()] == ["rendered bootstrap_description"]


def test_read_board_omits_the_text_of_closed_issues_only() -> None:
    tracker = FakeIssueTracker(clock=ManualClock(), threads=SimThreads())
    for number in (1, 2):
        tracker.create(
            type=IssueType.BUG,
            title=f"t{number}",
            description=f"d{number}",
            created_by="loop",
            iteration=1,
        )
    tracker.update_status(2, IssueStatus.CLOSED, actor="judge", iteration=1)
    snapshot = execute(owner_over(tracker), ReadBoard())
    assert isinstance(snapshot, BoardSnapshot)
    assert [(row.id, row.description) for row in snapshot.issues] == [(1, "d1"), (2, "")]


def test_progress_is_logged_once_per_key() -> None:
    progress = MemoryProgressLog()
    owner = owner_over(FakeIssueTracker(clock=ManualClock(), threads=SimThreads()), progress)
    entry = LogProgress(
        key="p1", iteration=1, step="implement", issue_id=1, payload_json='{"summary": "s"}'
    )
    assert isinstance(run(owner.inspect(entry, _context(entry))), NotApplied)
    execute(owner, entry)
    execute(owner, entry)
    assert progress.read().count("implement 1") == 1
    assert isinstance(run(owner.inspect(entry, _context(entry))), Applied)


def test_every_board_read_is_published_for_a_human_readable_mirror() -> None:
    tracker = FakeIssueTracker(clock=ManualClock(), threads=SimThreads())
    tracker.create(type=IssueType.BUG, title="t", description="d", created_by="loop", iteration=1)
    seen: list[int] = []
    owner = IssueBoardOwner(
        tracker,
        MemoryProgressLog(),
        TEXTS,
        publish=lambda issues: seen.append(len(issues)),
    )
    execute(owner, ReadBoard())
    execute(owner, ReadBoard())
    assert seen == [1, 1]
