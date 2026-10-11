"""Between jobs: read the board, bootstrap it, pick the next issue, assess, and end the run.

The legacy loop drained the board in a `while` and queried the tracker whenever it needed a
fact. Here the loop is a cursor over the last board snapshot: every decision that depends on
the board is made from a fresh read, and a write makes the snapshot stale. The stop decision
of a run (a blocked queue, a quiet board, an exhausted round budget) is a function of the
snapshot, so it is the same on every replay.
"""

from typing import Literal

from vibesys.orchestration.issue_queue.strategy import _board as board
from vibesys.orchestration.issue_queue.strategy import _job as job_decisions
from vibesys.orchestration.issue_queue.strategy._draft import Draft, change_key, operation
from vibesys.orchestration.issue_queue.strategy._ids import IDS, attempt_id
from vibesys.orchestration.issue_queue.strategy._operations import ReadBoard
from vibesys.orchestration.issue_queue.strategy._state import (
    BoardState,
    Cursor,
    Ending,
    IssueQueueStrategyState,
    Job,
    JobKind,
    Pending,
    Step,
)
from vs_core.api import OperationResult, RevisionRef, RunView, run_scope
from vs_issue_tracker.api import (
    AppliedChanges,
    ApplyIssueChanges,
    BoardSnapshot,
    CreateIssue,
    IssueType,
    TemplateBody,
)

BOOTSTRAP_TITLE = "Build inference server for the reference model"
BOOTSTRAP_TEMPLATE = "bootstrap_description"


def tip_revision(state: IssueQueueStrategyState, view: RunView) -> RevisionRef:
    """The revision the next job builds on: the last closed issue's candidate, else the input."""
    return state.tip.revision or view.facts.baseline


def decide(draft: Draft) -> None:
    """Take the one step the cursor needs, when no job is open and the run is not ending."""
    state = draft.state
    if state.job is not None or state.stopping or state.ending is not None:
        return
    if state.cursor is Cursor.START:
        _start(draft)
    elif state.cursor is Cursor.DRAIN:
        _drain(draft)
    elif state.cursor is Cursor.ASSESS:
        _assess(draft)
    else:
        _after_perf(draft)


def _finish(draft: Draft, outcome: Literal["success", "failure"], reason: str) -> None:
    draft.update(ending=Ending(outcome=outcome, reason=reason))


def _fresh(draft: Draft) -> bool:
    """Whether the cached board may be decided from; otherwise a read is sent or awaited."""
    current = draft.state.board
    if current.fresh:
        return True
    if current.read.step is Step.AWAITING:
        return False
    identifier = IDS.decision_id("read", "board", current.reads)
    draft.emit(operation(draft, identifier, run_scope(draft.view), ReadBoard()))
    read = Pending(step=Step.AWAITING, awaiting=identifier, retries=current.read.retries)
    draft.update(board=current.model_copy(update={"read": read, "reads": current.reads + 1}))
    return False


def _start(draft: Draft) -> None:
    if not _fresh(draft):
        return
    current = draft.state.board
    if current.bootstrap_done:
        draft.update(cursor=Cursor.DRAIN)
        _drain(draft)
        return
    if current.bootstrap.step is Step.AWAITING:
        return
    identifier = IDS.decision_id("apply", "bootstrap", current.bootstrap.retries)
    create = CreateIssue(
        key=change_key(draft.view, None, "bootstrap"),
        type=IssueType.FEATURE,
        title=BOOTSTRAP_TITLE,
        body=TemplateBody(template=BOOTSTRAP_TEMPLATE),
        created_by="loop:bootstrap",
        iteration=draft.state.round_idx + 1,
    )
    draft.emit(
        operation(draft, identifier, run_scope(draft.view), ApplyIssueChanges(changes=(create,)))
    )
    bootstrap = Pending(step=Step.AWAITING, awaiting=identifier, retries=current.bootstrap.retries)
    draft.update(board=current.model_copy(update={"bootstrap": bootstrap}))


def _open(draft: Draft, kind: JobKind, issue_id: int | None = None) -> None:
    state = draft.state
    issue = None if issue_id is None else next(i for i in state.board.issues if i.id == issue_id)
    sequence = state.jobs_started + 1
    job = Job(
        attempt=attempt_id(kind.value, sequence),
        kind=kind,
        sequence=sequence,
        iteration=state.round_idx + 1,
        issue=issue,
        base=tip_revision(state, draft.view),
    )
    draft.update(job=job, jobs_started=state.jobs_started + 1)
    job_decisions.advance(draft)


def _drain(draft: Draft) -> None:
    if not _fresh(draft):
        return
    state = draft.state
    config = draft.config
    if state.round_idx >= config.max_rounds:
        _finish(draft, "failure", f"the round budget of {config.max_rounds} rounds is spent")
        return
    pick = (
        None
        if state.processed >= config.max_issues_per_round
        else board.next_open(state.board.issues)
    )
    if pick is None:
        draft.update(cursor=Cursor.ASSESS)
        _assess(draft)
        return
    _open(draft, JobKind.ISSUE, pick.id)


def _assess(draft: Draft) -> None:
    if not _fresh(draft):
        return
    issues = draft.state.board.issues
    if board.blocked_queue(issues):
        count = len(board.remaining(issues))
        _finish(draft, "failure", f"all {count} remaining issues are blocked")
        return
    _open(draft, JobKind.PERF)


def _after_perf(draft: Draft) -> None:
    if not _fresh(draft):
        return
    state = draft.state
    last = state.performance[-1] if state.performance else None
    filed = last is not None and last.iteration == state.round_idx + 1 and bool(last.new_issues)
    if not board.has_open(state.board.issues) and not filed:
        _finish(draft, "success", "the board is drained and the last evaluation filed nothing")
        return
    draft.update(cursor=Cursor.DRAIN, round_idx=state.round_idx + 1, processed=0)
    _drain(draft)


def on_snapshot(state: IssueQueueStrategyState, snapshot: BoardSnapshot) -> IssueQueueStrategyState:
    """A board read answered: cache it. A read that failed leaves the cache stale."""
    current = state.board
    if snapshot.status != "succeeded":
        return state.model_copy(
            update={
                "board": current.model_copy(update={"read": Pending(retries=current.read.retries)})
            }
        )
    fresh = BoardState(
        issues=snapshot.issues,
        fresh=True,
        read=Pending(),
        reads=current.reads,
        bootstrap_done=current.bootstrap_done,
        bootstrap=current.bootstrap,
    )
    return state.model_copy(update={"board": fresh})


def on_bootstrap(
    state: IssueQueueStrategyState, outcome: AppliedChanges
) -> IssueQueueStrategyState:
    """The bootstrap issue's write answered: the board is stale and the run begins draining."""
    current = state.board
    if outcome.status != "succeeded":
        reason = f"the bootstrap issue could not be filed: {outcome.status}: {outcome.detail}"
        return state.model_copy(update={"ending": Ending(outcome="failure", reason=reason)})
    board_state = current.model_copy(
        update={"bootstrap_done": True, "bootstrap": Pending(), "fresh": False}
    )
    return state.model_copy(update={"board": board_state, "cursor": Cursor.DRAIN})


def on_operation(
    state: IssueQueueStrategyState, event: OperationResult, decision: str | None
) -> IssueQueueStrategyState:
    """Fold the answer of a board request the run itself sent, or leave the state unchanged."""
    current = state.board
    outcome = event.outcome
    reading = current.read.awaiting
    if isinstance(outcome, BoardSnapshot) and reading is not None and reading.root == decision:
        return on_snapshot(state, outcome)
    filing = current.bootstrap.awaiting
    if isinstance(outcome, AppliedChanges) and filing is not None and filing.root == decision:
        return on_bootstrap(state, outcome)
    return state


def retry_blocked(
    state: IssueQueueStrategyState, blocked: frozenset[str | None], max_retries: int
) -> IssueQueueStrategyState:
    """Send a board request again once its effect stayed unknown, or end the run if it keeps failing.

    A request core blocked stays blocked in the view; the request it is sent again as has a
    new decision identity and the same keys, so a write that did land is not repeated.
    """
    current = state.board
    reading = current.read.awaiting
    if reading is not None and reading.root in blocked:
        if current.read.retries >= max_retries:
            return _unavailable(state, "reading the board")
        read = Pending(retries=current.read.retries + 1)
        return state.model_copy(update={"board": current.model_copy(update={"read": read})})
    filing = current.bootstrap.awaiting
    if filing is not None and filing.root in blocked:
        if current.bootstrap.retries >= max_retries:
            return _unavailable(state, "filing the bootstrap issue")
        bootstrap = Pending(retries=current.bootstrap.retries + 1)
        board_state = current.model_copy(update={"bootstrap": bootstrap})
        return state.model_copy(update={"board": board_state})
    return state


def on_rejected(
    state: IssueQueueStrategyState, decision: str, detail: str
) -> IssueQueueStrategyState | None:
    """A board request the run itself sent was refused: the tracker cannot be used."""
    current = state.board
    for pending in (current.read, current.bootstrap):
        if pending.awaiting is not None and pending.awaiting.root == decision:
            return _unavailable(state, detail)
    return None


def _unavailable(state: IssueQueueStrategyState, doing: str) -> IssueQueueStrategyState:
    reason = f"the issue tracker is unavailable while {doing}"
    return state.model_copy(update={"ending": Ending(outcome="failure", reason=reason)})
