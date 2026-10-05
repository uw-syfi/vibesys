"""Pure FIFO admission and occupancy episodes, with receipt-derived budgets.

Scheduling owns queue and capacity only. Registration, charging, acquisition and
retirement are requested through typed signals, never performed in this leaf.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from ._proofs import (
    Proven,
    accepted_receipt_for,
    admission_remaining,
    committed_stop,
    draining,
)
from .types.attempts import AttemptPhase, RetireRequested
from .types.common import (
    AttemptRef,
    ChargeKind,
    ContractValidationError,
    OperationNormalizationKind,
    OperationSchemaRef,
    RejectionCode,
    RequestId,
    RunStatus,
    WorkspaceMode,
)
from .types.evaluation import ObservationsDue
from .types.intents import RecoveryPhase
from .types.kernel import AreaChange
from .types.scheduling import (
    AdmissionControl,
    AdmitAttempt,
    AttemptReady,
    AttemptReopenRequest,
    AttemptReopenRequested,
    AttemptRequest,
    AttemptRequested,
    ClockAdvanced,
    QueueEntryRetired,
    RegisterAttempt,
    RunDrained,
    SchedulingState,
    Slot,
    SlotChargeEnded,
    SlotReleased,
)
from .types.strategy import Accepted, Operation, Rejected, StartAttempt, Stop

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.common import DecisionId
    from .types.kernel import SchedulingContext, Signal
    from .types.scheduling import AdmissionRequest, SchedulingEvent


def _target(request: AdmissionRequest) -> AttemptRef:
    if isinstance(request, AttemptReopenRequest):
        return request.attempt
    return AttemptRef(attempt_id=request.attempt_id, generation=request.generation)


def _owner(context: SchedulingContext, target: AttemptRef) -> AttemptView | None:
    return next(
        (
            attempt
            for attempt in context.attempts.attempts
            if attempt.attempt_id == target.attempt_id and attempt.generation == target.generation
        ),
        None,
    )


def _mode(
    context: SchedulingContext, target: AttemptRef, admission_id: DecisionId
) -> WorkspaceMode:
    owner = _owner(context, target)
    if owner is not None:
        return owner.workspace.mode
    decision = next(
        (
            receipt.decision
            for receipt in context.run.receipts
            if receipt.decision_id == admission_id
        ),
        None,
    )
    if not isinstance(decision, StartAttempt):
        raise ContractValidationError("decision_id", "admission has no canonical workspace plan")
    return decision.workspace.mode


def _remaining(state: SchedulingState, context: SchedulingContext) -> int:
    # Unregistered queue rows reserve budget during propagation. Once registered,
    # their authoritative receipts replace that reservation, never add to it.
    reserved = sum(
        request.admission_charge
        for request in state.queue
        if isinstance(request, AttemptRequest) and _owner(context, _target(request)) is None
    )
    return admission_remaining(context.attempts.attempts, context.run.limits.max_attempts, reserved)


def _reject(
    state: SchedulingState, request: AdmissionRequest, code: RejectionCode, detail: str
) -> AreaChange[SchedulingState]:
    return AreaChange(
        state=state,
        events=(
            Rejected(
                decision_id=request.decision_id,
                code=code,
                path=("scheduling",),
                detail=detail,
            ),
        ),
    )


def _committed_stops(context: SchedulingContext) -> tuple[Stop, ...]:
    # Restored contracts can carry a rejected payload or mismatched identities.
    # Only the run's accepted, current-scope decision proves Stop authority.
    return tuple(
        receipt.decision
        for receipt in context.run.receipts
        if isinstance(receipt.decision, Stop)
        and isinstance(receipt.feedback, Accepted)
        and receipt.decision_id == receipt.decision.decision_id == receipt.feedback.decision_id
        and receipt.decision.scope.owner == context.run.run_id
        and receipt.decision.scope.generation == context.run.generation
    )


def _first_stop(context: SchedulingContext) -> Stop | None:
    """The shared proof of the first committed Stop, bound to the run's result.

    The shared predicate is stricter than a bare accepted-receipt scan: it also
    checks the persisted digest and that a rejected first Stop denies authority
    instead of letting a later one take over.
    """
    match committed_stop(context.run):
        case Proven(value=stop):
            return stop
        case _:
            return None


def _accepting(context: SchedulingContext) -> bool:
    # admission_closed fences intake. Execution inhibition belongs to the run's
    # durable status, so closing intake cannot strand its already accepted FIFO.
    open_run = context.run.status == RunStatus.RUNNING and context.run.result is None
    return (
        (open_run or draining(context.run))
        and context.run.now_at < context.run.deadline_at
        and context.intents.recovery.phase == RecoveryPhase.READY
    )


def _deadline_retirements(state: SchedulingState, context: SchedulingContext) -> tuple[Signal, ...]:
    """Expired queued episodes retain ownership until Attempts proves retirement."""
    live_run = context.run.status in (RunStatus.RUNNING, RunStatus.PAUSED)
    if context.run.now_at < context.run.deadline_at or not (
        (live_run and context.run.result is None) or draining(context.run)
    ):
        return ()
    return tuple(
        RetireRequested(
            attempt=_target(request),
            disposition="cancel",
            authority=RequestId(root=f"deadline:{request.decision_id.root}"),
            admission_id=request.decision_id,
            requested_at=context.run.now_at,
        )
        for request in state.queue
        if _admission_proved(state, context, request, None)
    )


def _fits(state: SchedulingState, context: SchedulingContext, request: AdmissionRequest) -> bool:
    if any(slot.attempt.attempt_id == _target(request).attempt_id for slot in state.slots):
        return False
    if len(state.slots) >= context.run.limits.max_parallel:
        return False
    if any(set(request.pools).intersection(slot.pools) for slot in state.slots):
        return False
    if _mode(context, _target(request), request.decision_id) != WorkspaceMode.EXCLUSIVE_ROOT:
        return True
    return not any(
        _mode(context, slot.attempt, slot.admission_id) == WorkspaceMode.EXCLUSIVE_ROOT
        for slot in state.slots
    )


def _fill(
    state: SchedulingState,
    context: SchedulingContext,
    registering: AttemptRequest | None = None,
    *,
    retire_expired: bool = True,
) -> AreaChange[SchedulingState]:
    # A retirement answer (QueueEntryRetired) must not re-request retirement of
    # the other expired entries: their requests are already in flight in the same
    # propagation, and a repeated identical signal is a cycle.
    signals: list[Signal] = list(_deadline_retirements(state, context) if retire_expired else ())
    while state.queue and _accepting(context):
        head = state.queue[0]
        if not _admission_proved(state, context, head, registering) or not _fits(
            state, context, head
        ):
            break
        slot = Slot(
            attempt=_target(head),
            admission_id=head.decision_id,
            pools=head.pools,
            admitted_at=context.run.now_at,
        )
        state = state.model_copy(update={"queue": state.queue[1:], "slots": (*state.slots, slot)})
        signals.append(AdmitAttempt(request=head))
    if (
        state.admission_closed
        and not state.queue
        and not state.slots
        and context.run.status == RunStatus.CLOSING
        and _first_stop(context) is not None
    ):
        signals.append(RunDrained())
    return AreaChange(state=state, signals=tuple(signals))


def _duplicate(
    state: SchedulingState, context: SchedulingContext, request: AdmissionRequest
) -> AreaChange[SchedulingState] | None:
    for queued in state.queue:
        if queued.decision_id == request.decision_id:
            return (
                AreaChange(state=state)
                if queued == request
                else _reject(
                    state, request, RejectionCode.IDENTITY_CONFLICT, "queued payload changed"
                )
            )
        if _target(queued) == _target(request):
            return _reject(state, request, RejectionCode.OWNERSHIP, "attempt already queued")
    for slot in state.slots:
        if slot.attempt == _target(request):
            if slot.admission_id == request.decision_id and slot.pools == request.pools:
                return AreaChange(state=state)
            return _reject(
                state, request, RejectionCode.OWNERSHIP, "attempt already occupies capacity"
            )
    return _existing_duplicate(state, context, request)


def _existing_duplicate(
    state: SchedulingState, context: SchedulingContext, request: AdmissionRequest
) -> AreaChange[SchedulingState] | None:
    if isinstance(request, AttemptReopenRequest) and _reopen_replayed(context, request):
        return AreaChange(state=state)
    owner = _owner(context, _target(request))
    if (
        isinstance(request, AttemptRequest)
        and owner is not None
        and owner.phase != AttemptPhase.QUEUED
    ):
        return _reject(state, request, RejectionCode.OWNERSHIP, "attempt already registered")
    return None


def _registration(context: SchedulingContext, target: AttemptRef) -> StartAttempt | None:
    for receipt in context.run.receipts:
        proof = accepted_receipt_for(context.run.receipts, receipt.decision_id, None)
        decision = proof.value.decision if isinstance(proof, Proven) else None
        if (
            isinstance(decision, StartAttempt)
            and decision.scope.owner == context.run.run_id
            and decision.attempt_id == target.attempt_id
            and decision.scope.generation == target.generation
        ):
            return decision
    return None


def _validate_start(
    state: SchedulingState, context: SchedulingContext, request: AttemptRequest
) -> AreaChange[SchedulingState] | None:
    proof = accepted_receipt_for(context.run.receipts, request.decision_id, None)
    decision = proof.value.decision if isinstance(proof, Proven) else None
    if not isinstance(decision, StartAttempt) or decision.scope.owner != context.run.run_id:
        return _reject(
            state, request, RejectionCode.OWNERSHIP, "start requires canonical accepted decision"
        )
    if (request.attempt_id, request.item_id, request.generation, request.admission_charge) != (
        decision.attempt_id,
        decision.item_id,
        decision.scope.generation,
        decision.budget.admission_charge,
    ):
        return _reject(
            state, request, RejectionCode.IDENTITY_CONFLICT, "start differs from canonical decision"
        )
    owner = _owner(context, _target(request))
    original = _registration(context, _target(request))
    if owner is not None and original != decision:
        return _reject(
            state,
            request,
            RejectionCode.OWNERSHIP,
            "attempt has a different registration authority",
        )
    if owner is not None and (
        owner.item_id != request.item_id
        or not any(
            charge.kind == ChargeKind.ADMISSION
            and charge.charged == request.admission_charge
            and charge.historical_proof is None
            for charge in owner.charges
        )
    ):
        return _reject(
            state,
            request,
            RejectionCode.OWNERSHIP,
            "queued owner requires its live admission receipt",
        )
    return None


def _reopen_replayed(context: SchedulingContext, request: AttemptReopenRequest) -> bool:
    owner = _owner(context, request.attempt)
    if owner is not None and (
        owner.admission_id == request.decision_id
        or (owner.closure is not None and owner.closure.admission_id == request.decision_id)
    ):
        return True
    return any(
        receipt.decision_id == request.decision_id and receipt.completion is not None
        for receipt in context.run.receipts
    )


def _reopen_proved(context: SchedulingContext, request: AttemptReopenRequest) -> bool:
    owner = _owner(context, request.attempt)
    if (
        owner is None
        or owner.phase != AttemptPhase.PARKED
        or owner.closure is None
        or owner.release_dependencies
    ):
        return False
    if _reopen_replayed(context, request):
        return False
    proof = accepted_receipt_for(context.run.receipts, request.decision_id, None)
    decision = proof.value.decision if isinstance(proof, Proven) else None
    if not isinstance(decision, Operation):
        return False
    normalization = decision.normalized_scope_reopen
    return (
        _reopen_declared(context, decision)
        and decision.scope.owner == context.run.run_id
        and decision.scope.generation == context.run.generation
        and request.request_id == RequestId(root=f"operation:{decision.decision_id.root}")
        and normalization is not None
        and normalization == decision.registered_scope_reopen
        and normalization.attempt == request.attempt
        and normalization.park_authority == owner.closure.authority
        and owner.closure.disposition == "park"
    )


def _reopen_declared(context: SchedulingContext, decision: Operation) -> bool:
    # A codec proves the payload's schema, not that this run offers reopening.
    wire = decision.registered_wire
    if wire is None:
        return False
    return any(
        descriptor.normalization == OperationNormalizationKind.SCOPE_REOPEN
        and OperationSchemaRef(
            kind=descriptor.kind,
            request_schema=descriptor.request_schema,
            outcome_schema=descriptor.outcome_schema,
            lifecycle=descriptor.lifecycle,
        )
        == wire.schema_ref
        for descriptor in context.run.capabilities.operations
    )


def _admission_proved(
    state: SchedulingState,
    context: SchedulingContext,
    request: AdmissionRequest,
    registering: AttemptRequest | None,
) -> bool:
    if isinstance(request, AttemptReopenRequest):
        return _reopen_proved(context, request)
    owner = _owner(context, _target(request))
    if owner is None:
        # Only the same atomic transition that publishes RegisterAttempt may
        # admit an unregistered row. Persisted queue rows need receipt proof.
        return request == registering
    return (
        owner.phase == AttemptPhase.QUEUED
        and owner.closure is None
        and _validate_start(state, context, request) is None
    )


def _enqueue(
    state: SchedulingState, context: SchedulingContext, request: AdmissionRequest
) -> AreaChange[SchedulingState]:
    if isinstance(request, AttemptRequest):
        invalid = _validate_start(state, context, request)
        if invalid is not None:
            return invalid
    duplicate = _duplicate(state, context, request)
    if duplicate is not None:
        return duplicate
    if (
        state.admission_closed
        or context.run.status
        in (
            RunStatus.CLOSING,
            RunStatus.BLOCKED,
            RunStatus.TERMINAL,
        )
        or context.run.result is not None
        or context.run.now_at >= context.run.deadline_at
    ):
        return _reject(state, request, RejectionCode.CLOSED_SCOPE, "admission is closed")
    owner = _owner(context, _target(request))
    if (
        isinstance(request, AttemptRequest)
        and owner is None
        and request.admission_charge > _remaining(state, context)
    ):
        return _reject(state, request, RejectionCode.BUDGET, "admission budget exhausted")
    if isinstance(request, AttemptReopenRequest) and not _reopen_proved(context, request):
        return _reject(
            state,
            request,
            RejectionCode.OWNERSHIP,
            "reentry requires declared canonical operation and exact park authority",
        )
    if len(set(request.pools)) != len(request.pools):
        raise ContractValidationError("pools", "duplicate resource pool")
    queued = state.model_copy(update={"queue": (*state.queue, request)})
    registering = request if isinstance(request, AttemptRequest) and owner is None else None
    filled = _fill(queued, context, registering)
    registration = (
        (RegisterAttempt(request=request),)
        if isinstance(request, AttemptRequest) and owner is None
        else ()
    )
    return filled.model_copy(update={"signals": (*registration, *filled.signals)})


def _end_charge(state: SchedulingState, event: SlotChargeEnded) -> AreaChange[SchedulingState]:
    slots: list[Slot] = []
    for slot in state.slots:
        if (
            slot.attempt != event.attempt
            or slot.admission_id != event.admission_id
            or slot.charge_ended_at is not None
        ):
            slots.append(slot)
            continue
        if event.ended_at < slot.admitted_at:
            raise ContractValidationError("ended_at", "precedes occupancy admission")
        slots.append(slot.model_copy(update={"charge_ended_at": event.ended_at}))
    return AreaChange(state=state.model_copy(update={"slots": tuple(slots)}))


def _release(
    state: SchedulingState, context: SchedulingContext, event: SlotReleased
) -> AreaChange[SchedulingState]:
    slot = next(
        (
            slot
            for slot in state.slots
            if slot.attempt == event.attempt and slot.admission_id == event.admission_id
        ),
        None,
    )
    if slot is None:
        return AreaChange(state=state)
    end = slot.charge_ended_at if slot.charge_ended_at is not None else context.run.now_at
    released = state.model_copy(
        update={
            "slots": tuple(held for held in state.slots if held != slot),
            "released_slot_seconds": state.released_slot_seconds + max(0.0, end - slot.admitted_at),
        }
    )
    return _fill(released, context)


def _cancel(state: SchedulingState, context: SchedulingContext) -> tuple[Signal, ...] | None:
    if context.run.status != RunStatus.CLOSING:
        return None
    decision = _first_stop(context)
    # Reordered controls cannot change the first committed stop disposition.
    if decision is None or decision.mode != "cancel":
        return None
    # A parked closure fences its old episode, not a queued reopening. Cancel
    # the carried new admission identity unless that exact episode is closing.
    episodes = tuple((slot.attempt, slot.admission_id) for slot in state.slots)
    episodes += tuple((_target(request), request.decision_id) for request in state.queue)
    return tuple(
        RetireRequested(
            attempt=attempt,
            disposition="cancel",
            authority=RequestId(root=f"stop:{decision.decision_id.root}:{admission_id.root}"),
            admission_id=admission_id,
            requested_at=context.run.now_at,
        )
        for attempt, admission_id in episodes
        if (owner := _owner(context, attempt)) is not None
        and (owner.closure is None or owner.closure.admission_id != admission_id)
    )


def _stop_replay(
    state: SchedulingState, context: SchedulingContext
) -> AreaChange[SchedulingState] | None:
    stops = _committed_stops(context)
    if len(stops) <= 1:
        return None
    # The kernel stages a proposed result before dispatch; rejecting here rolls
    # the whole proposal back, so the first committed stop retains authority.
    return AreaChange(
        state=state,
        events=(
            Rejected(
                decision_id=stops[-1].decision_id,
                code=RejectionCode.CLOSED_SCOPE,
                path=("run", "result"),
                detail="first committed stop retains its result and disposition",
            ),
        ),
    )


def _control(
    state: SchedulingState, context: SchedulingContext, event: AdmissionControl
) -> AreaChange[SchedulingState]:
    if event.action == "pause" and context.run.status != RunStatus.PAUSED:
        raise ContractValidationError(
            "admission_control.run.status", "pause requires the run's committed PAUSED status"
        )
    if event.action == "resume":
        if context.run.status != RunStatus.RUNNING or context.run.result is not None:
            return AreaChange(state=state)
        return _fill(state.model_copy(update={"admission_closed": False}), context)
    refusal = _stop_replay(state, context)
    if refusal is not None:
        return refusal
    cancellations = _cancel(state, context) if event.action == "cancel" else ()
    if cancellations is None:
        return AreaChange(state=state)
    closed = state.model_copy(update={"admission_closed": True})
    if event.action == "pause":
        return AreaChange(state=closed)
    drained = _fill(closed, context)
    return drained.model_copy(update={"signals": (*cancellations, *drained.signals)})


def _retired_entry(request: AdmissionRequest, event: QueueEntryRetired) -> bool:
    # Attempts B names the exact queued decision, so a delayed old retirement
    # cannot remove a later start or reopen of the same attempt.
    return _target(request) == event.attempt and request.decision_id == event.admission_id


def _ready(
    state: SchedulingState, context: SchedulingContext, event: AttemptReady
) -> AreaChange[SchedulingState]:
    owner = _owner(context, event.attempt)
    valid = (
        owner is not None
        and owner.phase == AttemptPhase.ACTIVE
        and owner.admission_id == event.admission_id
        and owner.closure is None
        and any(
            slot.attempt == event.attempt
            and slot.admission_id == event.admission_id
            and slot.charge_ended_at is None
            for slot in state.slots
        )
    )
    # Readiness is nonterminal feedback. Frozen state has no delivered marker;
    # duplicate positive facts can repeat it, but never grant new capacity.
    return AreaChange(state=state, events=(event,) if valid else ())


def schedule(
    state: SchedulingState, context: SchedulingContext, event: SchedulingEvent
) -> AreaChange[SchedulingState]:
    """Consume scheduling facts, preserving exact occupancy episode authority.

    Admission consumes ADMISSION receipts, never a second spent/refunded ledger.
    Cleanup retains capacity after charge-end until matching release proof.
    """
    match event:
        case AttemptRequested() | AttemptReopenRequested():
            return _enqueue(state, context, event.request)
        case SlotChargeEnded():
            return _end_charge(state, event)
        case SlotReleased():
            return _release(state, context, event)
        case AttemptReady():
            change = _ready(state, context, event)
        case QueueEntryRetired():
            queued = state.model_copy(
                update={
                    "queue": tuple(
                        request for request in state.queue if not _retired_entry(request, event)
                    )
                }
            )
            return _fill(queued, context, retire_expired=False)
        case ClockAdvanced():
            if event.now_at < context.run.now_at:
                raise ContractValidationError("now_at", "clock moved backwards")
            filled = _fill(state, context)
            # Core time is the only trigger of paced job polls, so every tick asks.
            change = filled.model_copy(
                update={"signals": (*filled.signals, ObservationsDue(now_at=context.run.now_at))}
            )
        case AdmissionControl():
            return _control(state, context, event)
        case _:
            assert_never(event)
    return change
