"""Intent ledger: the core's durable record of every request it has authorized.

A request is issued (registered PREPARED by the kernel), dispatched (committed
before any I/O), observed, and finally terminal. The ledger decides which
observations are fresh, forwards each fresh one to its owning area, and
completes decisions it owns. Observations are never accepted with a
non-increasing sequence: duplicates and older sequences are inert, a different
fact at an accepted sequence is a typed rejection.

Unknown acceptance and retryable failures keep the intent open for a later,
higher-sequence observation; only a conclusive terminal observation closes it,
exactly once. Recovery (``_intent_recovery``) reads this ledger and never writes
its request fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._intent_forward import declaration, intents_own, owner_signals
from ._outcomes import prove_outcome
from ._proofs import (
    Mismatch,
    Missing,
    ProofField,
    Proven,
    accepted_receipt_for,
    fresh_observation,
    observation_for,
    released_owner,
)
from ._registry import ContractError
from ._values import digest
from .types.attempts import RevisionOperationRequested
from .types.common import (
    CompletionStatus,
    ContractValidationError,
    EventId,
    ExecuteRegisteredOperation,
    LifecycleClass,
    Observation,
    ObservationStatus,
    OperationNormalizationKind,
    RequestId,
    RevisionAuthority,
    SetupFailureKind,
)
from .types.evaluation import RegisteredJobRequested
from .types.intents import (
    BlockIntent,
    CancelOwnedResource,
    DecisionDependencyResolved,
    DispatchAuthorized,
    InspectRequest,
    Intent,
    IntentPhase,
    IntentsState,
    OperationResult,
    OperationRetireRequested,
    RequestObserved,
    RequestPrepared,
    TargetObservation,
    request_lifecycle,
)
from .types.kernel import AreaChange, DecisionCompleted
from .types.sessions import RegisteredTurnRequested
from .types.strategy import Cancel, Withdraw

if TYPE_CHECKING:
    from .types.common import DecisionId
    from .types.intents import IntentsEvent, Request
    from .types.kernel import IntentsContext, Signal, StrategyEvent

    type Facts = RequestObserved | TargetObservation

_CONCLUSIVE = (
    ObservationStatus.SUCCEEDED,
    ObservationStatus.FAILED,
    ObservationStatus.REJECTED,
    ObservationStatus.CANCELLED,
)
_REPLAYABLE = (LifecycleClass.QUERY, LifecycleClass.IDEMPOTENT_WRITE)
_COMPLETION = {
    ObservationStatus.SUCCEEDED: CompletionStatus.SUCCEEDED,
    ObservationStatus.CANCELLED: CompletionStatus.CANCELLED,
    ObservationStatus.FAILED: CompletionStatus.FAILED,
    ObservationStatus.REJECTED: CompletionStatus.FAILED,
}


def advance(
    state: IntentsState, context: IntentsContext, event: IntentsEvent
) -> AreaChange[IntentsState]:
    """Consume the five ledger events; recovery events belong to Intents B."""
    match event:
        case RequestPrepared():
            return _prepared(state, context, event)
        case DispatchAuthorized():
            return _dispatched(state, event)
        case RequestObserved():
            return _observed(state, context, event)
        case DecisionDependencyResolved():
            return _dependency_resolved(state, context, event)
        case OperationRetireRequested():
            return _retire(state, context, event)
        case _:
            raise ContractValidationError("event.kind", "event is owned by intent recovery")


def _unique(state: IntentsState, identity: RequestId) -> Intent | None:
    rows = tuple(row for row in state.intents if row.request_id == identity)
    if len(rows) > 1:
        raise ContractError(("request_id", identity.root), "ambiguous canonical request identity")
    return rows[0] if rows else None


def _replace(state: IntentsState, intent: Intent) -> IntentsState:
    return state.model_copy(
        update={
            "intents": tuple(
                intent if row.request_id == intent.request_id else row for row in state.intents
            )
        }
    )


def _conclusive(observation: Observation | None) -> bool:
    return observation is not None and observation.terminal and observation.status in _CONCLUSIVE


def _finish(change: AreaChange[IntentsState], *more: AreaChange[IntentsState]) -> AreaChange:
    """Merge sequential changes; signals and requests keep order without duplicates."""
    merged = change
    for item in more:
        merged = AreaChange(
            state=item.state,
            signals=tuple(dict.fromkeys((*merged.signals, *item.signals))),
            requests=tuple(dict.fromkeys((*merged.requests, *item.requests))),
            events=(*merged.events, *item.events),
        )
    return merged


# --- Registration -----------------------------------------------------------------


def _prepared(
    state: IntentsState, context: IntentsContext, event: RequestPrepared
) -> AreaChange[IntentsState]:
    """Route an accepted proposal to the one area that owns its lifecycle."""
    request = event.request
    if request_lifecycle(request) != event.lifecycle:
        raise ContractError(("lifecycle",), "differs from the canonical request class")
    if request.request_id is not None:
        previous = _unique(state, request.request_id)
        if previous is not None:
            if previous.payload_digest != digest(request):
                raise ContractError(("request_id",), "request identity conflict")
            return AreaChange(state=state)
    if not isinstance(request, ExecuteRegisteredOperation):
        return AreaChange(state=state, requests=(request,))
    return _route_operation(state, context, event, request)


def _route_operation(
    state: IntentsState,
    context: IntentsContext,
    event: RequestPrepared,
    request: ExecuteRegisteredOperation,
) -> AreaChange[IntentsState]:
    descriptor = declaration(context, request)
    if descriptor.normalization == OperationNormalizationKind.SCOPE_REOPEN:
        raise ContractError(("operation",), "scope reopen enters through its own guarded event")
    if descriptor.revision_authority != RevisionAuthority.NONE:
        owner: Signal = RevisionOperationRequested(
            request=request, authority=descriptor.revision_authority
        )
        return AreaChange(state=state, signals=(owner,))
    if descriptor.lifecycle == LifecycleClass.SESSION_TURN:
        if event.normalized_turn is None:
            raise ContractError(("normalized_turn",), "registered turn requires its normalization")
        turn: Signal = RegisteredTurnRequested(request=request, turn=event.normalized_turn)
        return AreaChange(state=state, signals=(turn,))
    if descriptor.resource_pool is not None:
        job: Signal = RegisteredJobRequested(
            request=request,
            resource_pool=descriptor.resource_pool,
            expected_measurement=event.normalized_measurement,
        )
        return AreaChange(state=state, signals=(job,))
    return AreaChange(state=state, requests=(request,))


def _dispatched(state: IntentsState, event: DispatchAuthorized) -> AreaChange[IntentsState]:
    """Record that I/O may start; the kernel already proved dependencies and recovery."""
    intent = _unique(state, event.request_id)
    if intent is None:
        raise ContractError(("request_id",), "dispatch names no canonical request")
    match intent.phase:
        case IntentPhase.PREPARED:
            pass
        case IntentPhase.DISPATCHED:
            return AreaChange(state=state)
        case IntentPhase.RECONCILING if intent.lifecycle in _REPLAYABLE:
            pass
        case _:
            raise ContractError(("phase",), f"{intent.phase.value} request cannot be dispatched")
    updated = intent.model_copy(update={"phase": IntentPhase.DISPATCHED})
    return AreaChange(state=_replace(state, updated))


# --- Observation ------------------------------------------------------------------


def _reject_proof(proof: Mismatch | Missing, path: str) -> ContractError:
    detail = proof.field.value if isinstance(proof, Mismatch) else proof.reason.value
    return ContractError(("observation", path), f"differs from canonical request: {detail}")


def _observed(
    state: IntentsState, context: IntentsContext, event: RequestObserved
) -> AreaChange[IntentsState]:
    """Commit the root fact, then independent root facts about an inspected target."""
    root = _unique(state, event.observation.request_id)
    if root is None:
        raise ContractError(("observation", "request_id"), "observation names no canonical request")
    change = _apply(state, context, root, event)
    target = event.target
    if target is None or target.target_resource is not None:
        return change
    original = _unique(change.state, target.observation.request_id)
    if original is None:
        raise ContractError(("target", "request_id"), "target names no canonical request")
    return _finish(change, _apply(change.state, context, original, target))


def _apply(
    state: IntentsState,
    context: IntentsContext,
    intent: Intent,
    facts: Facts,
    *,
    unsent: bool = False,
) -> AreaChange[IntentsState]:
    observation = facts.observation
    proof = observation_for(intent, observation)
    if not isinstance(proof, Proven):
        raise _reject_proof(proof, "scope")
    if intent.phase == IntentPhase.PREPARED and not unsent:
        raise ContractError(
            ("observation", "request_id"), "observation requires committed dispatch authorization"
        )
    previous = intent.observation
    fresh = fresh_observation(() if previous is None else (previous,), observation, complete=True)
    if isinstance(fresh, Mismatch) and fresh.field == ProofField.SEQUENCE:
        if previous is not None and observation.sequence < previous.sequence:
            return AreaChange(state=state)
        raise ContractError(("observation", "sequence"), "conflicts with the accepted observation")
    if not isinstance(fresh, Proven):
        raise _reject_proof(fresh, "sequence")
    if observation == previous:
        return AreaChange(state=state)
    if intent.phase == IntentPhase.COMPLETED:
        return _refine(state, intent, observation)
    return _advance_phase(state, context, intent, facts)


def _refine(
    state: IntentsState, intent: Intent, observation: Observation
) -> AreaChange[IntentsState]:
    """A closed intent only gains release facts; it never changes disposition."""
    previous = intent.observation
    if (
        previous is None
        or not observation.terminal
        or observation.status != previous.status
        or observation.accepted != previous.accepted
        or observation.resource_id != previous.resource_id
        or (previous.released and not observation.released)
        or (previous.children_complete and not observation.children_complete)
        or not set(previous.children) <= set(observation.children)
    ):
        raise ContractError(("observation",), "terminal request cannot change its disposition")
    updated = intent.model_copy(
        update={"observation": observation, "sequence": observation.sequence}
    )
    return AreaChange(state=_replace(state, updated))


def _retry_limit(context: IntentsContext, intent: Intent) -> int:
    request = intent.request
    if isinstance(request, ExecuteRegisteredOperation):
        return request.retry_limit
    return context.run.limits.max_retries


def _next_phase(
    context: IntentsContext, intent: Intent, observation: Observation
) -> tuple[IntentPhase, int]:
    """Only a conclusive observation closes; everything else keeps the intent open."""
    if _conclusive(observation) and not (
        observation.status == ObservationStatus.SUCCEEDED and not observation.accepted
    ):
        return IntentPhase.COMPLETED, intent.retry_count
    if observation.status == ObservationStatus.UNKNOWN or observation.terminal:
        # Unknown, or success whose acceptance is unproven: inspect before any retry.
        return IntentPhase.RECONCILING, intent.retry_count
    if observation.status in (
        ObservationStatus.FAILED,
        ObservationStatus.REJECTED,
        ObservationStatus.CANCELLED,
    ):
        retries = intent.retry_count + 1
        if retries > _retry_limit(context, intent):
            return IntentPhase.BLOCKED, intent.retry_count
        return IntentPhase.DISPATCHED, retries
    return IntentPhase.DISPATCHED, intent.retry_count


def _record(intent: Intent, facts: Facts, phase: IntentPhase, retries: int) -> Intent:
    observation = facts.observation
    update: dict[str, object] = {
        "phase": phase,
        "retry_count": retries,
        "sequence": observation.sequence,
        "observation": observation,
        "setup_failure": facts.setup_failure,
        "evaluation_result": facts.evaluation_result,
        "suspension": facts.suspension,
    }
    if facts.outcome is not None and facts.operation_schema is not None:
        update.update(
            outcome_schema=facts.operation_schema.outcome_schema,
            outcome_json=facts.outcome_json,
            outcome=facts.outcome,
        )
    return intent.model_copy(update=update)


def _advance_phase(
    state: IntentsState, context: IntentsContext, intent: Intent, facts: Facts
) -> AreaChange[IntentsState]:
    phase, retries = _next_phase(context, intent, facts.observation)
    updated = _record(intent, facts, phase, retries)
    state = _replace(state, updated)
    signals = owner_signals(context, updated, facts)
    events: tuple[StrategyEvent, ...] = ()
    if phase == IntentPhase.COMPLETED:
        state = _block_target(state, updated)
        completion, events = _complete(context, updated, facts)
        change = AreaChange(
            state=state, signals=tuple(dict.fromkeys((*signals, *completion))), events=events
        )
        status = facts.observation.status
        if status != ObservationStatus.SUCCEEDED:
            return _finish(change, _cancel_dependents(change.state, context, updated))
        return change
    return AreaChange(state=state, signals=signals)


def _block_target(state: IntentsState, intent: Intent) -> IntentsState:
    """A recorded block command leaves its target fenced awaiting reconciliation."""
    request = intent.request
    observation = intent.observation
    if (
        not isinstance(request, BlockIntent)
        or observation is None
        or observation.status != ObservationStatus.SUCCEEDED
    ):
        return state
    target = _unique(state, request.target)
    if target is None or target.phase == IntentPhase.COMPLETED:
        return state
    return _replace(state, target.model_copy(update={"phase": IntentPhase.BLOCKED}))


# --- Decision completion -----------------------------------------------------------


def _completable(context: IntentsContext, intent: Intent) -> bool:
    request = intent.request
    if isinstance(request, CancelOwnedResource):
        return True
    return isinstance(request, ExecuteRegisteredOperation) and intents_own(
        declaration(context, request)
    )


def _completion_signal(
    context: IntentsContext, decision_id: DecisionId, status: CompletionStatus
) -> tuple[Signal, ...]:
    """One completion per accepted, unfinished decision."""
    proof = accepted_receipt_for(context.run.receipts, decision_id, None)
    if not isinstance(proof, Proven) or proof.value.completion is not None:
        return ()
    return (DecisionCompleted(decision_id=proof.value.decision_id, status=status),)


def _operation_result(
    intent: Intent, facts: Facts, context: IntentsContext
) -> tuple[StrategyEvent, ...]:
    request = intent.request
    if (
        not isinstance(request, ExecuteRegisteredOperation)
        or not intents_own(declaration(context, request))
        or facts.operation_schema is None
        or facts.outcome is None
        or not facts.outcome_is_registered
    ):
        return ()
    result = OperationResult(
        operation_id=request.operation_id,
        observation=facts.observation,
        outcome_schema=facts.operation_schema.outcome_schema,
        operation_schema=facts.operation_schema,
        outcome_json=facts.outcome_json,
        outcome=facts.outcome,
    )
    return (prove_outcome(result, facts.outcome, facts.operation_schema),)


def _complete(
    context: IntentsContext, intent: Intent, facts: Facts
) -> tuple[tuple[Signal, ...], tuple[StrategyEvent, ...]]:
    if not _completable(context, intent) or intent.request.decision_id is None:
        return (), ()
    status = _COMPLETION[facts.observation.status]
    return (
        _completion_signal(context, intent.request.decision_id, status),
        _operation_result(intent, facts, context),
    )


# --- Dependencies and retirement ----------------------------------------------------


def _unsent_cancellation(context: IntentsContext, intent: Intent) -> TargetObservation:
    """Never-dispatched work is provably unaccepted, so core can close it itself."""
    request = intent.request
    return TargetObservation(
        observation=Observation(
            event_id=EventId(root=f"cancelled-unsent:{intent.request_id.root}"),
            request_id=intent.request_id,
            scope=request.scope,
            sequence=0,
            observed_at=context.run.now_at,
            status=ObservationStatus.CANCELLED,
            accepted=False,
            terminal=True,
            released=True,
            children_complete=True,
            admission_id=request.admission_id,
        ),
        setup_failure=SetupFailureKind.UNKNOWN,
    )


def _cancel_prepared(
    state: IntentsState, context: IntentsContext, doomed: tuple[Intent, ...]
) -> AreaChange[IntentsState]:
    change = AreaChange(state=state)
    for intent in doomed:
        current = _unique(change.state, intent.request_id)
        if current is None or current.phase != IntentPhase.PREPARED:
            continue
        facts = _unsent_cancellation(context, current)
        change = _finish(change, _apply(change.state, context, current, facts, unsent=True))
    return change


def _cancel_dependents(
    state: IntentsState, context: IntentsContext, failed: Intent
) -> AreaChange[IntentsState]:
    """Prepared requests that required this request can never be dispatched."""
    doomed = tuple(
        row
        for row in state.intents
        if row.phase == IntentPhase.PREPARED and failed.request_id in row.request.depends_on
    )
    return _cancel_prepared(state, context, doomed)


def _dependency_resolved(
    state: IntentsState, context: IntentsContext, event: DecisionDependencyResolved
) -> AreaChange[IntentsState]:
    """A failed or cancelled decision releases no dependent: its prepared work is cancelled."""
    if event.status == CompletionStatus.SUCCEEDED:
        return AreaChange(state=state)
    doomed = tuple(
        row
        for row in state.intents
        if row.phase == IntentPhase.PREPARED
        and event.decision_id in row.request.decision_dependencies
    )
    return _cancel_prepared(state, context, doomed)


def _retirement_decision(
    context: IntentsContext, event: OperationRetireRequested
) -> DecisionId | None:
    rows = tuple(
        row.decision_id
        for row in context.run.receipts
        if isinstance(row.decision, Withdraw)
        and isinstance(row.decision.disposition, Cancel)
        and row.decision.target == event.operation
        and row.decision.scope == event.scope
        and isinstance(accepted_receipt_for(context.run.receipts, row.decision_id, None), Proven)
    )
    return rows[0] if len(rows) == 1 else None


def _retire(
    state: IntentsState, context: IntentsContext, event: OperationRetireRequested
) -> AreaChange[IntentsState]:
    """Cancel an owned operation: unsent work is closed, live work gets a bounded command."""
    matches = tuple(
        row
        for row in state.intents
        if isinstance(row.request, ExecuteRegisteredOperation)
        and row.request.operation_id == event.operation.operation_id
        and row.request.scope == event.scope
    )
    if len(matches) != 1:
        raise ContractError(("operation",), "retirement needs one canonical operation")
    intent = matches[0]
    withdraw = _retirement_decision(context, event)
    done = (
        ()
        if withdraw is None
        else _completion_signal(context, withdraw, CompletionStatus.SUCCEEDED)
    )
    if intent.phase == IntentPhase.PREPARED:
        change = _cancel_prepared(state, context, (intent,))
        return _finish(change, AreaChange(state=change.state, signals=done))
    observation = intent.observation
    if intent.phase == IntentPhase.COMPLETED and isinstance(
        released_owner(intent, (intent,)), Proven
    ):
        return AreaChange(state=state, signals=done)
    resource = None if observation is None else observation.resource_id
    request: Request
    if resource is not None:
        request = CancelOwnedResource(
            request_id=RequestId(root=f"retire:{intent.request_id.root}"),
            scope=intent.request.scope,
            admission_id=intent.request.admission_id,
            deadline_at=context.run.now_at + context.run.limits.cancellation_bound,
            resource_id=resource,
            target=intent.request_id,
        )
    else:
        request = InspectRequest(
            request_id=RequestId(root=f"retire-inspect:{intent.request_id.root}"),
            scope=intent.request.scope,
            admission_id=intent.request.admission_id,
            deadline_at=context.run.now_at + context.run.limits.reconciliation_bound,
            target=intent.request_id,
        )
    return AreaChange(state=state, requests=(request,))
