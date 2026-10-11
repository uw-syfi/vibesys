"""The open job's decisions: from opening its attempt to settling it.

A job is one attempt: working one issue (implement, record, review, file the verdict) or one
performance evaluation. `advance` proposes the one decision the job's current phase needs and
moves the job to the awaiting step; the events folded in `_job_events` move it on. Lifecycle
facts such as whether a turn is dispatched or a slot is held stay in core.
"""

from vibesys.candidates.api import STOPPED
from vibesys.orchestration.issue_queue.strategy import _board as board
from vibesys.orchestration.issue_queue.strategy._draft import (
    Draft,
    change_key,
    key_of,
    operation,
    render_id,
    render_request,
    request_turn,
    subject_of,
    turn_id,
)
from vibesys.orchestration.issue_queue.strategy._ids import IDS, attempt_ref, item_id
from vibesys.orchestration.issue_queue.strategy._prompts import (
    ImplementPrompt,
    PerformancePrompt,
    PriorPerformance,
    PromptContext,
    ReviewPrompt,
)
from vibesys.orchestration.issue_queue.strategy._schemas import (
    IMPLEMENTER_REPLY,
    JUDGE_REPLY,
    PERF_REPLY,
)
from vibesys.orchestration.issue_queue.strategy._state import (
    Job,
    JobKind,
    Phase,
    Role,
    Step,
    TurnRecord,
)
from vs_core.api import (
    Access,
    AssessmentKind,
    AssessmentProposal,
    AttemptBudget,
    DecisionId,
    RevisionRef,
    SchemaRef,
    Settle,
    StartAttempt,
    TurnShape,
    Withdraw,
    WorkspaceMode,
    WorkspacePlan,
    attempt_scope,
    run_scope,
    workspace_for,
)
from vs_issue_tracker.api import (
    ApplyIssueChanges,
    CreateIssue,
    IssueChange,
    IssueRow,
    IssueStatus,
    LogProgress,
    RecordAttempt,
    SetIssueStatus,
    TextBody,
)


class NoTurnPhaseError(ValueError):
    """A turn was asked for in a phase that talks to no role."""

    def __init__(self, phase: Phase) -> None:
        super().__init__(f"phase {phase.value!r} has no turn")


class MissingIssueError(ValueError):
    """An issue job holds no issue, which its constructor never allows."""

    def __init__(self, job: Job) -> None:
        super().__init__(f"{job.kind.value} job {job.sequence} holds no issue")


def issue_of(job: Job) -> IssueRow:
    """The issue an issue job works on."""
    if job.issue is None:
        raise MissingIssueError(job)
    return job.issue


_BOARD_PHASES = (Phase.CLAIM, Phase.RECORD, Phase.FILE, Phase.BLOCK)
_NOTE_LIMIT = 200
_FEEDBACK_LIMIT = 500


def role_of(job: Job) -> Role | None:
    """The agent role the job's phase talks to, or None for a phase without a turn."""
    if job.phase is Phase.WORK:
        return Role.IMPLEMENTER if job.kind is JobKind.ISSUE else Role.PERF
    if job.phase is Phase.REVIEW:
        return Role.JUDGE
    return None


def advance(draft: Draft) -> None:
    """Propose the open job's next decision and store the job it leaves behind."""
    job = draft.state.job
    if job is None or job.phase is Phase.DONE:
        return
    if draft.state.stopping and job.failure != STOPPED and job.phase is not Phase.SETTLE:
        job = _stopped(job)
        if job.phase is Phase.DONE:
            draft.update(job=job)
            return
    waits = job.step in (Step.SUSPENDED, Step.RETAINING) or (
        job.step is Step.AWAITING and job.phase is not Phase.SETTLE
    )
    if waits:
        draft.update(job=job)
        return
    if role_of(job) is not None:
        job = _turn(draft, job)
    elif job.phase is Phase.START:
        job = _start(draft, job)
    elif job.phase is Phase.SETTLE:
        job = _settle(draft, job)
    else:
        job = _write(draft, job)
    draft.update(job=job)


def _stopped(job: Job) -> Job:
    """An operator stop ends an unstarted job at once and settles a started one."""
    if job.phase is Phase.START and job.step is Step.AWAITING:
        return job  # the start is in flight; AttemptReady will settle it
    if not job.ready:
        return job.model_copy(update={"phase": Phase.DONE, "failure": STOPPED, "step": Step.NEEDED})
    return job.model_copy(
        update={"phase": Phase.SETTLE, "failure": STOPPED, "step": Step.NEEDED, "awaiting": None}
    )


def _start(draft: Draft, job: Job) -> Job:
    if job.step is not Step.NEEDED:
        return job
    config = draft.config
    identifier = IDS.decision_id("start", key_of(job))
    paid = config.max_attempts_per_issue if job.kind is JobKind.ISSUE else 1
    # A logical turn may be corrected or asked again this often; an issue has a turn for the
    # implementer and one for the judge for each of its attempts.
    turns = 2 * paid
    draft.emit(
        StartAttempt(
            decision_id=identifier,
            scope=run_scope(draft.view),
            attempt_id=job.attempt,
            item_id=item_id(job.kind.value, job.sequence),
            workspace=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=job.base),
            budget=AttemptBudget(
                admission_charge=1,
                paid_invocation_limit=paid,
                retry_limit=(config.max_corrections + config.max_turn_drops) * turns,
            ),
        )
    )
    return job.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _first_turn(job: Job, role: Role) -> TurnRecord:
    return TurnRecord(
        role=role, serial=job.next_serial, charge="paid" if role is Role.IMPLEMENTER else "free"
    )


def _context(draft: Draft, job: Job, role: Role) -> PromptContext:
    issues = draft.state.board.issues
    if role is Role.IMPLEMENTER:
        return ImplementPrompt(
            issue=board.detail(issue_of(job)),
            board=board.briefs(issues),
            feedback=job.feedback,
            analysis=job.analysis,
        )
    if role is Role.JUDGE:
        return ReviewPrompt(
            issue=board.detail(issue_of(job)),
            board=board.briefs(issues),
            summary=job.summary,
            files_touched=job.files_touched,
            self_check=job.self_check,
        )
    return PerformancePrompt(
        iteration=job.iteration,
        max_new_issues=draft.config.max_issues_per_perf_eval,
        board=board.briefs(issues),
        prior=tuple(
            PriorPerformance(
                iteration=record.iteration,
                throughput_trend=record.throughput_trend,
                latency_trend=record.latency_trend,
                metrics_json=record.metrics_json,
                new_issues=len(record.new_issues),
            )
            for record in draft.state.performance
        ),
        load_levels_json=draft.config.load_levels_json,
    )


def _turn_shape(
    draft: Draft, job: Job, role: Role
) -> tuple[WorkspaceMode, Access, SchemaRef, float, RevisionRef]:
    config = draft.config
    if role is Role.IMPLEMENTER:
        return (
            WorkspaceMode.ISOLATED_CHILD,
            Access.WRITE_CANDIDATE,
            IMPLEMENTER_REPLY,
            config.implementer_turn_seconds,
            job.base,
        )
    if role is Role.PERF:
        return (
            WorkspaceMode.ISOLATED_CHILD,
            Access.WRITE_CANDIDATE,
            PERF_REPLY,
            config.perf_turn_seconds,
            job.base,
        )
    return (
        WorkspaceMode.READ_ONLY_REVISION,
        Access.READ_ONLY,
        JUDGE_REPLY,
        config.judge_turn_seconds,
        job.candidate or job.base,
    )


def _turn(draft: Draft, job: Job) -> Job:
    role = role_of(job)
    if role is None:
        raise NoTurnPhaseError(job.phase)
    turn = job.turn or _first_turn(job, role)
    subject = subject_of(job, role)
    scope = attempt_scope(draft.view, job.attempt, job.generation)
    if job.step is Step.NEEDED:
        if not draft.due(turn):
            return job
        body = turn.context or _context(draft, job, role)
        identifier = render_id(subject, turn)
        draft.emit(operation(draft, identifier, scope, render_request(subject, turn, body)))
        return job.model_copy(
            update={
                "turn": turn,
                "step": Step.RENDERING,
                "awaiting": identifier,
                "next_serial": max(job.next_serial, turn.serial + 1),
            }
        )
    if job.step is not Step.RENDERED:
        return job
    mode, access, schema, seconds, revision = _turn_shape(draft, job, role)
    draft.emit(
        request_turn(
            draft,
            TurnShape(
                role=role.value,
                subject=subject,
                workspace=workspace_for(scope, revision, mode),
                access=access,
                output_schema=schema,
                seconds=seconds,
            ),
            turn,
            scope,
        )
    )
    return job.model_copy(
        update={
            "step": Step.AWAITING,
            "awaiting": turn_id(subject, turn),
            "turns_spent": job.turns_spent + (1 if turn.charge == "paid" else 0),
        }
    )


def _write_id(job: Job) -> DecisionId:
    """Decision identity of the job's current board write, distinct for each retry."""
    return IDS.decision_id(
        "apply", f"{key_of(job)}.{job.phase.value}.{job.turns_spent}", job.write_retries
    )


def _creates(draft: Draft, job: Job, created_by: str) -> tuple[CreateIssue, ...]:
    return tuple(
        CreateIssue(
            key=change_key(draft.view, job, f"filed.{job.turns_spent}.{index}"),
            type=filed.type,
            title=filed.title,
            body=TextBody(text=filed.description),
            created_by=created_by,
            iteration=job.iteration,
        )
        for index, filed in enumerate(job.filed)
    )


type Writes = tuple[tuple[IssueChange, ...], tuple[LogProgress, ...]]


def _claim(draft: Draft, job: Job) -> Writes:
    claim = SetIssueStatus(
        key=change_key(draft.view, job, "claim"),
        issue_id=issue_of(job).id,
        status=IssueStatus.IN_PROGRESS,
        actor="loop",
        iteration=job.iteration,
        note="claimed for processing",
    )
    return (claim,), ()


def _record(draft: Draft, job: Job) -> Writes:
    issue_id = issue_of(job).id
    record = RecordAttempt(
        key=change_key(draft.view, job, f"attempt.{job.turns_spent}"),
        issue_id=issue_id,
        actor="implementer",
        iteration=job.iteration,
        note=job.summary[:_NOTE_LIMIT],
        payload_json=job.reply_json,
    )
    log = LogProgress(
        key=change_key(draft.view, job, f"log.implement.{job.turns_spent}"),
        iteration=job.iteration,
        step="implement",
        issue_id=issue_id,
        payload_json=job.reply_json,
    )
    return (record,), (log,)


def _block(draft: Draft, job: Job) -> Writes:
    block = SetIssueStatus(
        key=change_key(draft.view, job, "block"),
        issue_id=issue_of(job).id,
        status=IssueStatus.BLOCKED,
        actor="loop",
        iteration=job.iteration,
        note=f"exhausted {draft.config.max_attempts_per_issue} attempts",
    )
    return (block,), ()


def _verdict(draft: Draft, job: Job) -> Writes:
    issue_id = issue_of(job).id
    passed = bool(job.review_passed)
    verdict = SetIssueStatus(
        key=change_key(draft.view, job, f"verdict.{job.turns_spent}"),
        issue_id=issue_id,
        status=IssueStatus.CLOSED if passed else IssueStatus.IN_PROGRESS,
        actor="judge",
        iteration=job.iteration,
        note=(
            f"closed by judge after attempt {job.turns_spent}"
            if passed
            else (job.feedback or "")[:_FEEDBACK_LIMIT]
        ),
        payload_json=job.reply_json,
    )
    log = LogProgress(
        key=change_key(draft.view, job, f"log.review.{job.turns_spent}"),
        iteration=job.iteration,
        step="review",
        issue_id=issue_id,
        payload_json=job.reply_json,
    )
    return (verdict, *_creates(draft, job, "judge")), (log,)


def _filing(draft: Draft, job: Job) -> Writes:
    log = LogProgress(
        key=change_key(draft.view, job, "log.performance"),
        iteration=job.iteration,
        step="performance",
        issue_id=None,
        payload_json=job.reply_json,
    )
    return _creates(draft, job, "perf_eval"), (log,)


def _changes(draft: Draft, job: Job) -> Writes:
    """The keyed changes and progress entries a board-write phase makes."""
    if job.phase is Phase.CLAIM:
        return _claim(draft, job)
    if job.phase is Phase.RECORD:
        return _record(draft, job)
    if job.phase is Phase.BLOCK:
        return _block(draft, job)
    return _verdict(draft, job) if job.kind is JobKind.ISSUE else _filing(draft, job)


def _write(draft: Draft, job: Job) -> Job:
    """Send the job's board write; a phase with nothing to change moves straight on."""
    if job.step is not Step.NEEDED or job.phase not in _BOARD_PHASES:
        return job
    changes, logs = _changes(draft, job)
    scope = run_scope(draft.view)
    for entry in logs:
        identifier = IDS.decision_id("log", f"{key_of(job)}.{job.phase.value}.{job.turns_spent}")
        draft.emit(operation(draft, identifier, scope, entry))
    if not changes:
        return job.model_copy(update={"step": Step.NEEDED, "awaiting": None, "phase": Phase.SETTLE})
    identifier = _write_id(job)
    draft.emit(operation(draft, identifier, scope, ApplyIssueChanges(changes=changes)))
    return job.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def settle_for(job: Job) -> Settle:
    """The assessments, eligibility and retention to propose for a finished job.

    A closed issue whose turn changed the tree is a retained, eligible candidate vouched for
    by the judge, and the next job builds on it. Nothing else is kept: an issue that closed
    without a change, a blocked or failed one, and a performance evaluation settle discarded.
    """
    if job.failure == STOPPED:
        return Settle(assessments=(), eligible=False, retention="discard", outcome="cancelled")
    if (
        job.kind is JobKind.ISSUE
        and job.closed
        and job.candidate is not None
        and job.judge_invocation is not None
    ):
        return Settle(
            assessments=(
                AssessmentProposal(
                    kind=AssessmentKind.LOCAL_VALIDATION,
                    verdict="satisfied",
                    sources=(job.judge_invocation,),
                    candidate=job.candidate,
                    schema_version=1,
                ),
            ),
            eligible=True,
            retention="candidate",
            outcome="succeeded",
            candidate=job.candidate,
        )
    failed = job.failure is not None or (job.kind is JobKind.ISSUE and not job.closed)
    return Settle(
        assessments=(),
        eligible=False,
        retention="discard",
        outcome="failed" if failed else "succeeded",
    )


def _settle(draft: Draft, job: Job) -> Job:
    if job.settle_sent:
        return job
    identifier = IDS.decision_id("settle", key_of(job))
    draft.emit(
        Withdraw(
            decision_id=identifier,
            scope=run_scope(draft.view),
            target=attempt_ref(job.attempt, job.generation),
            disposition=settle_for(job),
        )
    )
    return job.model_copy(
        update={"step": Step.AWAITING, "awaiting": identifier, "settle_sent": True}
    )


__all__ = ["advance", "role_of", "settle_for"]
