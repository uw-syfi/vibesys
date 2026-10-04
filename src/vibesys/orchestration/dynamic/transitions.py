"""Pure envelope transitions for withdrawal and atomic workstream settlement.

Strategy supplies a round proposal. Lifecycle safety, refunds, phase changes,
steer drops and completion are decided together here before the shell commits.
"""

from dataclasses import replace
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vibesys.hypothesis import CandidateDisposition, HypothesisOutcome, RoundRecord
from vibesys.hypothesis import transitions as hypothesis_transitions
from vibesys.orchestration.dynamic.lifecycle import (
    BlockIntent,
    CancelEvaluation,
    CompleteIntent,
    ContinuationStatus,
    DependencyContinuation,
    DispatchIntent,
    EvaluationContinuation,
    EvaluationEvidenceId,
    EvaluationOutcome,
    EvaluationProgress,
    EvaluationStage,
    EvaluationTimeout,
    InspectEvaluation,
    IntentKind,
    IntentStage,
    LifecycleEvent,
    LifecycleIntent,
    LifecycleRequest,
    NonnegativeSeconds,
    PrepareIntent,
    ProfilerDependency,
    RecoveryStarted,
    ResumeAgentTurn,
    TimedOut,
    awaiting_evaluation,
    continuation_pending,
)
from vibesys.orchestration.dynamic.lifecycle import (
    step as ledger_step,
)
from vibesys.orchestration.dynamic.models import (
    DynamicState,
    DynamicWorkstream,
    JournalEntry,
    WorkstreamPhase,
    planned_id,
)


class AlreadySettledError(ValueError):
    """A recorded round wins the race against withdrawal."""


class EvaluationContinuationError(ValueError):
    """A suspension transition conflicts with durable ownership or dispatch authority."""


class WithdrawRequested(BaseModel):
    """Close a workstream to ordinary execution before stopping its effects."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_id: str
    kind: IntentKind


class SettlementProposed(BaseModel):
    """A round proposal and external cleanup acknowledgement for an owned intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str
    record: RoundRecord | None = None
    at_s: float
    drop_journal: tuple[JournalEntry, ...] = ()
    unresolved: bool = False
    retry_limit: Annotated[int, Field(ge=0)]


class InterruptedTurnReplaced(BaseModel):
    """Retained WIP and the bounded replacement charge commit together."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_id: str
    revision: str = Field(min_length=1)
    retry_limit: Annotated[int, Field(ge=0)]


class AttemptBoundReached(BaseModel):
    """End a charged attempt before dispatching its next evaluation continuation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str
    reason: str = Field(min_length=1)


class WorkerAwaitingEvaluation(BaseModel):
    """Host-validated yield, after retaining the workspace revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["worker_awaiting_evaluation"] = "worker_awaiting_evaluation"
    continuation: EvaluationContinuation


def dependency_wait(continuation: DependencyContinuation) -> WorkerAwaitingEvaluation:
    """Build the product wait event from a validated typed dependency continuation."""
    return WorkerAwaitingEvaluation(continuation=continuation)


class EvaluationSettled(BaseModel):
    """Trusted observation, attributed to its immutable evidence identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["evaluation_settled"] = "evaluation_settled"
    continuation_id: str
    scope_id: str
    generation: Annotated[int, Field(ge=0)]
    handle: str
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: tuple[EvaluationEvidenceId, ...] = ()
    outcome: EvaluationOutcome
    at_s: NonnegativeSeconds
    observation_state: Literal["pending", "running", "unknown"] = "unknown"
    stage: EvaluationStage | None = None
    queued_seconds: NonnegativeSeconds | None = None
    ran_seconds: NonnegativeSeconds | None = None
    pending_reason: str | None = None
    estimated_start_s: NonnegativeSeconds | None = None

    @field_validator("evidence_ids")
    @classmethod
    def _unique_evidence(cls, ids: tuple[str, ...]) -> tuple[str, ...]:
        if len(ids) != len(set(ids)):
            message = "evidence_ids must be unique"
            raise ValueError(message)
        return ids


class ProfilerSettled(BaseModel):
    """Trusted terminal operation observation bound to the yielded principal."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["profiler_settled"] = "profiler_settled"
    continuation_id: str
    handle: str
    principal_id: str
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: EvaluationOutcome


class EvaluationObserved(BaseModel):
    """Progress observation that cannot settle or authorize a continuation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["evaluation_observed"] = "evaluation_observed"
    continuation_id: str
    scope_id: str
    generation: Annotated[int, Field(ge=0)]
    handle: str
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    at_s: NonnegativeSeconds
    observation_state: Literal["pending", "running", "unknown"] = "unknown"
    stage: EvaluationStage | None = None
    queued_seconds: NonnegativeSeconds | None = None
    ran_seconds: NonnegativeSeconds | None = None
    pending_reason: str | None = None
    estimated_start_s: NonnegativeSeconds | None = None


class DeadlineReached(BaseModel):
    """Absolute logical time supplied by the shell; the core reads no clock."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["deadline_reached"] = "deadline_reached"
    continuation_id: str
    at_s: NonnegativeSeconds


class EvaluationInspected(BaseModel):
    """One bounded inspection settles or explicitly blocks termination intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["evaluation_inspected"] = "evaluation_inspected"
    operation_id: str
    outcome: EvaluationOutcome
    observation_state: Literal["pending", "running", "unknown"] = "unknown"


class EvaluationDispatchStopped(BaseModel):
    """Stop/deadline preserves durable waits and suppresses new dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["evaluation_dispatch_stopped"] = "evaluation_dispatch_stopped"
    stopped: bool = True


class EvaluationWaitReopened(BaseModel):
    """Explicitly resolve parked dependencies before permitting a resume."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    continuation_id: str
    kind: Literal["evaluation_wait_reopened"] = "evaluation_wait_reopened"
    resolved_cancelled_handles: tuple[str, ...] = ()


type EnvelopeEvent = (
    WithdrawRequested
    | SettlementProposed
    | InterruptedTurnReplaced
    | AttemptBoundReached
    | WorkerAwaitingEvaluation
    | EvaluationSettled
    | ProfilerSettled
    | EvaluationObserved
    | DeadlineReached
    | EvaluationInspected
    | EvaluationDispatchStopped
    | EvaluationWaitReopened
    | LifecycleEvent
)


def step(
    state: DynamicState, event: EnvelopeEvent
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    """Return a new envelope with one lifecycle transition; input is immutable."""
    result = state.model_copy(deep=True)
    requests: tuple[LifecycleRequest, ...] = ()
    match event:
        case WithdrawRequested(scope_id=scope_id, kind=kind):
            return _withdraw(result, scope_id, kind)
        case (
            WorkerAwaitingEvaluation()
            | EvaluationSettled()
            | ProfilerSettled()
            | EvaluationObserved()
            | DeadlineReached()
            | EvaluationInspected()
            | EvaluationWaitReopened()
        ):
            return _suspension_event(result, event)
        case EvaluationDispatchStopped(stopped=stopped):
            result.lifecycle = result.lifecycle.model_copy(update={"stopped": stopped})
        case SettlementProposed():
            result = _settle(result, event)
        case AttemptBoundReached():
            return _end_bounded_attempt(result, event), ()
        case InterruptedTurnReplaced():
            return _replace_interrupted(result, event)
        case (
            PrepareIntent()
            | DispatchIntent()
            | CompleteIntent()
            | BlockIntent()
            | RecoveryStarted()
        ):
            result, requests = _ledger_event(result, event)
    return result, requests


def _suspension_event(
    state: DynamicState,
    event: WorkerAwaitingEvaluation
    | EvaluationSettled
    | ProfilerSettled
    | EvaluationObserved
    | DeadlineReached
    | EvaluationInspected
    | EvaluationWaitReopened,
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    match event:
        case WorkerAwaitingEvaluation():
            return _await_evaluations(state, event)
        case EvaluationObserved():
            return _evaluation_settled(
                state,
                EvaluationSettled(
                    **event.model_dump(exclude={"kind"}), outcome=EvaluationOutcome.UNKNOWN
                ),
            )
        case EvaluationSettled() | ProfilerSettled():
            return (
                _profiler_settled(state, event)
                if isinstance(event, ProfilerSettled)
                else _evaluation_settled(state, event)
            )
        case DeadlineReached():
            return _deadline_reached(state, event)
        case EvaluationInspected():
            return _evaluation_inspected(state, event)
        case EvaluationWaitReopened():
            return _reopen_evaluation_wait(state, event)


def _withdraw(
    result: DynamicState, scope_id: str, kind: IntentKind
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    if kind not in {IntentKind.PARK, IntentKind.CANCEL}:
        message = f"withdraw kind must be park or cancel: {kind}"
        raise ValueError(message)
    entries = [*result.workstreams, *result.profiles]
    item = next(item for item in entries if planned_id(item.plan) == scope_id)
    if any(record.round_number == item.sequence for record in result.search.rounds):
        message = f"workstream {scope_id!r} is already settled"
        raise AlreadySettledError(message)
    intent = _withdrawal_intent(
        result,
        scope_id,
        item.sequence,
        kind,
        parked=isinstance(item, DynamicWorkstream) and item.phase is WorkstreamPhase.PARKED,
    )
    if intent.stage is IntentStage.COMPLETED:
        return result, ()
    result.lifecycle, _ = ledger_step(result.lifecycle, PrepareIntent(intent=intent))
    continuations = {
        key: continuation.model_copy(
            update={
                "status": ContinuationStatus.PARKED
                if kind is IntentKind.PARK
                else ContinuationStatus.CANCELLED,
                "park_operation_id": intent.operation_id
                if kind is IntentKind.PARK
                else continuation.park_operation_id,
            }
        )
        if continuation.scope_id == scope_id
        and continuation.generation == item.sequence
        and continuation_pending(result.lifecycle, key)
        else continuation
        for key, continuation in result.lifecycle.continuations.items()
    }
    result.lifecycle = result.lifecycle.model_copy(update={"continuations": continuations})
    return result, (intent,)


def _withdrawal_intent(
    state: DynamicState, scope_id: str, generation: int, kind: IntentKind, *, parked: bool
) -> LifecycleIntent:
    owned = [
        intent
        for intent in state.lifecycle.intents.values()
        if (intent.scope_id, intent.generation) == (scope_id, generation)
    ]
    if kind is IntentKind.PARK and any(intent.kind is IntentKind.CANCEL for intent in owned):
        message = "park cannot reopen a cancelled generation"
        raise EvaluationContinuationError(message)
    same = [intent for intent in owned if intent.kind is kind]
    pending = next((intent for intent in same if intent.stage is not IntentStage.COMPLETED), None)
    if pending is not None:
        return pending.model_copy(update={"stage": IntentStage.PREPARED})
    if parked and kind is IntentKind.PARK and same:
        return same[-1]
    suffix = kind.value if not same else f"{kind.value}-{len(same) + 1}"
    return LifecycleIntent(
        operation_id=f"{scope_id}/{generation}/{suffix}",
        scope_id=scope_id,
        generation=generation,
        kind=kind,
    )


def _ledger_event(
    state: DynamicState, event: LifecycleEvent
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    owners = {item.hypothesis_id: item for item in state.workstreams}
    continuations = {}
    for key, continuation in state.lifecycle.continuations.items():
        owner = owners.get(continuation.scope_id)
        if (
            owner is None
            or owner.sequence != continuation.generation
            or owner.phase
            in {WorkstreamPhase.CANCELLED, WorkstreamPhase.EVALUATED, WorkstreamPhase.FAILED}
        ):
            continuations[key] = continuation.model_copy(
                update={"status": ContinuationStatus.CANCELLED}
            )
        else:
            continuations[key] = continuation
    if isinstance(event, DispatchIntent):
        dispatched = state.lifecycle.intents[event.operation_id]
        if dispatched.kind is IntentKind.RESUME and any(
            intent.scope_id == dispatched.scope_id
            and intent.generation == dispatched.generation
            and intent.kind is IntentKind.REOPEN
            and intent.stage is not IntentStage.COMPLETED
            for intent in state.lifecycle.intents.values()
        ):
            return state, ()
    lifecycle = state.lifecycle.model_copy(update={"continuations": continuations})
    state.lifecycle, requests = ledger_step(lifecycle, event)
    for request in requests:
        if isinstance(event, DispatchIntent) and isinstance(request, ResumeAgentTurn):
            _reserve_notes(state, request.scope_id, request.invocation_id)
    if isinstance(event, CompleteIntent):
        intent = state.lifecycle.intents[event.operation_id]
        if intent.kind is IntentKind.RESUME:
            _acknowledge_notes(state, intent.scope_id, intent.operation_id)
    return state, requests


def _reserve_notes(state: DynamicState, scope_id: str, invocation_id: str) -> None:
    if state.agent is None:
        return
    state.agent.steers[scope_id] = [
        note.model_copy(update={"reserved_to": invocation_id})
        if note.delivered_to is None and note.dropped is None and note.reserved_to is None
        else note
        for note in state.agent.steers.get(scope_id, [])
    ]


def _acknowledge_notes(state: DynamicState, scope_id: str, invocation_id: str) -> None:
    if state.agent is None:
        return
    state.agent.steers[scope_id] = [
        note.model_copy(update={"delivered_to": invocation_id})
        if note.delivered_to is None and note.dropped is None and note.reserved_to == invocation_id
        else note
        for note in state.agent.steers.get(scope_id, [])
    ]


def _await_evaluations(
    state: DynamicState, event: WorkerAwaitingEvaluation
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    continuation = event.continuation
    existing = state.lifecycle.continuations.get(continuation.continuation_id)
    if existing is not None:
        if (
            existing.model_copy(
                update={
                    "settlements": continuation.settlements,
                    "evidence_ids": continuation.evidence_ids,
                    "status": continuation.status,
                    "park_operation_id": continuation.park_operation_id,
                    "progress": continuation.progress,
                    "timed_out": continuation.timed_out,
                }
            )
            != continuation
        ):
            message = "conflicting continuation_id"
            raise EvaluationContinuationError(message)
        return state, ()
    if continuation.timed_out is not None:
        message = "new continuation.timed_out must be absent; DeadlineReached owns expiry"
        raise EvaluationContinuationError(message)
    item = next(
        (item for item in state.workstreams if item.hypothesis_id == continuation.scope_id), None
    )
    if item is None or item.sequence != continuation.generation:
        message = "continuation refers to an unowned workstream generation"
        raise EvaluationContinuationError(message)
    if item.phase.value != continuation.original_stage:
        message = "continuation.original_stage differs from workstream phase"
        raise EvaluationContinuationError(message)
    if any(
        intent.scope_id == continuation.scope_id
        and intent.generation == continuation.generation
        and (
            intent.kind is IntentKind.CANCEL
            or (intent.kind is IntentKind.PARK and intent.stage is not IntentStage.COMPLETED)
        )
        for intent in state.lifecycle.intents.values()
    ):
        message = "continuation cannot yield in a withdrawn generation"
        raise EvaluationContinuationError(message)
    yielded = state.lifecycle.intents.get(continuation.yielded_invocation_id)
    if (
        yielded is None
        or yielded.kind not in {IntentKind.TURN, IntentKind.RESUME}
        or (yielded.scope_id, yielded.generation)
        != (continuation.scope_id, continuation.generation)
    ):
        message = "continuation.yielded_invocation_id requires an owned turn"
        raise EvaluationContinuationError(message)
    _validate_yielded_turn(state, continuation, yielded)
    outstanding = state.lifecycle.model_copy(
        update={
            "continuations": {
                key: old
                for key, old in state.lifecycle.continuations.items()
                if f"{old.continuation_id}/resume" != continuation.yielded_invocation_id
            }
        }
    )
    if awaiting_evaluation(outstanding, continuation.scope_id, continuation.generation):
        message = "scope generation already owns an unfinished continuation"
        raise EvaluationContinuationError(message)
    if any(
        old.yielded_invocation_id == continuation.yielded_invocation_id
        for old in state.lifecycle.continuations.values()
    ):
        message = "yielded invocation already owns a continuation"
        raise EvaluationContinuationError(message)
    state.lifecycle, _ = ledger_step(
        state.lifecycle, CompleteIntent(operation_id=yielded.operation_id)
    )
    _acknowledge_notes(state, continuation.scope_id, yielded.operation_id)
    observe = LifecycleIntent(
        operation_id=f"{continuation.continuation_id}/observe",
        scope_id=continuation.scope_id,
        generation=continuation.generation,
        kind=IntentKind.OBSERVE,
        continuation_id=continuation.continuation_id,
    )
    lifecycle = state.lifecycle.model_copy(
        update={
            "continuations": {
                **state.lifecycle.continuations,
                continuation.continuation_id: continuation,
            }
        }
    )
    state.lifecycle, _ = ledger_step(lifecycle, PrepareIntent(intent=observe))
    return _prepare_resume(state, continuation) if continuation.settled else (state, ())


def _validate_yielded_turn(
    state: DynamicState, continuation: DependencyContinuation, yielded: LifecycleIntent
) -> None:
    if yielded.kind is IntentKind.RESUME:
        previous = state.lifecycle.continuations.get(yielded.continuation_id or "")
        if previous is None or (
            continuation.session_key,
            continuation.role,
            continuation.original_stage,
        ) != (previous.session_key, previous.role, previous.original_stage):
            message = "resumed continuation must preserve session_key, role and original_stage"
            raise EvaluationContinuationError(message)
    if yielded.stage is not IntentStage.DISPATCHED:
        message = "continuation requires a dispatched yielded turn"
        raise EvaluationContinuationError(message)


def _profiler_settled(
    state: DynamicState, event: ProfilerSettled
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    continuation = state.lifecycle.continuations.get(event.continuation_id)
    if continuation is None or event.handle in continuation.settlements:
        return state, ()
    dependency = next(
        (item for item in continuation.dependencies if item.handle == event.handle), None
    )
    if (
        not isinstance(dependency, ProfilerDependency)
        or (dependency.principal_id, dependency.request_digest)
        != (event.principal_id, event.request_digest)
        or event.outcome is EvaluationOutcome.UNKNOWN
    ):
        return state, ()
    continuation = continuation.model_copy(
        update={"settlements": {**continuation.settlements, event.handle: event.outcome}}
    )
    state = _store_continuation(state, continuation)
    return _prepare_resume(state, continuation) if continuation.settled else (state, ())


def _evaluation_settled(
    state: DynamicState, event: EvaluationSettled
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    continuation = state.lifecycle.continuations.get(event.continuation_id)
    if continuation is None or (event.scope_id, event.generation) != (
        continuation.evaluation_scope_id,
        continuation.evaluation_generation,
    ):
        return state, ()
    dependency = next(
        (
            dependency
            for dependency in continuation.dependencies
            if dependency.handle == event.handle
        ),
        None,
    )
    if (
        dependency is None
        or isinstance(dependency, ProfilerDependency)
        or (
            dependency.candidate_digest,
            dependency.evaluator_digest,
            dependency.workload_digest,
            dependency.environment_digest,
        )
        != (
            event.candidate_digest,
            event.evaluator_digest,
            event.workload_digest,
            event.environment_digest,
        )
        or event.handle in continuation.settlements
        or continuation.timed_out is not None
    ):
        return state, ()
    continuation = _observed_progress(continuation, event)
    state = _store_continuation(state, continuation)
    if continuation.deadline_at_s is not None and event.at_s >= continuation.deadline_at_s:
        return _deadline_reached(
            state,
            DeadlineReached(
                continuation_id=continuation.continuation_id,
                at_s=event.at_s,
            ),
        )
    if event.outcome is EvaluationOutcome.UNKNOWN:
        return state, ()
    continuation = continuation.model_copy(
        update={
            "settlements": {**continuation.settlements, event.handle: event.outcome},
            "evidence_ids": {
                **continuation.evidence_ids,
                event.handle: event.evidence_ids,
            },
        }
    )
    state.lifecycle = state.lifecycle.model_copy(
        update={
            "continuations": {
                **state.lifecycle.continuations,
                continuation.continuation_id: continuation,
            }
        }
    )
    return _prepare_resume(state, continuation) if continuation.settled else (state, ())


def _observed_progress(
    continuation: DependencyContinuation,
    event: EvaluationSettled,
) -> DependencyContinuation:
    if event.outcome is not EvaluationOutcome.UNKNOWN:
        return continuation
    previous = continuation.progress.get(event.handle)
    if (
        previous is not None
        and previous.observed_at_s is not None
        and event.at_s < previous.observed_at_s
    ):
        return continuation
    progress = EvaluationProgress(
        observation_state=event.observation_state,
        observed_at_s=event.at_s,
        stage=event.stage,
        queued_seconds=event.queued_seconds,
        ran_seconds=event.ran_seconds,
        pending_reason=event.pending_reason,
        estimated_start_s=event.estimated_start_s,
    )
    return continuation.model_copy(
        update={"progress": {**continuation.progress, event.handle: progress}}
    )


def _store_continuation(state: DynamicState, continuation: DependencyContinuation) -> DynamicState:
    state.lifecycle = state.lifecycle.model_copy(
        update={
            "continuations": {
                **state.lifecycle.continuations,
                continuation.continuation_id: continuation,
            }
        }
    )
    return state


def _deadline_reached(
    state: DynamicState,
    event: DeadlineReached,
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    continuation = state.lifecycle.continuations.get(event.continuation_id)
    if (
        continuation is None
        or continuation.deadline_at_s is None
        or event.at_s < continuation.deadline_at_s
        or continuation.ready_to_resume
        or not continuation_pending(state.lifecycle, event.continuation_id)
    ):
        return state, ()
    details = tuple(
        EvaluationTimeout(
            handle=dependency.handle,
            stage=continuation.progress.get(dependency.handle, EvaluationProgress()).stage,
            queued_seconds=continuation.progress.get(
                dependency.handle, EvaluationProgress()
            ).queued_seconds,
            ran_seconds=continuation.progress.get(
                dependency.handle, EvaluationProgress()
            ).ran_seconds,
        )
        for dependency in continuation.dependencies
        if dependency.handle not in continuation.settlements
    )
    continuation = continuation.model_copy(
        update={
            "timed_out": TimedOut(
                deadline_at_s=continuation.deadline_at_s,
                reached_at_s=event.at_s,
                evaluations=details,
            )
        }
    )
    intents = dict(state.lifecycle.intents)
    observe_id = f"{continuation.continuation_id}/observe"
    intents[observe_id] = intents[observe_id].model_copy(update={"stage": IntentStage.COMPLETED})
    for index, dependency in enumerate(continuation.dependencies):
        if dependency.handle in continuation.settlements:
            continue
        progress = continuation.progress.get(dependency.handle, EvaluationProgress())
        kind = (
            IntentKind.INSPECT_EVALUATION
            if progress.observation_state == "unknown"
            else IntentKind.CANCEL_EVALUATION
        )
        intent = _evaluation_intent(continuation, index, kind)
        intents[intent.operation_id] = intent
    item = next(
        (item for item in state.workstreams if item.hypothesis_id == continuation.scope_id), None
    )
    if (
        item is None
        or item.sequence != continuation.generation
        or item.phase
        in {WorkstreamPhase.CANCELLED, WorkstreamPhase.EVALUATED, WorkstreamPhase.FAILED}
    ):
        continuation = continuation.model_copy(update={"status": ContinuationStatus.CANCELLED})
    if continuation.status is ContinuationStatus.ACTIVE:
        resume_id = f"{continuation.continuation_id}/resume"
        intents[resume_id] = LifecycleIntent(
            operation_id=resume_id,
            scope_id=continuation.scope_id,
            generation=continuation.generation,
            kind=IntentKind.RESUME,
            continuation_id=continuation.continuation_id,
        )
    # Timeout, termination authority, and successor resume are one validated transaction.
    state.lifecycle = state.lifecycle.model_copy(
        update={
            "intents": intents,
            "continuations": {
                **state.lifecycle.continuations,
                continuation.continuation_id: continuation,
            },
        }
    )
    state.lifecycle, requests = ledger_step(state.lifecycle, RecoveryStarted())
    return state, tuple(
        request
        for request in requests
        if isinstance(request, (CancelEvaluation, InspectEvaluation, ResumeAgentTurn))
        and request.continuation.continuation_id == continuation.continuation_id
    )


def _evaluation_intent(
    continuation: DependencyContinuation,
    index: int,
    kind: IntentKind,
) -> LifecycleIntent:
    suffix = "inspect" if kind is IntentKind.INSPECT_EVALUATION else "cancel"
    return LifecycleIntent(
        operation_id=f"{continuation.continuation_id}/{suffix}-{index}",
        scope_id=continuation.scope_id,
        generation=continuation.generation,
        kind=kind,
        continuation_id=continuation.continuation_id,
        evaluation_index=index,
    )


def _evaluation_inspected(
    state: DynamicState,
    event: EvaluationInspected,
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    intent = state.lifecycle.intents.get(event.operation_id)
    if intent is None or intent.kind is not IntentKind.INSPECT_EVALUATION:
        message = "evaluation inspection requires its owned inspect intent"
        raise EvaluationContinuationError(message)
    if intent.stage in {IntentStage.COMPLETED, IntentStage.BLOCKED}:
        return state, ()
    continuation = state.lifecycle.continuations[intent.continuation_id or ""]
    index = intent.evaluation_index
    if index is None:
        message = "evaluation inspection requires evaluation_index"
        raise EvaluationContinuationError(message)
    cancel_id = f"{continuation.continuation_id}/cancel-{index}"
    if event.outcome is EvaluationOutcome.UNKNOWN and event.observation_state == "unknown":
        blocked = _evaluation_intent(continuation, index, IntentKind.CANCEL_EVALUATION).model_copy(
            update={"stage": IntentStage.BLOCKED}
        )
        # Inspection and unresolved termination commit atomically; blocked intent authorizes no I/O.
        state.lifecycle = state.lifecycle.model_copy(
            update={
                "intents": {
                    **state.lifecycle.intents,
                    intent.operation_id: intent.model_copy(update={"stage": IntentStage.BLOCKED}),
                    cancel_id: blocked,
                }
            }
        )
        return state, ()
    if event.outcome is not EvaluationOutcome.UNKNOWN:
        state.lifecycle, _ = ledger_step(
            state.lifecycle, CompleteIntent(operation_id=intent.operation_id)
        )
        return state, ()
    handle = continuation.dependencies[index].handle
    previous = continuation.progress.get(handle, EvaluationProgress())
    progress = previous.model_copy(update={"observation_state": event.observation_state})
    continuation = continuation.model_copy(
        update={"progress": {**continuation.progress, handle: progress}}
    )
    cancel = _evaluation_intent(continuation, index, IntentKind.CANCEL_EVALUATION)
    state.lifecycle = state.lifecycle.model_copy(
        update={
            "intents": {
                **state.lifecycle.intents,
                intent.operation_id: intent.model_copy(update={"stage": IntentStage.COMPLETED}),
                cancel_id: cancel,
            },
            "continuations": {
                **state.lifecycle.continuations,
                continuation.continuation_id: continuation,
            },
        }
    )
    state.lifecycle, requests = ledger_step(state.lifecycle, DispatchIntent(operation_id=cancel_id))
    return state, requests


def _prepare_resume(
    state: DynamicState, continuation: DependencyContinuation
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    state.lifecycle, _ = ledger_step(
        state.lifecycle, CompleteIntent(operation_id=f"{continuation.continuation_id}/observe")
    )
    item = next(
        (item for item in state.workstreams if item.hypothesis_id == continuation.scope_id), None
    )
    if (
        item is None
        or item.sequence != continuation.generation
        or item.phase
        in {WorkstreamPhase.CANCELLED, WorkstreamPhase.EVALUATED, WorkstreamPhase.FAILED}
    ):
        return state, ()
    operation_id = f"{continuation.continuation_id}/resume"
    if (
        continuation.status is not ContinuationStatus.ACTIVE
        or operation_id in state.lifecycle.intents
    ):
        return state, ()
    intent = LifecycleIntent(
        operation_id=operation_id,
        scope_id=continuation.scope_id,
        generation=continuation.generation,
        kind=IntentKind.RESUME,
        continuation_id=continuation.continuation_id,
    )
    state.lifecycle, _ = ledger_step(state.lifecycle, PrepareIntent(intent=intent))
    return state, ()


def evaluation_wait_reopen(
    continuation_id: str, resolved_cancelled_handles: tuple[str, ...]
) -> EvaluationWaitReopened:
    """Build the policy command that explicitly resolves a parked wait's cancellations."""
    return EvaluationWaitReopened(
        continuation_id=continuation_id, resolved_cancelled_handles=resolved_cancelled_handles
    )


def _reopen_evaluation_wait(
    state: DynamicState, event: EvaluationWaitReopened
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    continuation = state.lifecycle.continuations[event.continuation_id]
    resume = state.lifecycle.intents.get(f"{event.continuation_id}/resume")
    if resume is not None and resume.stage is IntentStage.COMPLETED:
        return state, ()
    if continuation.status is ContinuationStatus.CANCELLED:
        message = "cancelled continuation cannot reopen"
        raise EvaluationContinuationError(message)
    if continuation.status is ContinuationStatus.ACTIVE:
        return state, ()
    park = state.lifecycle.intents.get(continuation.park_operation_id or "")
    if park is None or park.stage is not IntentStage.COMPLETED:
        message = "reopen requires completed park cleanup"
        raise EvaluationContinuationError(message)
    item_index = next(
        (
            index
            for index, item in enumerate(state.workstreams)
            if item.hypothesis_id == continuation.scope_id
            and item.sequence == continuation.generation
        ),
        None,
    )
    if item_index is None or state.workstreams[item_index].phase is not WorkstreamPhase.PARKED:
        message = "reopen requires the current parked workstream generation"
        raise EvaluationContinuationError(message)
    cancelled = {
        handle
        for handle, outcome in continuation.settlements.items()
        if outcome is EvaluationOutcome.CANCELLED
    }
    if not continuation.ready_to_resume or set(event.resolved_cancelled_handles) != cancelled:
        message = "reopen must explicitly resolve all cancelled dependencies"
        raise EvaluationContinuationError(message)
    state.workstreams[item_index] = state.workstreams[item_index].model_copy(
        update={"phase": WorkstreamPhase(continuation.original_stage)}
    )
    continuation = continuation.model_copy(update={"status": ContinuationStatus.ACTIVE})
    state = _activate_continuation(state, continuation)
    reopen = LifecycleIntent(
        operation_id=f"{continuation.park_operation_id}/reopen",
        scope_id=continuation.scope_id,
        generation=continuation.generation,
        kind=IntentKind.REOPEN,
    )
    state.lifecycle, _ = ledger_step(state.lifecycle, PrepareIntent(intent=reopen))
    return _prepare_resume(state, continuation)


def _activate_continuation(
    state: DynamicState, continuation: DependencyContinuation
) -> DynamicState:
    intents = dict(state.lifecycle.intents)
    resume_id = f"{continuation.continuation_id}/resume"
    if continuation.timed_out is not None and resume_id not in intents:
        intents[resume_id] = LifecycleIntent(
            operation_id=resume_id,
            scope_id=continuation.scope_id,
            generation=continuation.generation,
            kind=IntentKind.RESUME,
            continuation_id=continuation.continuation_id,
        )
    state.lifecycle = state.lifecycle.model_copy(
        update={
            "intents": intents,
            "continuations": {
                **state.lifecycle.continuations,
                continuation.continuation_id: continuation,
            },
        }
    )
    return state


def validate_workstream_replacement(state: DynamicState, scope_id: str) -> None:
    """A planner cannot replace a parked wait without resolving cancelled handles."""
    if any(
        continuation.scope_id == scope_id and continuation.status is ContinuationStatus.PARKED
        for continuation in state.lifecycle.continuations.values()
    ):
        message = f"{scope_id}: parked evaluation wait requires explicit handle resolution"
        raise EvaluationContinuationError(message)


def _replace_interrupted(
    state: DynamicState,
    event: InterruptedTurnReplaced,
) -> tuple[DynamicState, tuple[LifecycleRequest, ...]]:
    index = next(
        index
        for index, item in enumerate(state.workstreams)
        if item.hypothesis_id == event.scope_id
    )
    item = state.workstreams[index]
    intents = [
        intent
        for intent in state.lifecycle.intents.values()
        if intent.scope_id == item.hypothesis_id
        and intent.generation == item.sequence
        and intent.kind is IntentKind.INTERRUPT
        and intent.stage is not IntentStage.COMPLETED
    ]
    if not intents:
        completed = [
            intent
            for intent in state.lifecycle.intents.values()
            if intent.scope_id == item.hypothesis_id
            and intent.generation == item.sequence
            and intent.kind is IntentKind.INTERRUPT
            and intent.stage is IntentStage.COMPLETED
            and intent.resume_revision == event.revision
        ]
        if completed:
            return state, ()
        message = f"{event.scope_id}: replacement requires a pending interrupt intent"
        raise ValueError(message)
    budget = item.budget.refund_interrupted(event.retry_limit)
    if budget is None:
        message = f"{event.scope_id}: interrupted past its refund bound"
        raise ValueError(message)
    sequence = item.invocation_sequence + 1
    invocation_id = f"{item.hypothesis_id}/dynamic-implementer/invocation-{sequence}"
    replacement = LifecycleIntent(
        operation_id=invocation_id,
        scope_id=item.hypothesis_id,
        generation=item.sequence,
        kind=IntentKind.TURN,
        invocation_id=invocation_id,
    )
    state.workstreams[index] = item.model_copy(
        update={
            "phase": WorkstreamPhase.IMPLEMENTING,
            "candidate_revision": event.revision,
            "budget": budget.charge(),
            "invocation_sequence": sequence,
        }
    )
    for intent in intents:
        state.lifecycle, _ = ledger_step(
            state.lifecycle,
            CompleteIntent(operation_id=intent.operation_id, resume_revision=event.revision),
        )
    state.lifecycle, _ = ledger_step(state.lifecycle, PrepareIntent(intent=replacement))
    if state.agent is not None:
        notes = state.agent.steers.get(item.hypothesis_id, [])
        state.agent.steers[item.hypothesis_id] = [
            note.model_copy(update={"reserved_to": invocation_id})
            if note.delivered_to is None and note.dropped is None and note.reserved_to is None
            else note
            for note in notes
        ]
    return state, (replacement,)


def _settle(state: DynamicState, event: SettlementProposed) -> DynamicState:
    intent = state.lifecycle.intents[event.operation_id]
    index = next(
        index
        for index, item in enumerate(state.workstreams)
        if item.hypothesis_id == intent.scope_id and item.sequence == intent.generation
    )
    item = state.workstreams[index]
    recorded = any(record.round_number == item.sequence for record in state.search.rounds)
    if not recorded:
        if intent.kind is IntentKind.PARK:
            budget = item.budget
            if item.phase is WorkstreamPhase.IMPLEMENTING and not awaiting_evaluation(
                state.lifecycle, item.hypothesis_id, item.sequence
            ):
                budget = budget.refund_interrupted(event.retry_limit) or budget
            state.workstreams[index] = item.model_copy(
                update={"phase": WorkstreamPhase.PARKED, "budget": budget}
            )
        elif intent.kind is IntentKind.CANCEL:
            if event.record is None:
                message = "cancel settlement requires a round proposal"
                raise ValueError(message)
            if event.record.round_number != intent.generation:
                message = "settlement record.round_number must match intent.generation"
                raise ValueError(message)
            record = replace(
                event.record,
                passed=False,
                hypothesis_outcome=HypothesisOutcome.INCONCLUSIVE.value,
                hypothesis_declared_outcome=HypothesisOutcome.INCONCLUSIVE.value,
                candidate_disposition=CandidateDisposition.DISCARD.value,
                candidate_retained=False,
            )
            state.workstreams[index] = item.model_copy(update={"phase": WorkstreamPhase.CANCELLED})
            active = state.search.model_copy(
                update={"active_hypothesis_id": item.hypothesis_id}, deep=True
            )
            state.search = hypothesis_transitions.append_round(active, record, keep_active=False)
            if state.search.active_hypothesis_id is not None:
                state.search = hypothesis_transitions.finish_hypothesis(state.search)
            _drop_notes(state, item.hypothesis_id, event.drop_journal)
        else:
            message = f"settlement cannot complete {intent.kind}"
            raise ValueError(message)
    _settle_children(state, state.workstreams[index], intent)
    acknowledgement = (
        BlockIntent(operation_id=event.operation_id)
        if event.unresolved
        else CompleteIntent(operation_id=event.operation_id)
    )
    state.lifecycle, _ = ledger_step(state.lifecycle, acknowledgement)
    return state


def _settle_children(state: DynamicState, item: DynamicWorkstream, intent: LifecycleIntent) -> None:
    """Drain acknowledged child turns; fence ambiguous note delivery on park."""
    for child in tuple(state.lifecycle.intents.values()):
        if child.scope_id != item.hypothesis_id or child.generation != item.sequence:
            continue
        if intent.kind is IntentKind.PARK and child.kind in {IntentKind.OBSERVE, IntentKind.RESUME}:
            continue
        if (
            child.kind
            not in {IntentKind.TURN, IntentKind.INTERRUPT, IntentKind.OBSERVE, IntentKind.RESUME}
            or child.stage is IntentStage.COMPLETED
        ):
            continue
        notes = state.agent.steers.get(item.hypothesis_id, []) if state.agent is not None else []
        reserved = [
            note
            for note in notes
            if note.reserved_to == child.operation_id
            and note.delivered_to is None
            and note.dropped is None
        ]
        if intent.kind is IntentKind.PARK and child.stage is IntentStage.DISPATCHED and reserved:
            state.lifecycle, _ = ledger_step(
                state.lifecycle, BlockIntent(operation_id=child.operation_id)
            )
        else:
            state.lifecycle, _ = ledger_step(
                state.lifecycle,
                CompleteIntent(
                    operation_id=child.operation_id,
                    resume_revision=item.candidate_revision
                    if child.kind is IntentKind.INTERRUPT
                    else None,
                ),
            )
            if (
                intent.kind is IntentKind.PARK
                and child.stage is IntentStage.PREPARED
                and state.agent is not None
            ):
                state.agent.steers[item.hypothesis_id] = [
                    note.model_copy(update={"reserved_to": None}) if note in reserved else note
                    for note in notes
                ]


def _drop_notes(state: DynamicState, scope_id: str, journal: tuple[JournalEntry, ...]) -> None:
    if state.agent is None:
        if journal:
            message = "drop_journal requires agent state"
            raise ValueError(message)
        return
    notes = state.agent.steers.get(scope_id, [])
    pending = [note for note in notes if note.delivered_to is None and note.dropped is None]
    if len(pending) != len(journal):
        message = f"drop_journal for {scope_id!r} must contain one entry per pending steer"
        raise ValueError(message)
    state.agent.steers[scope_id] = [
        note.model_copy(update={"dropped": "workstream_settled"})
        if note.delivered_to is None and note.dropped is None
        else note
        for note in notes
    ]
    state.agent.journal.extend(journal)


__all__ = [
    "AlreadySettledError",
    "AttemptBoundReached",
    "DeadlineReached",
    "EnvelopeEvent",
    "EvaluationContinuationError",
    "EvaluationDispatchStopped",
    "EvaluationInspected",
    "EvaluationObserved",
    "EvaluationSettled",
    "EvaluationWaitReopened",
    "InterruptedTurnReplaced",
    "SettlementProposed",
    "WithdrawRequested",
    "WorkerAwaitingEvaluation",
    "evaluation_wait_reopen",
    "step",
    "validate_workstream_replacement",
]


def _end_bounded_attempt(state: DynamicState, event: AttemptBoundReached) -> DynamicState:
    intent = state.lifecycle.intents[event.operation_id]
    permitted = (
        intent.kind is IntentKind.RESUME
        and intent.stage in {IntentStage.PREPARED, IntentStage.DISPATCHED}
    ) or (intent.kind is IntentKind.TURN and intent.stage is IntentStage.DISPATCHED)
    if not permitted:
        message = "attempt bound requires an owned unfinished turn or resume"
        raise EvaluationContinuationError(message)
    state.lifecycle, _ = ledger_step(
        state.lifecycle, CompleteIntent(operation_id=event.operation_id)
    )
    if intent.stage is IntentStage.DISPATCHED:
        _acknowledge_notes(state, intent.scope_id, intent.operation_id)
    index = next(
        index
        for index, item in enumerate(state.workstreams)
        if (item.hypothesis_id, item.sequence) == (intent.scope_id, intent.generation)
    )
    state.workstreams[index] = state.workstreams[index].model_copy(
        update={
            "phase": WorkstreamPhase.FAILED,
            "feedback": event.reason,
            "last_error": event.reason,
            "implementation": None,
            "review": None,
            "evaluation": None,
        }
    )
    return state
