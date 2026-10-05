"""Event handlers of a workstream: fold each core feedback into its record.

Handlers never propose decisions; they move a record to the phase and step whose
decision `_attempts.advance` proposes next. Duplicate or late events find no
matching awaiting record and leave the state unchanged.
"""

from typing import Literal

from pydantic import TypeAdapter, ValidationError

from vibesys.hypothesis import HypothesisConfig, HypothesisOutcome
from vibesys.hypothesis.cadence import keeps_hypothesis_active, review_due
from vibesys.orchestration.dynamic.models import (
    ImplementerReply,
    ImplementerResult,
    JudgeReply,
    ReviewResult,
    WaitingForEvaluation,
)
from vibesys.orchestration.dynamic.strategy import _ids as ids
from vibesys.orchestration.dynamic.strategy._attempts import measured_revision, role_of, subject_of
from vibesys.orchestration.dynamic.strategy._baseline import stages
from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._draft import invocation_for
from vibesys.orchestration.dynamic.strategy._evidence import (
    accept_readings,
    ledger_refs,
    trusted_keys,
    turn_candidate,
)
from vibesys.orchestration.dynamic.strategy._operations import (
    EvidenceReadings,
    ParentVerification,
    RenderedArtifacts,
)
from vibesys.orchestration.dynamic.strategy._parents import (
    ParentConflictError,
    ParentSnapshot,
    ingest,
)
from vibesys.orchestration.dynamic.strategy._prompts import ReplyCorrectionPrompt, ResumePrompt
from vibesys.orchestration.dynamic.strategy._rows import reading_of
from vibesys.orchestration.dynamic.strategy._settlement import STOPPED
from vibesys.orchestration.dynamic.strategy._state import (
    AttemptRecord,
    DynamicStrategyState,
    HypothesisRecord,
    Role,
    RoundRecord,
    Step,
    TurnRecord,
    Unreachable,
    UnreachableReason,
    WorkKind,
    WorkPhase,
)
from vs_core.api import (
    AttemptExhausted,
    AttemptReady,
    AttemptSettled,
    EvidenceKind,
    IntentBlocked,
    InvocationRef,
    MeasurementResult,
    ObservationStatus,
    OperationResult,
    Rejected,
    ResumeAuthorized,
    RevisionRef,
    RunView,
    TurnResult,
    TurnSuspended,
)

_IMPLEMENTER_REPLY = TypeAdapter(ImplementerReply)
_JUDGE_REPLY = TypeAdapter(JudgeReply)
_CORRECTION_ROLE: dict[Role, Literal["planner", "implementer", "judge", "profiler"]] = {
    Role.PLANNER: "planner",
    Role.IMPLEMENTER: "implementer",
    Role.JUDGE: "judge",
    Role.PROFILER: "profiler",
}


def _put(state: DynamicStrategyState, index: int, record: AttemptRecord) -> DynamicStrategyState:
    attempts = list(state.attempts)
    attempts[index] = record
    return state.model_copy(update={"attempts": tuple(attempts)})


def _awaiting(state: DynamicStrategyState, decision: str | None) -> int | None:
    if decision is None:
        return None
    return next(
        (
            index
            for index, item in enumerate(state.attempts)
            if item.awaiting is not None
            and item.awaiting.root == decision
            and item.phase is not WorkPhase.DONE
        ),
        None,
    )


def fail(state: DynamicStrategyState, index: int, reason: str) -> DynamicStrategyState:
    """End a workstream with a typed failure; admitted attempts must still settle."""
    record = state.attempts[index]
    if record.phase is WorkPhase.DONE:
        return state
    if record.ready:
        failed = record.model_copy(
            update={
                "phase": WorkPhase.SETTLE,
                "step": Step.NEEDED,
                "awaiting": None,
                "failure": record.failure or reason,
            }
        )
        return _put(state, index, failed)
    failed = record.model_copy(
        update={"phase": WorkPhase.DONE, "awaiting": None, "failure": record.failure or reason}
    )
    refunded = state.refunded + 1
    return _put(state, index, failed).model_copy(update={"refunded": refunded})


def on_rejected(state: DynamicStrategyState, event: Rejected) -> DynamicStrategyState:
    """A decision core refused fails the workstream awaiting it."""
    index = _awaiting(state, event.decision_id.root)
    if index is None:
        return state
    return fail(state, index, f"decision rejected: {event.code.value}: {event.detail}")


def on_ready(state: DynamicStrategyState, event: AttemptReady) -> DynamicStrategyState:
    """The attempt holds a slot: begin its first turn, or settle it if a stop raced."""
    index = next(
        (
            i
            for i, item in enumerate(state.attempts)
            if item.attempt == event.attempt.attempt_id and item.phase is WorkPhase.START
        ),
        None,
    )
    if index is None:
        return state
    record = state.attempts[index].model_copy(
        update={"ready": True, "generation": event.attempt.generation, "awaiting": None}
    )
    if state.stopping:
        record = record.model_copy(
            update={"phase": WorkPhase.SETTLE, "step": Step.NEEDED, "failure": STOPPED}
        )
    else:
        phase = WorkPhase.IMPLEMENT if record.plan.kind is WorkKind.IMPLEMENT else WorkPhase.PROFILE
        record = record.model_copy(update={"phase": phase, "step": Step.NEEDED, "turn": None})
    return _put(state, index, record)


def _rendered(
    state: DynamicStrategyState, index: int, outcome: RenderedArtifacts
) -> DynamicStrategyState:
    record = state.attempts[index]
    if outcome.status != "succeeded" or not outcome.prompts or record.turn is None:
        return fail(state, index, f"prompt render {outcome.status}")
    turn = record.turn.model_copy(
        update={"prompts": outcome.prompts, "tool_policy": outcome.tool_policy}
    )
    return _put(
        state,
        index,
        record.model_copy(update={"turn": turn, "step": Step.RENDERED, "awaiting": None}),
    )


def _verified(
    state: DynamicStrategyState, index: int, outcome: ParentVerification
) -> DynamicStrategyState:
    record = state.attempts[index]
    if outcome.status == "succeeded" and outcome.verified:
        return _put(
            state,
            index,
            record.model_copy(
                update={"phase": WorkPhase.START, "step": Step.NEEDED, "awaiting": None}
            ),
        )
    root = record.parent.revision_id.root
    withheld = state.withheld if root in state.withheld else (*state.withheld, root)
    failed = fail(state, index, f"parent {root} could not be verified")
    planner = failed.planner.model_copy(update={"blocked_at_done": None})
    return failed.model_copy(update={"withheld": withheld, "planner": planner})


def _snapshot(record: AttemptRecord) -> ParentSnapshot | None:
    accuracy = reading_of(record.readings, EvidenceKind.CORRECTNESS)
    if accuracy is None or not accuracy.passed or record.candidate is None:
        return None
    return ParentSnapshot(
        hypothesis_id=record.plan.work_id,
        generation=record.generation,
        revision=record.candidate,
        accuracy=accuracy,
        benchmark=reading_of(record.readings, EvidenceKind.BENCHMARK),
        submission_index=record.sequence,
        change_summary=record.summary or None,
    )


def publish_parent(
    state: DynamicStrategyState, record: AttemptRecord, view: RunView
) -> DynamicStrategyState:
    """Offer the exact candidate as a parent once core proves it retained and verified."""
    snapshot = _snapshot(record)
    if snapshot is None:
        return state
    try:
        parents = ingest(state.parents, snapshot, view)
    except ParentConflictError:
        return state
    return state.model_copy(update={"parents": parents})


def _interpreted(
    state: DynamicStrategyState, index: int, outcome: EvidenceReadings, view: RunView
) -> DynamicStrategyState:
    record = state.attempts[index]
    readings = accept_readings(outcome, ledger_refs(view, record.evidence) or ())
    if isinstance(readings, str):
        return fail(state, index, f"evidence readings refused: {readings}")
    if outcome.status != "succeeded" or not readings:
        return fail(state, index, f"evidence could not be decoded ({outcome.status})")
    record = record.model_copy(
        update={
            "readings": readings,
            "phase": WorkPhase.SETTLE,
            "step": Step.NEEDED,
            "awaiting": None,
        }
    )
    return publish_parent(_put(state, index, record), record, view)


def on_operation(
    state: DynamicStrategyState, view: RunView, event: OperationResult
) -> DynamicStrategyState:
    """Route a registered operation's typed outcome to the record that awaits it."""
    index = _awaiting(state, ids.decision_of_operation(event.operation_id.root))
    if index is None:
        return state
    outcome = event.outcome
    if isinstance(outcome, RenderedArtifacts):
        return _rendered(state, index, outcome)
    if isinstance(outcome, ParentVerification):
        return _verified(state, index, outcome)
    if isinstance(outcome, EvidenceReadings):
        return _interpreted(state, index, outcome, view)
    return state


def on_measurement(state: DynamicStrategyState, event: MeasurementResult) -> DynamicStrategyState:
    """Record a candidate's trusted evidence keys for interpretation.

    The result must come from the awaiting attempt's own scope and generation, and
    only evidence of the measured revision and purpose that core trusts is kept.
    """
    index = next(
        (
            i
            for i, item in enumerate(state.attempts)
            if item.attempt == event.scope.owner
            and item.generation == event.scope.generation
            and item.phase is WorkPhase.MEASURE
            and item.step is Step.AWAITING
        ),
        None,
    )
    if index is None:
        return state
    current = state.attempts[index]
    evidence = trusted_keys(
        event,
        scope=event.scope,
        candidate=measured_revision(current),
        purpose="profile" if current.plan.kind is WorkKind.PROFILE else "official",
    )
    if not evidence:
        return fail(
            state, index, f"measurement produced no trusted evidence ({event.status.value})"
        )
    record = current.model_copy(
        update={
            "evidence": evidence,
            "phase": WorkPhase.INTERPRET,
            "step": Step.NEEDED,
            "awaiting": None,
        }
    )
    return _put(state, index, record)


def _invocation_index(state: DynamicStrategyState, invocation_id: str) -> int | None:
    for index, record in enumerate(state.attempts):
        role = role_of(record.phase)
        if (
            role is not None
            and record.step is Step.AWAITING
            and record.turn is not None
            and invocation_id == invocation_for(role, subject_of(record, role), record.turn)
        ):
            return index
    return None


def _turn_index(state: DynamicStrategyState, event: TurnResult) -> int | None:
    return _invocation_index(state, event.invocation.invocation_id.root)


def owns_turn(state: DynamicStrategyState, event: TurnResult) -> bool:
    """Whether ``event`` answers an attempt's outstanding turn."""
    return _turn_index(state, event) is not None


def _correct(
    state: DynamicStrategyState,
    index: int,
    config: DynamicConfig,
    error: str,
    invalid: InvocationRef,
) -> DynamicStrategyState | None:
    """Ask the role to fix its reply; the correction names the invalid turn as its predecessor."""
    record = state.attempts[index]
    turn = record.turn
    role = role_of(record.phase)
    if turn is None or role is None or turn.corrections >= config.max_corrections:
        return None
    corrected = TurnRecord(
        role=role,
        serial=turn.serial + 1,
        corrections=turn.corrections + 1,
        charge="correction",
        invocation=invalid,
        context=ReplyCorrectionPrompt(role=_CORRECTION_ROLE[role], error=error),
    )
    return _put(
        state,
        index,
        record.model_copy(update={"turn": corrected, "step": Step.NEEDED, "awaiting": None}),
    )


def _retry(
    state: DynamicStrategyState, index: int, config: DynamicConfig, feedback: str, reason: str
) -> DynamicStrategyState:
    record = state.attempts[index]
    turn = record.turn
    if (
        record.plan.kind is not WorkKind.IMPLEMENT
        or record.turns_spent >= config.max_retries_per_round
    ):
        return fail(state, index, reason)
    retried = record.model_copy(
        update={
            "phase": WorkPhase.IMPLEMENT,
            "step": Step.NEEDED,
            "awaiting": None,
            "feedback": feedback,
            "review_passed": None,
            "turn": TurnRecord(
                role=Role.IMPLEMENTER,
                serial=0 if turn is None else turn.serial + 1,
                charge="paid",
            ),
        }
    )
    return _put(state, index, retried)


def _yielded(state: DynamicStrategyState, index: int, view: RunView) -> DynamicStrategyState:
    """The reply asked to wait: only core's `TurnSuspended` completes the yield."""
    record = state.attempts[index]
    if "suspend" not in view.capabilities.lifecycle:
        unreachable = Unreachable(
            reason=UnreachableReason.RESUME_CAPABILITY_NOT_OFFERED, subject=record.plan.work_id
        )
        failed = fail(state, index, "the run offers no suspend capability")
        return failed.model_copy(update={"unreachable": (*failed.unreachable, unreachable)})
    turn = (record.turn or TurnRecord(role=Role.IMPLEMENTER)).model_copy(update={"yielded": True})
    return _put(state, index, record.model_copy(update={"turn": turn}))


def on_suspended(state: DynamicStrategyState, event: TurnSuspended) -> DynamicStrategyState:
    """Core recorded the continuation: wait for `ResumeAuthorized` of exactly that ID."""
    continuation = event.continuation
    index = _invocation_index(state, continuation.invocation.invocation_id.root)
    if index is None:
        return state
    record = state.attempts[index]
    turn = (record.turn or TurnRecord(role=Role.IMPLEMENTER)).model_copy(
        update={
            "invocation": continuation.invocation,
            "continuation": continuation.continuation_id,
            "yielded": True,
        }
    )
    return _put(
        state,
        index,
        record.model_copy(update={"turn": turn, "step": Step.SUSPENDED, "awaiting": None}),
    )


def _next_after_candidate(record: AttemptRecord, config: DynamicConfig) -> AttemptRecord:
    if record.outcome is None:
        return record
    hypothesis = HypothesisConfig(
        max_rounds=config.start_budget,
        judge_every=config.judge_every,
        max_retries_per_round=config.max_retries_per_round,
        max_continuation_rounds=config.max_continuation_rounds,
    )
    due = review_due(hypothesis, round_number=record.sequence, outcome=record.outcome)
    if due:
        return record.model_copy(
            update={"phase": WorkPhase.REVIEW, "step": Step.NEEDED, "turn": None, "awaiting": None}
        )
    return _measure_or_settle(record, config)


def _measure_or_settle(record: AttemptRecord, config: DynamicConfig) -> AttemptRecord:
    phase = WorkPhase.MEASURE if stages(config) else WorkPhase.SETTLE
    return record.model_copy(
        update={"phase": phase, "step": Step.NEEDED, "turn": None, "awaiting": None}
    )


def _nominates(result: ImplementerResult) -> bool:
    """Whether the reply claims a candidate, so core's checkpoint of the turn decides it."""
    return result.outcome not in (
        HypothesisOutcome.IMPLEMENTATION_FAILED,
        HypothesisOutcome.BLOCKED,
    )


def settle_retained(
    state: DynamicStrategyState, view: RunView, config: DynamicConfig
) -> DynamicStrategyState:
    """Fold each held implementer reply once core has retained or declined its checkpoint.

    Core delivers a turn's reply before the executor snapshots the workspace, so the
    candidate is readable from the view only later. Until then the reply stays held.
    """
    for index, record in enumerate(state.attempts):
        invocation = record.held_invocation
        if record.step is not Step.RETAINING or invocation is None or record.held_reply is None:
            continue
        live = next((item for item in view.attempts if item.attempt_id == record.attempt), None)
        candidate = turn_candidate(view, record.attempt, invocation)
        declined = live is not None and any(
            row.invocation == invocation for row in live.checkpoint_declines
        )
        if candidate is None and not declined:
            continue
        reply = _decode(Role.IMPLEMENTER, record.held_reply)
        released = record.model_copy(
            update={"step": Step.NEEDED, "held_reply": None, "held_invocation": None}
        )
        state = _put(state, index, released)
        if isinstance(reply, ImplementerResult):
            state = _implemented(state, index, config, reply, candidate)
    return state


def _implemented(
    state: DynamicStrategyState,
    index: int,
    config: DynamicConfig,
    result: ImplementerResult,
    candidate: RevisionRef | None,
) -> DynamicStrategyState:
    record = state.attempts[index]
    record = record.model_copy(
        update={
            "outcome": result.outcome,
            "summary": result.summary,
            "next_step": result.next_step,
            "candidate": candidate,
            "awaiting": None,
        }
    )
    state = _put(state, index, record)
    if result.outcome is HypothesisOutcome.IMPLEMENTATION_FAILED:
        return _retry(state, index, config, result.summary, "implementation failed")
    if result.outcome is HypothesisOutcome.BLOCKED:
        return _put(
            state, index, record.model_copy(update={"phase": WorkPhase.SETTLE, "step": Step.NEEDED})
        )
    if candidate is None or candidate == record.parent:
        return fail(state, index, "the implementer retained no changed candidate")
    return _put(state, index, _next_after_candidate(record, config))


def _reviewed(
    state: DynamicStrategyState,
    index: int,
    config: DynamicConfig,
    result: ReviewResult,
    event: TurnResult,
) -> DynamicStrategyState:
    record = state.attempts[index].model_copy(
        update={
            "review_passed": result.passed,
            "judge_invocation": event.invocation,
            "feedback": result.feedback or result.analysis,
            "awaiting": None,
        }
    )
    state = _put(state, index, record)
    if result.passed:
        return _put(state, index, _measure_or_settle(record, config))
    return _retry(
        state, index, config, result.feedback or result.analysis, "review rejected the candidate"
    )


def _decode(
    role: Role, payload: str
) -> ImplementerResult | ReviewResult | WaitingForEvaluation | str:
    adapter = _JUDGE_REPLY if role is Role.JUDGE else _IMPLEMENTER_REPLY
    try:
        return adapter.validate_json(payload)
    except ValidationError as error:
        return str(error)


def on_turn(
    state: DynamicStrategyState, view: RunView, config: DynamicConfig, event: TurnResult
) -> DynamicStrategyState:
    """Interpret a role's reply: continue, correct, retry, suspend or fail."""
    index = _turn_index(state, event)
    if index is None:
        return state
    record = state.attempts[index]
    role = role_of(record.phase)
    if role is None:
        return state
    if event.observation.status is not ObservationStatus.SUCCEEDED or event.output_json is None:
        reason = f"{role.value} turn did not complete ({event.observation.status.value})"
        if role is Role.IMPLEMENTER:
            return _retry(state, index, config, reason, reason)
        return fail(state, index, reason)
    if role is Role.PROFILER:
        return _put(state, index, _profiled(record, config))
    return _answered(state, index, view, config, event)


def _answered(
    state: DynamicStrategyState,
    index: int,
    view: RunView,
    config: DynamicConfig,
    event: TurnResult,
) -> DynamicStrategyState:
    role = Role.JUDGE if state.attempts[index].phase is WorkPhase.REVIEW else Role.IMPLEMENTER
    reply = _decode(role, event.output_json or "")
    if isinstance(reply, str):
        return _correct(state, index, config, reply, event.invocation) or fail(
            state, index, f"invalid {role.value} reply"
        )
    if isinstance(reply, WaitingForEvaluation):
        return _yielded(state, index, view)
    if isinstance(reply, ImplementerResult):
        candidate = turn_candidate(view, state.attempts[index].attempt, event.invocation)
        if candidate is None and _nominates(reply):
            return _put(
                state,
                index,
                state.attempts[index].model_copy(
                    update={
                        "step": Step.RETAINING,
                        "awaiting": None,
                        "held_reply": event.output_json,
                        "held_invocation": event.invocation,
                    }
                ),
            )
        return _implemented(state, index, config, reply, candidate)
    return _reviewed(state, index, config, reply, event)


def _profiled(record: AttemptRecord, config: DynamicConfig) -> AttemptRecord:
    phase = WorkPhase.MEASURE if config.profile_measurement else WorkPhase.SETTLE
    return record.model_copy(
        update={"phase": phase, "step": Step.NEEDED, "awaiting": None, "turn": None}
    )


def _suspended_index(state: DynamicStrategyState, event: ResumeAuthorized) -> int | None:
    for index, item in enumerate(state.attempts):
        if (
            item.step is Step.SUSPENDED
            and item.turn is not None
            and item.turn.continuation == event.continuation_id
        ):
            return index
    return None


def on_resume(state: DynamicStrategyState, event: ResumeAuthorized) -> DynamicStrategyState:
    """Authorize the suspended turn's resume with the invocation core named."""
    index = _suspended_index(state, event)
    if index is None:
        return state
    record = state.attempts[index]
    role = role_of(record.phase)
    previous = record.turn
    if role is None or previous is None or role is Role.PLANNER:
        return state
    resumed = TurnRecord(
        role=role,
        serial=previous.serial + 1,
        charge="resume",
        context=ResumePrompt(
            role=role.value,  # type: ignore[arg-type]
            retained_revision=record.candidate,
            timed_out=event.timeout is not None,
            evidence=tuple(item.evidence_id for item in event.evidence),
            repeated_failure=None
            if event.repeated_failure is None
            else str(event.repeated_failure),
        ),
        invocation=previous.invocation,
        resume_as=event.next_invocation.invocation_id,
        continuation=event.continuation_id,
    )
    return _put(state, index, record.model_copy(update={"turn": resumed, "step": Step.NEEDED}))


def on_exhausted(state: DynamicStrategyState, event: AttemptExhausted) -> DynamicStrategyState:
    """A spent budget ends the workstream; core grants no automatic retry."""
    index = next(
        (i for i, item in enumerate(state.attempts) if item.attempt == event.attempt.attempt_id),
        None,
    )
    if index is None:
        return state
    return fail(state, index, f"attempt budget exhausted ({event.reason})")


def _round(record: AttemptRecord, settled: AttemptSettled, config: DynamicConfig) -> RoundRecord:
    accuracy = reading_of(record.readings, EvidenceKind.CORRECTNESS)
    benchmark = reading_of(record.readings, EvidenceKind.BENCHMARK)
    kept = keeps_hypothesis_active(
        outcome=record.outcome,
        next_step=record.next_step,
        continuation_rounds=0,
        max_continuation_rounds=config.max_continuation_rounds,
    )
    return RoundRecord(
        sequence=record.sequence,
        attempt=record.attempt,
        outcome=record.outcome,
        summary=record.summary,
        review_passed=record.review_passed,
        candidate=settled.settlement.candidate,
        accuracy_passed=None if accuracy is None else accuracy.passed,
        benchmark_passed=None if benchmark is None else benchmark.passed,
        metrics=() if benchmark is None else benchmark.metrics,
        partial=None if benchmark is None else benchmark.partial,
        eligible=settled.settlement.eligible,
        failure=record.failure,
        settlement=settled.settlement.settlement_id,
        kept_active=kept,
    )


def on_settled(
    state: DynamicStrategyState, view: RunView, config: DynamicConfig, event: AttemptSettled
) -> DynamicStrategyState:
    """Finish the workstream: record its round and publish its verified candidate."""
    settlement = event.settlement
    index = next(
        (
            i
            for i, item in enumerate(state.attempts)
            if item.attempt == settlement.attempt.attempt_id and item.phase is not WorkPhase.DONE
        ),
        None,
    )
    if index is None:
        return state
    record = state.attempts[index]
    done = record.model_copy(update={"phase": WorkPhase.DONE, "awaiting": None})
    state = _put(state, index, done)
    if record.plan.kind is WorkKind.PROFILE:
        return state
    state = _record_round(state, done, _round(done, event, config))
    return publish_parent(state, done, view)


def _record_round(
    state: DynamicStrategyState, record: AttemptRecord, row: RoundRecord
) -> DynamicStrategyState:
    hypotheses = list(state.hypotheses)
    index = next(
        (i for i, item in enumerate(hypotheses) if item.hypothesis_id == record.plan.work_id), None
    )
    if index is None:
        return state
    current: HypothesisRecord = hypotheses[index]
    hypotheses[index] = current.model_copy(
        update={
            "rounds": (*current.rounds, row),
            "continuation_rounds": current.continuation_rounds
            + (1 if record.plan.continue_hypothesis else 0),
        }
    )
    return state.model_copy(update={"hypotheses": tuple(hypotheses)})


def on_blocked(state: DynamicStrategyState, event: IntentBlocked) -> DynamicStrategyState:
    """A blocked request leaves its effect unknown: end the workstream awaiting it.

    An operation names its decision in its request ID. Any other blocked request of
    an attempt (turn, measurement, start) ends that attempt's awaited step.
    """
    detail = f"request blocked: {event.diagnostic}"
    index = _awaiting(state, ids.decision_of_operation(event.target.root))
    if index is None and event.scope.owner.kind == "attempt":
        index = next(
            (
                i
                for i, item in enumerate(state.attempts)
                if item.attempt == event.scope.owner
                and item.generation == event.scope.generation
                and item.step is Step.AWAITING
                and item.phase is not WorkPhase.DONE
            ),
            None,
        )
    return state if index is None else fail(state, index, detail)
