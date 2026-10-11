"""Event handlers of the open job: fold each core feedback into it.

Handlers never propose decisions; they move the job to the phase and step whose decision
`_job.advance` proposes next. Duplicate or late events find no matching awaiting job and leave
the state unchanged.
"""

import json
from collections.abc import Sequence
from typing import Literal

from pydantic import ValidationError

from vibesys.candidates.api import STOPPED
from vibesys.orchestration.issue_queue.models import (
    ImplementerReplyModel,
    IssueImplementerResult,
    IssueJudgeResult,
    IssuePerfEvalResult,
    JudgeReplyModel,
    NewBug,
    NewIssue,
    perf_result_model,
)
from vibesys.orchestration.issue_queue.strategy import _ids as ids
from vibesys.orchestration.issue_queue.strategy._config import IssueQueueConfig
from vibesys.orchestration.issue_queue.strategy._draft import invocation_of, subject_of
from vibesys.orchestration.issue_queue.strategy._job import role_of
from vibesys.orchestration.issue_queue.strategy._prompts import (
    ReplyCorrectionPrompt,
    ResumePrompt,
    WaitUnrecordedPrompt,
)
from vibesys.orchestration.issue_queue.strategy._state import (
    Cursor,
    Ending,
    FiledIssue,
    IssueQueueStrategyState,
    Job,
    JobKind,
    PerformanceRecord,
    Phase,
    Role,
    Step,
    Tip,
)
from vs_core.api import (
    AttemptExhausted,
    AttemptReady,
    AttemptSettled,
    IntentBlocked,
    InvocationRef,
    ObservationStatus,
    OperationResult,
    Rejected,
    RenderedArtifacts,
    ResumeAuthorized,
    RevisionRef,
    RunView,
    TurnFailureKind,
    TurnResult,
    TurnSuspended,
)
from vs_issue_tracker.api import AppliedChanges, IssueType

_ERROR_LIMIT = 2000
_WRITE_PHASES = (Phase.CLAIM, Phase.RECORD, Phase.FILE, Phase.BLOCK)
_ROLE_NAME: dict[Role, Literal["implementer", "judge", "perf-evaluator"]] = {
    Role.IMPLEMENTER: "implementer",
    Role.JUDGE: "judge",
    Role.PERF: "perf-evaluator",
}
type Result = IssueImplementerResult | IssueJudgeResult | IssuePerfEvalResult


def _with(state: IssueQueueStrategyState, job: Job) -> IssueQueueStrategyState:
    return state.model_copy(update={"job": job})


def _awaiting(state: IssueQueueStrategyState, decision: str | None) -> Job | None:
    job = state.job
    if job is None or job.phase is Phase.DONE or not job.awaits(decision):
        return None
    return job


def retire(state: IssueQueueStrategyState) -> IssueQueueStrategyState:
    """Clear a finished job and move the run to what follows it.

    A finished issue counts toward the round's cap and a finished evaluation closes the
    round's work; either makes the board stale. A job that failed ends the run: its issue is
    left as the board last recorded it, and nothing is picked up again that could fail the
    same way.
    """
    job = state.job
    if job is None:
        return state
    ending = state.ending
    if job.failure is not None and job.failure != STOPPED and ending is None:
        ending = _failed_ending(job)
    cursor = Cursor.AFTER_PERF if job.kind is JobKind.PERF else state.cursor
    processed = state.processed + (1 if job.kind is JobKind.ISSUE else 0)
    board = state.board.model_copy(update={"fresh": False})
    return state.model_copy(
        update={
            "job": None,
            "cursor": cursor,
            "processed": processed,
            "board": board,
            "ending": ending,
        }
    )


def _failed_ending(job: Job) -> Ending:
    subject = (
        f"issue #{job.issue.id}"
        if job.issue is not None
        else f"performance evaluation {job.iteration}"
    )
    return Ending(outcome="failure", reason=f"{subject} could not be completed: {job.failure}")


def fail(state: IssueQueueStrategyState, reason: str) -> IssueQueueStrategyState:
    """End the open job with a typed failure; an admitted attempt must still settle."""
    job = state.job
    if job is None or job.phase is Phase.DONE:
        return state
    failed = job.failed(reason)
    if failed.owes_settlement:
        return _with(state, failed.model_copy(update={"phase": Phase.SETTLE}))
    # An attempt core never admitted has nothing to settle.
    return retire(_with(state, failed.model_copy(update={"phase": Phase.DONE})))


def on_rejected(state: IssueQueueStrategyState, event: Rejected) -> IssueQueueStrategyState:
    """A decision core refused fails the job awaiting it."""
    if _awaiting(state, event.decision_id.root) is None:
        return state
    return fail(state, f"decision rejected: {event.code.value}: {event.detail}")


def on_ready(state: IssueQueueStrategyState, event: AttemptReady) -> IssueQueueStrategyState:
    """The attempt holds a slot: begin the job's work, or settle it if a stop raced."""
    job = state.job
    if job is None or job.attempt != event.attempt.attempt_id or job.phase is not Phase.START:
        return state
    admitted = job.admitted(event.attempt.generation)
    if state.stopping:
        update = {"phase": Phase.SETTLE, "step": Step.NEEDED, "failure": STOPPED}
    else:
        phase = Phase.CLAIM if job.kind is JobKind.ISSUE else Phase.WORK
        update = {"phase": phase, "step": Step.NEEDED, "turn": None}
    return _with(state, admitted.model_copy(update=update))


def retry_blocked(
    state: IssueQueueStrategyState, blocked: frozenset[str | None], config: IssueQueueConfig
) -> IssueQueueStrategyState:
    """Send the job's board write again once its effect stayed unknown, or fail the job.

    A write core blocked stays blocked in the view; the request it is sent again as has a new
    decision identity and the same keys, so a write that did land is not repeated.
    """
    job = state.job
    if job is None or job.phase not in _WRITE_PHASES or job.awaiting is None:
        return state
    if job.awaiting.root not in blocked:
        return state
    if job.write_retries >= config.max_board_retries:
        return fail(state, f"a board request stayed unknown (sent {job.write_retries + 1} times)")
    return _with(
        state,
        job.model_copy(
            update={"write_retries": job.write_retries + 1, "step": Step.NEEDED, "awaiting": None}
        ),
    )


def on_blocked(state: IssueQueueStrategyState, event: IntentBlocked) -> IssueQueueStrategyState:
    """A blocked request of the job's attempt leaves its effect unknown: end the job.

    A board write is run-scoped and is retried from the view instead (`retry_blocked`).
    """
    job = state.job
    if job is None or job.phase is Phase.DONE:
        return state
    detail = f"request blocked: {event.diagnostic}"
    decision = ids.decision_of_operation(event.target.root)
    attempt_owned = event.scope.owner.kind == "attempt" and event.scope.owner == job.attempt
    awaiting = job.awaits(decision) or (
        attempt_owned and event.scope.generation == job.generation and job.step is Step.AWAITING
    )
    return fail(state, detail) if awaiting else state


def _rendered(
    state: IssueQueueStrategyState, job: Job, outcome: RenderedArtifacts
) -> IssueQueueStrategyState:
    if outcome.status != "succeeded" or not outcome.prompts or job.turn is None:
        return fail(state, f"prompt render {outcome.status}")
    turn = job.turn.with_prompts(outcome.prompts, outcome.tool_policy)
    return _with(
        state, job.model_copy(update={"turn": turn, "step": Step.RENDERED, "awaiting": None})
    )


def _after_write(job: Job, outcome: AppliedChanges, config: IssueQueueConfig) -> dict[str, object]:
    """The job fields a landed board write changes: the phase that follows it, and what it made."""
    if job.phase is Phase.CLAIM:
        return {"phase": Phase.WORK}
    if job.phase is Phase.RECORD:
        return {"phase": Phase.REVIEW}
    if job.phase is Phase.BLOCK:
        return {"phase": Phase.SETTLE}
    if job.kind is JobKind.PERF:
        return {
            "phase": Phase.SETTLE,
            "created": tuple(result.issue_id for result in outcome.results),
        }
    if job.review_passed:
        return {"phase": Phase.SETTLE, "closed": True}
    spent = job.turns_spent >= config.max_attempts_per_issue
    return {"phase": Phase.BLOCK if spent else Phase.WORK}


def _written(
    state: IssueQueueStrategyState, job: Job, outcome: AppliedChanges, config: IssueQueueConfig
) -> IssueQueueStrategyState:
    """A board write landed: move the job to the phase after it."""
    if outcome.status != "succeeded":
        return fail(state, f"board write {outcome.status}: {outcome.detail}")
    base: dict[str, object] = {
        "step": Step.NEEDED,
        "awaiting": None,
        "write_retries": 0,
        "turn": None,
    }
    return _with(state, job.model_copy(update={**base, **_after_write(job, outcome, config)}))


def on_operation(
    state: IssueQueueStrategyState, event: OperationResult, config: IssueQueueConfig
) -> IssueQueueStrategyState:
    """Route a registered operation's typed outcome to the job that awaits it."""
    job = _awaiting(state, ids.decision_of_operation(event.operation_id.root))
    if job is None:
        return state
    outcome = event.outcome
    if isinstance(outcome, RenderedArtifacts):
        return _rendered(state, job, outcome)
    if isinstance(outcome, AppliedChanges):
        return _written(state, job, outcome, config)
    return state


def _turn_job(state: IssueQueueStrategyState, invocation_id: str) -> Job | None:
    job = state.job
    if job is None or job.step is not Step.AWAITING or job.turn is None:
        return None
    role = role_of(job)
    if role is None or invocation_id != invocation_of(role, subject_of(job, role), job.turn):
        return None
    return job


def owns_turn(state: IssueQueueStrategyState, event: TurnResult) -> bool:
    """Whether ``event`` answers the open job's outstanding turn."""
    return _turn_job(state, event.invocation.invocation_id.root) is not None


def _decode(role: Role, payload: str, config: IssueQueueConfig) -> object:
    """The reply as its typed result or wait, or the schema violation as text."""
    try:
        if role is Role.IMPLEMENTER:
            return ImplementerReplyModel.model_validate_json(payload).root
        if role is Role.JUDGE:
            return JudgeReplyModel.model_validate_json(payload).root
        return perf_result_model(config.max_issues_per_perf_eval).model_validate_json(payload)
    except ValidationError as error:
        return str(error)[:_ERROR_LIMIT]


def _correct(
    state: IssueQueueStrategyState,
    config: IssueQueueConfig,
    error: str | None,
    invalid: InvocationRef,
) -> IssueQueueStrategyState | None:
    """Ask the role to fix its reply; the correction names the invalid turn as its predecessor.

    ``error`` is the schema violation to name, or None when the reply was valid but asked to
    wait and the run recorded no wait.
    """
    job = state.job
    role = None if job is None else role_of(job)
    if job is None or job.turn is None or role is None:
        return None
    corrected = job.turn.corrected(
        invalid,
        WaitUnrecordedPrompt()
        if error is None
        else ReplyCorrectionPrompt(role=_ROLE_NAME[role], error=error),
        limit=config.max_corrections,
    )
    if corrected is None:
        return None
    return _with(
        state, job.model_copy(update={"turn": corrected, "step": Step.NEEDED, "awaiting": None})
    )


def _ask_again(
    state: IssueQueueStrategyState, config: IssueQueueConfig, event: TurnResult, now: float
) -> IssueQueueStrategyState:
    """The transport lost the turn: ask the role again after a backoff, or fail the job.

    The new turn is a correction of the lost one (it spends no paid turn), asks the same
    question after the drop backoff on the run clock, and counts against ``max_turn_drops``
    for this logical turn. A resume cannot be asked again: core authorizes each
    continuation's resume once.
    """
    job = state.job
    role = None if job is None else role_of(job)
    reason = f"{role.value if role else 'agent'} turn was lost ({event.detail or 'transport'})"
    if job is None or job.turn is None or role is None:
        return fail(state, reason)
    again = job.turn.asked_again(
        event.invocation,
        now=now,
        limit=config.max_turn_drops,
        backoff=config.drop_backoff(job.turn.drops),
    )
    if again is None:
        return fail(state, reason)
    return _with(
        state, job.model_copy(update={"turn": again, "step": Step.NEEDED, "awaiting": None})
    )


def _yielded(state: IssueQueueStrategyState, view: RunView) -> IssueQueueStrategyState:
    """The reply asked to wait: only core's `TurnSuspended` completes the yield."""
    job = state.job
    if job is None or job.turn is None:
        return state
    if "suspend" not in view.capabilities.lifecycle:
        return fail(state, "the run offers no suspend capability")
    return _with(state, job.model_copy(update={"turn": job.turn.yielding()}))


def on_suspended(state: IssueQueueStrategyState, event: TurnSuspended) -> IssueQueueStrategyState:
    """Core recorded the continuation: wait for `ResumeAuthorized` of exactly that ID."""
    continuation = event.continuation
    job = _turn_job(state, continuation.invocation.invocation_id.root)
    if job is None or job.turn is None:
        return state
    turn = job.turn.suspended(continuation.invocation, continuation.continuation_id)
    return _with(
        state, job.model_copy(update={"turn": turn, "step": Step.SUSPENDED, "awaiting": None})
    )


def on_resume(state: IssueQueueStrategyState, event: ResumeAuthorized) -> IssueQueueStrategyState:
    """Authorize the suspended turn's resume with the invocation core named."""
    job = state.job
    if (
        job is None
        or job.step is not Step.SUSPENDED
        or job.turn is None
        or job.turn.continuation != event.continuation_id
    ):
        return state
    role = role_of(job)
    if role is None:
        return state
    resumed = job.turn.resumed(
        authorized=event.next_invocation.invocation_id,
        continuation=event.continuation_id,
        context=ResumePrompt(
            role=_ROLE_NAME[role],
            retained_revision=job.candidate,
            timed_out=event.timeout is not None,
            evidence=tuple(item.evidence_id for item in event.evidence),
            repeated_failure=None
            if event.repeated_failure is None
            else str(event.repeated_failure),
        ),
    )
    return _with(state, job.model_copy(update={"turn": resumed, "step": Step.NEEDED}))


def on_exhausted(
    state: IssueQueueStrategyState, event: AttemptExhausted
) -> IssueQueueStrategyState:
    """A spent budget ends the job; core grants no automatic retry."""
    job = state.job
    if job is None or job.attempt != event.attempt.attempt_id:
        return state
    return fail(state, f"attempt budget exhausted ({event.reason})")


def _files(filed: Sequence[NewBug | NewIssue]) -> tuple[FiledIssue, ...]:
    return tuple(
        FiledIssue(type=IssueType(item.type), title=item.title, description=item.description)
        for item in filed
    )


def _implemented(
    state: IssueQueueStrategyState,
    result: IssueImplementerResult,
    payload: str,
    candidate: RevisionRef | None,
) -> IssueQueueStrategyState:
    """The implementer answered: record the attempt, then review what it left."""
    job = state.job
    if job is None:
        return state
    changed = None if candidate == job.base else candidate
    return _with(
        state,
        job.model_copy(
            update={
                "candidate": changed,
                "summary": result.summary,
                "files_touched": result.files_touched,
                "self_check": result.self_check,
                "reply_json": payload,
                "phase": Phase.RECORD,
                "step": Step.NEEDED,
                "awaiting": None,
                "turn": None,
            }
        ),
    )


def _reviewed(
    state: IssueQueueStrategyState, result: IssueJudgeResult, payload: str, event: TurnResult
) -> IssueQueueStrategyState:
    job = state.job
    if job is None:
        return state
    passed = result.verdict == "pass"
    return _with(
        state,
        job.model_copy(
            update={
                "review_passed": passed,
                "judge_invocation": event.invocation,
                "feedback": None if passed else (result.feedback or result.analysis),
                "analysis": None if passed else result.analysis,
                "filed": _files(result.new_issues),
                "reply_json": payload,
                "phase": Phase.FILE,
                "step": Step.NEEDED,
                "awaiting": None,
                "turn": None,
            }
        ),
    )


def _evaluated(
    state: IssueQueueStrategyState, result: IssuePerfEvalResult, payload: str
) -> IssueQueueStrategyState:
    job = state.job
    if job is None:
        return state
    return _with(
        state,
        job.model_copy(
            update={
                "filed": _files(result.new_issues),
                "reply_json": payload,
                "throughput_trend": result.throughput_trend,
                "latency_trend": result.latency_trend,
                "metrics_json": json.dumps(result.metrics.model_dump(mode="json"), sort_keys=True),
                "phase": Phase.FILE,
                "step": Step.NEEDED,
                "awaiting": None,
                "turn": None,
            }
        ),
    )


def _accept(
    state: IssueQueueStrategyState, reply: IssueJudgeResult, payload: str, event: TurnResult
) -> IssueQueueStrategyState:
    return _reviewed(state, reply, payload, event)


def _written_turn(
    state: IssueQueueStrategyState,
    reply: IssueImplementerResult | IssuePerfEvalResult,
    payload: str,
    candidate: RevisionRef | None,
) -> IssueQueueStrategyState:
    """Accept the result of a turn that wrote to its workspace, once its checkpoint is known."""
    if isinstance(reply, IssueImplementerResult):
        return _implemented(state, reply, payload, candidate)
    return _evaluated(state, reply, payload)


def _candidate_of(view: RunView, job: Job, invocation: InvocationRef) -> RevisionRef | None:
    """The revision the reply's turn chain retained, by core's one rule (`TurnChain.candidate`)."""
    if job.turn is None:
        return None
    return job.turn.candidate(view, job.attempt, invocation)


def settle_retained(
    state: IssueQueueStrategyState, view: RunView, config: IssueQueueConfig
) -> IssueQueueStrategyState:
    """Fold the held reply of a write turn once core has retained or declined its checkpoint.

    Core delivers a turn's reply before the executor snapshots the workspace, so the
    candidate is readable from the view only later. Until then the reply stays held.
    """
    job = state.job
    if job is None or not job.holds_reply or job.held_invocation is None or job.held_reply is None:
        return state
    invocation = job.held_invocation
    live = next((item for item in view.attempts if item.attempt_id == job.attempt), None)
    candidate = _candidate_of(view, job, invocation)
    declined = live is not None and any(
        row.invocation == invocation for row in live.checkpoint_declines
    )
    if candidate is None and not declined:
        return state
    role = Role.IMPLEMENTER if job.kind is JobKind.ISSUE else Role.PERF
    reply = _decode(role, job.held_reply, config)
    held = _with(state, job.released())
    if isinstance(reply, IssueImplementerResult | IssuePerfEvalResult):
        return _written_turn(held, reply, job.held_reply, candidate)
    return held


def _unfinished(
    state: IssueQueueStrategyState, view: RunView, config: IssueQueueConfig, event: TurnResult
) -> IssueQueueStrategyState:
    """A turn ended without a usable reply: ask again, correct the reply, or fail the job."""
    job = state.job
    role = None if job is None else role_of(job)
    if role is None:
        return state
    if event.failure is TurnFailureKind.TRANSPORT_LOST:
        return _ask_again(state, config, event, view.run.now_at)
    reason = f"{role.value} turn did not complete ({event.observation.status.value})"
    if event.failure is TurnFailureKind.PROVIDER_FAILED:
        # The executor ended the turn with the reason, which is how a reply that broke its
        # schema reaches the strategy: the role is asked to fix it, a bounded number of times.
        corrected = _correct(state, config, event.detail or reason, event.invocation)
        if corrected is not None:
            return corrected
    return fail(state, reason)


def _wait_or_correct(
    state: IssueQueueStrategyState, view: RunView, config: IssueQueueConfig, event: TurnResult
) -> IssueQueueStrategyState:
    """The reply asked to wait: yield if core recorded the wait, else correct the role."""
    if not event.suspending:
        # Core recorded no wait (it refused it, or the host lost it), so no TurnSuspended
        # will come: waiting for one would stall the run.
        return _correct(state, config, None, event.invocation) or fail(
            state, "the turn asked to wait and the run recorded no wait"
        )
    return _yielded(state, view)


def _answered(
    state: IssueQueueStrategyState, view: RunView, reply: Result, job: Job, event: TurnResult
) -> IssueQueueStrategyState:
    """Accept a role's result. A write turn's waits for its checkpoint to be readable."""
    payload = event.output_json or ""
    if isinstance(reply, IssueImplementerResult | IssuePerfEvalResult):
        candidate = _candidate_of(view, job, event.invocation)
        if candidate is None:
            return _with(state, job.holding(payload, event.invocation))
        return _written_turn(state, reply, payload, candidate)
    return _accept(state, reply, payload, event)


def on_turn(
    state: IssueQueueStrategyState, view: RunView, config: IssueQueueConfig, event: TurnResult
) -> IssueQueueStrategyState:
    """Interpret a role's reply: accept, correct, suspend or fail."""
    job = _turn_job(state, event.invocation.invocation_id.root)
    role = None if job is None else role_of(job)
    if job is None or role is None:
        return state
    if event.observation.status is not ObservationStatus.SUCCEEDED or event.output_json is None:
        return _unfinished(state, view, config, event)
    reply = _decode(role, event.output_json, config)
    if isinstance(reply, str):
        return _correct(state, config, reply, event.invocation) or fail(
            state, f"invalid {role.value} reply"
        )
    if isinstance(reply, IssueImplementerResult | IssueJudgeResult | IssuePerfEvalResult):
        return _answered(state, view, reply, job, event)
    return _wait_or_correct(state, view, config, event)


def _performance(job: Job) -> PerformanceRecord:
    return PerformanceRecord(
        iteration=job.iteration,
        throughput_trend=job.throughput_trend,
        latency_trend=job.latency_trend,
        metrics_json=job.metrics_json,
        new_issues=job.created,
    )


def on_settled(state: IssueQueueStrategyState, event: AttemptSettled) -> IssueQueueStrategyState:
    """Finish the job: advance the tip past a closed issue and record a finished evaluation."""
    settlement = event.settlement
    job = state.job
    if job is None or job.attempt != settlement.attempt.attempt_id or job.phase is Phase.DONE:
        return state
    update: dict[str, object] = {}
    if job.kind is JobKind.ISSUE and job.closed and settlement.candidate is not None:
        update["tip"] = Tip(revision=settlement.candidate, settlement=settlement.settlement_id)
    if job.kind is JobKind.PERF and job.failure is None and job.metrics_json:
        update["performance"] = (*state.performance, _performance(job))
    done = job.model_copy(update={"phase": Phase.DONE, "awaiting": None})
    return retire(_with(state.model_copy(update=update), done))
