"""Properties of the issue-queue search's pure helpers: the queue, the board reading, the writes.

Every decision the root takes is a function of the board it read and the queue's own record, so
these hold on every replay of every run.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.issue_queue._host import config
from tests.vibesys.orchestration.issue_queue._replies import evaluated, implemented, judged

from vibesys.orchestration.issue_queue.search.api import (
    Assess,
    Closing,
    Failure,
    FailureKind,
    Finish,
    IssueJob,
    PerfJob,
    Queue,
    Work,
    assessment_of,
    block,
    blocked_queue,
    bootstrap_title,
    bootstrap_writes,
    briefs,
    claim,
    filing,
    has_open,
    next_open,
    record,
    remaining,
    verdict,
)
from vs_core.api import InvocationId, InvocationRef, RevisionRef, SessionId
from vs_issue_tracker.api import IssueRow, IssueStatus, IssueType
from vs_sim.api.testing import examples

BASE = RevisionRef.of_git_commit("abc")
RULES = config()

rows = st.builds(
    IssueRow,
    id=st.integers(1, 40),
    type=st.sampled_from(IssueType),
    status=st.sampled_from(IssueStatus),
    title=st.text(min_size=1, max_size=20),
    description=st.text(max_size=20),
    attempts=st.integers(0, 3),
    created_by=st.just("loop"),
    created_iter=st.integers(0, 3),
)
boards = st.lists(rows, max_size=8, unique_by=lambda row: row.id).map(tuple)
queues = st.builds(
    Queue,
    round_idx=st.integers(0, 3),
    processed=st.integers(0, 6),
    assessed=st.booleans(),
    filed=st.booleans(),
)
SETTINGS = settings(
    max_examples=examples(pr=10, nightly=80), deadline=None, derandomize=True, database=None
)


@SETTINGS
@given(issues=boards)
def test_the_next_issue_is_open_and_no_open_issue_outranks_it(issues: tuple[IssueRow, ...]) -> None:
    pick = next_open(issues)

    assert (pick is not None) == has_open(issues)
    if pick is not None:
        assert pick.status is IssueStatus.OPEN
        order = [IssueType.BUG, IssueType.FEATURE, IssueType.PERF]
        for other in issues:
            if other.status is IssueStatus.OPEN:
                assert (order.index(pick.type), pick.id) <= (order.index(other.type), other.id)


@SETTINGS
@given(issues=boards)
def test_a_queue_is_blocked_only_when_work_remains_and_all_of_it_is_blocked(
    issues: tuple[IssueRow, ...],
) -> None:
    left = remaining(issues)

    assert blocked_queue(issues) == (
        bool(left) and all(row.status is IssueStatus.BLOCKED for row in left)
    )
    assert not (blocked_queue(issues) and has_open(issues))


@SETTINGS
@given(issues=boards, queue=queues)
def test_the_queue_decides_the_same_thing_every_time_and_within_its_budgets(
    issues: tuple[IssueRow, ...], queue: Queue
) -> None:
    step = queue.decide(issues, RULES)

    assert step == queue.decide(issues, RULES)
    if queue.round_idx >= RULES.max_rounds and not isinstance(step, Finish):
        raise AssertionError(step)
    match step:
        case Work(issue=issue):
            assert issue.status is IssueStatus.OPEN
            assert queue.processed < RULES.issues_per_round
            assert queue.round_idx < RULES.max_rounds
        case Assess():
            assert not has_open(issues) or queue.processed >= RULES.issues_per_round
            assert not blocked_queue(issues)
        case Finish(ending=ending):
            assert ending.reason


@SETTINGS
@given(issues=boards, queue=queues)
def test_a_drained_board_after_a_quiet_evaluation_is_a_success(
    issues: tuple[IssueRow, ...], queue: Queue
) -> None:
    quiet = queue.model_copy(update={"assessed": True, "filed": False})
    step = quiet.decide(issues, RULES)

    if not has_open(issues):
        assert isinstance(step, Finish)
        assert step.ending.outcome == "success"


@SETTINGS
@given(text=st.text(max_size=300))
def test_a_bootstrap_title_is_one_short_line_of_the_objective(text: str) -> None:
    title = bootstrap_title(text)

    assert "\n" not in title
    assert len(title) <= 80
    if not text.strip():
        assert title == ""


def _job(run_id: str, sequence: int, issue: IssueRow) -> IssueJob:
    return IssueJob(
        sequence=sequence,
        iteration=1,
        run_id=run_id,
        base=BASE,
        board=briefs((issue,)),
        config=RULES,
        issue=issue,
    )


ISSUE = IssueRow(
    id=1,
    type=IssueType.FEATURE,
    status=IssueStatus.OPEN,
    title="t",
    description="d",
    attempts=0,
    created_by="loop",
    created_iter=0,
)


def written(run_id: str) -> list[str]:
    """The key of every change and log entry a run's steps write, in order."""
    job = _job(run_id, 1, ISSUE)
    perf = PerfJob(
        sequence=2, iteration=1, run_id=run_id, base=BASE, board=briefs((ISSUE,)), config=RULES
    )
    writes = [
        bootstrap_writes(run_id, "title", 1),
        claim(job),
        record(job, 1, implemented()),
        verdict(job, 1, judged(bugs=("a",))),
        verdict(job, 2, judged()),
        block(job),
        filing(perf, evaluated("x", "y").new_issues, "{}"),
    ]
    return [item.key for write in writes for item in (*write.changes, *write.logs)]


@SETTINGS
@given(run_ids=st.lists(st.text(min_size=1, max_size=4), min_size=2, max_size=2, unique=True))
def test_every_change_of_a_run_has_its_own_key_and_two_runs_share_none(
    run_ids: list[str],
) -> None:
    first, second = (written(run_id) for run_id in run_ids)

    for found in (first, second):
        assert len(found) == len(set(found)), "a key repeats within one run"
    assert not set(first) & set(second)
    assert first == written(run_ids[0]), "keys are not a function of the job"


@SETTINGS
@given(closed=st.booleans(), changed=st.booleans(), vouched=st.booleans(), failed=st.booleans())
def test_only_a_closed_changed_vouched_issue_keeps_its_candidate(
    *, closed: bool, changed: bool, vouched: bool, failed: bool
) -> None:

    judge = (
        InvocationRef(
            session_id=SessionId(root="s"), invocation_id=InvocationId(root="i"), generation=0
        )
        if vouched
        else None
    )
    closing = Closing(
        closed=closed,
        candidate=BASE if changed else None,
        judge=judge,
        failure=Failure(kind=FailureKind.BOARD, reason="x") if failed else None,
    )
    settled = assessment_of(closing)

    keeps = closed and changed and vouched and not failed
    assert (settled.retention == "candidate") == keeps
    assert settled.eligible == keeps
    assert (settled.outcome == "succeeded") == (closed and not failed)
