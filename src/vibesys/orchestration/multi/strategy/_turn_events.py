"""The implementer's and the judge's replies: fold each turn's answer into the open round.

Both roles answer through the same protocol: a reply that violates its schema is corrected, a turn
the transport lost is asked again, a reply that waits for evaluations suspends and later resumes.
An implementer's reply is held until core has retained (or declined) the checkpoint it made, so the
candidate it is judged on is known. The judge's verdict, with the live-archive guard, decides
whether the attempt is repaired by another paid turn or goes on to validation and the gates.
"""

from typing import Literal

from pydantic import ValidationError

from vibesys.hypothesis import reviewed, rounds
from vibesys.orchestration.multi.models import ImplementerResponse, JudgeResponse
from vibesys.orchestration.multi.strategy import _context as context
from vibesys.orchestration.multi.strategy import _gates as gates
from vibesys.orchestration.multi.strategy._config import MultiConfig
from vibesys.orchestration.multi.strategy._draft import invocation_for
from vibesys.orchestration.multi.strategy._prompts import (
    ReplyCorrectionPrompt,
    ResumePrompt,
    WaitUnrecordedPrompt,
)
from vibesys.orchestration.multi.strategy._record import (
    Repair,
    fail,
    put,
    retry_or_settle,
    to_settle,
)
from vibesys.orchestration.multi.strategy._round import role_of, subject_of
from vibesys.orchestration.multi.strategy._state import (
    NOTE_LIMIT,
    MultiStrategyState,
    Role,
    RoundState,
    Step,
    Unreachable,
    UnreachableReason,
    WorkPhase,
)
from vibesys.orchestration.review import Verdict
from vs_core.api import (
    InvocationRef,
    ObservationStatus,
    ResumeAuthorized,
    RevisionRef,
    RunView,
    TurnCandidate,
    TurnCandidateFailed,
    TurnCandidatePending,
    TurnCandidateRetained,
    TurnFailureKind,
    TurnResult,
    TurnSuspended,
    WaitableReply,
    WaitingForEvaluation,
)

_IMPLEMENTER = WaitableReply[ImplementerResponse]
_JUDGE = WaitableReply[JudgeResponse]
_ROLE_NAMES: dict[Role, Literal["implementer", "judge"]] = {
    Role.IMPLEMENTER: "implementer",
    Role.JUDGE: "judge",
}
_DOMINATED = "the live Pareto archive dominates the claimed frontier row"


def _decode(
    role: Role, payload: str
) -> ImplementerResponse | JudgeResponse | WaitingForEvaluation | str:
    adapter = _JUDGE if role is Role.JUDGE else _IMPLEMENTER
    try:
        return adapter.model_validate_json(payload).reply
    except ValidationError as error:
        return str(error)


def candidate_of(
    view: RunView, record: RoundState, invocation: InvocationRef
) -> TurnCandidate | None:
    """Resolve the current invocation through its shared logical turn chain."""
    return (
        None
        if record.turn is None
        else record.turn.candidate_outcome(view, record.attempt, invocation)
    )


def _checkpoint_failure(candidate: TurnCandidateFailed) -> str:
    return f"implementer checkpoint declined ({candidate.reason.value})"


def owns_turn(state: MultiStrategyState, event: TurnResult) -> bool:
    """Whether ``event`` answers the round's outstanding turn."""
    record = state.round
    role = None if record is None else role_of(record.phase)
    return (
        record is not None
        and role is not None
        and record.step is Step.AWAITING
        and record.turn is not None
        and event.invocation.invocation_id.root
        == invocation_for(role, subject_of(record, role), record.turn)
    )


def on_turn(
    state: MultiStrategyState, view: RunView, config: MultiConfig, event: TurnResult
) -> MultiStrategyState:
    """Interpret a reply: continue, correct, retry, suspend or fail."""
    record = state.round
    if record is None or record.turn is None or role_of(record.phase) is None:
        return state
    if event.observation.status is not ObservationStatus.SUCCEEDED or event.output_json is None:
        return _unfinished(state, view, config, record, event)
    return _answered(state, view, config, record, event)


def _answered(
    state: MultiStrategyState,
    view: RunView,
    config: MultiConfig,
    record: RoundState,
    event: TurnResult,
) -> MultiStrategyState:
    """Act on the reply of a turn that completed with output."""
    role = role_of(record.phase)
    payload = event.output_json
    if role is None or payload is None:
        return state
    reply = _decode(role, payload)
    if isinstance(reply, str):
        correction = ReplyCorrectionPrompt(role=_ROLE_NAMES[role], error=reply)
        return _corrected(state, config, record, event, correction)
    if isinstance(reply, WaitingForEvaluation):
        return _waiting(state, view, config, record, event)
    if isinstance(reply, JudgeResponse):
        judged = record.model_copy(update={"judge_invocation": event.invocation})
        return _judged(state, view, config, judged, reply)
    candidate = candidate_of(view, record, event.invocation)
    if candidate is None or isinstance(candidate, TurnCandidatePending):
        result = put(state, record.holding(payload, event.invocation))
    elif isinstance(candidate, TurnCandidateFailed):
        result = fail(state, record, _checkpoint_failure(candidate))
    else:
        result = _replied(state, config, record, reply, candidate.revision)
    return result


def settle_retained(
    state: MultiStrategyState, view: RunView, config: MultiConfig
) -> MultiStrategyState:
    """Fold a held implementer reply once core has retained or declined its checkpoint."""
    record = state.round
    if record is None or not record.holds_reply:
        return state
    invocation = record.held_invocation
    if invocation is None or record.held_reply is None:
        return state
    candidate = candidate_of(view, record, invocation)
    if candidate is None or isinstance(candidate, TurnCandidatePending):
        return state
    reply = _decode(Role.IMPLEMENTER, record.held_reply)
    released = record.released()
    released_state = state.model_copy(update={"round": released})
    if isinstance(candidate, TurnCandidateFailed):
        return fail(released_state, released, _checkpoint_failure(candidate))
    if isinstance(candidate, TurnCandidateRetained) and isinstance(reply, ImplementerResponse):
        return _replied(released_state, config, released, reply, candidate.revision)
    return released_state


def _corrected(
    state: MultiStrategyState,
    config: MultiConfig,
    record: RoundState,
    event: TurnResult,
    correction: ReplyCorrectionPrompt | WaitUnrecordedPrompt,
) -> MultiStrategyState:
    """Ask the role to fix its reply; the correction names the invalid turn as its predecessor."""
    turn = record.turn
    corrected = (
        None
        if turn is None
        else turn.corrected(event.invocation, correction, limit=config.max_corrections)
    )
    if corrected is None:
        if isinstance(correction, ReplyCorrectionPrompt):
            return _no_answer(state, config, record, f"invalid reply: {correction.error}")
        return fail(state, record, "the turn asked to wait and the run recorded no wait")
    return put(
        state, record.model_copy(update={"turn": corrected, "step": Step.NEEDED, "awaiting": None})
    )


def _waiting(
    state: MultiStrategyState,
    view: RunView,
    config: MultiConfig,
    record: RoundState,
    event: TurnResult,
) -> MultiStrategyState:
    """The reply asked to wait: only core's `TurnSuspended` completes the yield."""
    if not event.suspending:
        # Core recorded no wait (it refused it, or the host lost it), so no TurnSuspended will
        # come: waiting for one would stall the run.
        return _corrected(state, config, record, event, WaitUnrecordedPrompt())
    if "suspend" not in view.capabilities.lifecycle:
        failed = fail(state, record, "the run offers no suspend capability")
        missing = Unreachable(
            reason=UnreachableReason.RESUME_CAPABILITY_NOT_OFFERED, subject=record.hypothesis_id
        )
        return failed.model_copy(update={"unreachable": (*failed.unreachable, missing)})
    turn = record.turn
    if turn is None:
        return state
    return put(state, record.model_copy(update={"turn": turn.yielding()}))


def _unfinished(
    state: MultiStrategyState,
    view: RunView,
    config: MultiConfig,
    record: RoundState,
    event: TurnResult,
) -> MultiStrategyState:
    """A turn ended without a usable reply: ask again if lost, else it is a failed answer."""
    turn = record.turn
    if turn is None:
        return state
    if event.failure is TurnFailureKind.TRANSPORT_LOST:
        again = turn.asked_again(
            event.invocation,
            now=view.run.now_at,
            limit=config.max_turn_drops,
            backoff=config.drop_backoff(turn.drops),
        )
        if again is None:
            return fail(state, record, f"the turn was lost ({event.detail or 'transport'})")
        return put(
            state, record.model_copy(update={"turn": again, "step": Step.NEEDED, "awaiting": None})
        )
    detail = event.detail or f"the turn did not complete ({event.observation.status.value})"
    return _no_answer(state, config, record, detail)


def _no_answer(
    state: MultiStrategyState, config: MultiConfig, record: RoundState, reason: str
) -> MultiStrategyState:
    """A role produced no valid answer: an implementer is retried, a judge's review is lost."""
    if record.phase is WorkPhase.REVIEW:
        # No verdict exists to act on and the candidate is intact, so the round ends unreviewed
        # (retained as work in progress) instead of paying for another implementation.
        noted = state.model_copy(update={"notes": (*state.notes, f"judge: {reason}")[-NOTE_LIMIT:]})
        return put(noted, to_settle(record, judge_pass=None, judge_invocation=None))
    cleared = record.model_copy(
        update={"response_json": None, "no_valid_reply": reason, "judge_pass": None}
    )
    return retry_or_settle(state, config, cleared, Repair())


def _replied(
    state: MultiStrategyState,
    config: MultiConfig,
    record: RoundState,
    reply: ImplementerResponse,
    candidate: RevisionRef | None,
) -> MultiStrategyState:
    """Accept a valid implementer reply: review it when due, else end the attempt."""
    ledger = context.ledger_of(state, config)
    record = record.model_copy(
        update={
            "response_json": reply.model_dump_json(),
            "candidate": candidate or record.candidate,
            "no_valid_reply": None,
            "awaiting": None,
            "conflict": None,
            "validation_failure": None,
        }
    )
    if record.candidate is None and reviewed.candidate_ready(reply):
        failed = rounds.note_attempt_failure(
            config.hypotheses, ledger, "retained no changed candidate"
        )
        noted = state.model_copy(update={"search_json": rounds.encode(failed).search_json})
        cleared = record.model_copy(
            update={"no_valid_reply": "the implementer retained no changed candidate"}
        )
        return retry_or_settle(noted, config, cleared, Repair())
    due = reviewed.review_due(
        config.hypotheses,
        ledger,
        reply,
        round_number=record.number,
        review_started=record.review_started,
        revalidation_required=record.revalidation_required,
    )
    if not due:
        return put(state, to_settle(record, review_skipped=True))
    conflict = rounds.pareto_conflict(config.hypotheses, ledger, reply)
    return put(
        state,
        record.model_copy(
            update={
                "phase": WorkPhase.REVIEW,
                "step": Step.NEEDED,
                "turn": None,
                "review_started": True,
                "revalidation_required": False,
                "conflict": None if conflict is None else context.conflict_view(conflict),
            }
        ),
    )


def _judged(
    state: MultiStrategyState,
    view: RunView,
    config: MultiConfig,
    record: RoundState,
    verdict: JudgeResponse,
) -> MultiStrategyState:
    """Apply the Pareto guard to the verdict, then repair, validate or measure."""
    conflict = record.conflict
    passing = verdict.verdict is Verdict.PASS and conflict is None
    judged = record.model_copy(update={"judge_pass": passing, "awaiting": None})
    if passing:
        return gates.after_review(state, view, config, judged)
    feedback = verdict.feedback or (_DOMINATED if conflict is not None else "review failed")
    ledger = rounds.note_attempt_failure(
        config.hypotheses, context.ledger_of(state, config), feedback
    )
    noted = state.model_copy(update={"search_json": rounds.encode(ledger).search_json})
    guarded = conflict if verdict.verdict is Verdict.PASS else None
    repair = verdict.feedback or (None if guarded is not None else feedback)
    return retry_or_settle(noted, config, judged, Repair(feedback=repair, conflict=guarded))


def on_suspended(state: MultiStrategyState, event: TurnSuspended) -> MultiStrategyState:
    """Core recorded the continuation: wait for `ResumeAuthorized` of exactly that ID."""
    record = state.round
    role = None if record is None else role_of(record.phase)
    continuation = event.continuation
    if record is None or record.turn is None or role is None or record.step is not Step.AWAITING:
        return state
    if continuation.invocation.invocation_id.root != invocation_for(
        role, subject_of(record, role), record.turn
    ):
        return state
    turn = record.turn.suspended(continuation.invocation, continuation.continuation_id)
    return put(
        state, record.model_copy(update={"turn": turn, "step": Step.SUSPENDED, "awaiting": None})
    )


def on_resume(state: MultiStrategyState, event: ResumeAuthorized) -> MultiStrategyState:
    """Authorize the suspended turn's resume with the invocation core named."""
    record = state.round
    role = None if record is None else role_of(record.phase)
    if (
        record is None
        or role is None
        or record.step is not Step.SUSPENDED
        or record.turn is None
        or record.turn.continuation != event.continuation_id
    ):
        return state
    resumed = record.turn.resumed(
        authorized=event.next_invocation.invocation_id,
        continuation=event.continuation_id,
        context=ResumePrompt(
            role=_ROLE_NAMES[role],
            retained_revision=record.candidate,
            timed_out=event.timeout is not None,
            evidence=tuple(item.evidence_id for item in event.evidence),
            repeated_failure=None
            if event.repeated_failure is None
            else str(event.repeated_failure),
        ),
    )
    return put(state, record.model_copy(update={"turn": resumed, "step": Step.NEEDED}))
