"""Durable occurrence reservation, delivery, disposal and interruption accounting.

InputRecord is the sole input authority. This leaf changes only inputs and
interrupts, emitting canonical facts for the turn and attempt owners.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import (
    Proven,
    accepted_receipt_for,
    current_admission,
    current_closure,
    fresh_observation,
    observation_for,
    request_matches,
)
from ._session_checkpoints import checkpoint_matches, turn_source_matches
from ._session_scope import attempt_for, proven_invocation, scope_active
from .types.attempts import (
    AttemptChargeRefundRequested,
    InvocationCheckpointRequested,
)
from .types.common import (
    AttemptId,
    AttemptRef,
    ChargeKind,
    CompletionStatus,
    ContractValidationError,
    ObservationStatus,
    RequestId,
    RunStatus,
    Scope,
)
from .types.intents import ExecuteRegisteredOperation
from .types.kernel import AreaChange, DecisionCompleted
from .types.session_inputs import (
    InputDelivered,
    InputDropped,
    InputDropReason,
    InputRecord,
    InvocationInputTarget,
    ItemInputTarget,
    ScopeInputTarget,
)
from .types.sessions import (
    DispatchTurn,
    InputAcceptanceObserved,
    InputReservationReleased,
    InputReservationRequested,
    InspectTurn,
    InterruptClaim,
    InterruptCompleted,
    InterruptRequested,
    InvocationCancellationRequested,
    InvocationChargeRefunded,
    InvocationCheckpointAvailable,
    ResumeSessionTurn,
    RunInvocationCheckpointRequested,
    SessionDrainRequested,
    SessionInputReceived,
    SessionPhase,
    SteerReceived,
    TurnInputsReserved,
)
from .types.strategy import Accepted, Interrupt, Operation, Withdraw

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.common import InvocationRef, RevisionRef
    from .types.intents import Request
    from .types.kernel import SessionsContext, Signal
    from .types.session_inputs import SessionInput
    from .types.sessions import Invocation, SessionsEvent, SessionsState


def _matches(item: SessionInput, invocation: Invocation, context: SessionsContext) -> bool:
    target = item.target
    if isinstance(target, InvocationInputTarget):
        return target.invocation == invocation.invocation
    if isinstance(target, ScopeInputTarget):
        return target.scope == invocation.scope
    owner = attempt_for(context, invocation.scope)
    return owner is not None and owner.item_id == target.item_id


def _drop(
    record: InputRecord, reason: InputDropReason, at: float
) -> tuple[InputRecord, InputDropped]:
    receipt = InputDropped(
        input_id=record.input.input_id, target=record.input.target, reason=reason, at=at
    )
    return record.model_copy(update={"receipt": receipt}), receipt


def _received(
    state: SessionsState, context: SessionsContext, item: SessionInput
) -> AreaChange[SessionsState]:
    existing = next((row for row in state.inputs if row.input.input_id == item.input_id), None)
    if existing is not None:
        if existing.input != item:
            raise ContractValidationError("input.input_id", "occurrence payload conflict")
        return AreaChange(state=state)
    record = InputRecord(input=item)
    events = ()
    if context.run.status == RunStatus.TERMINAL:
        record, receipt = _drop(record, InputDropReason.RUN_TERMINAL, context.run.now_at)
        events = (receipt,)
    return AreaChange(
        state=state.model_copy(update={"inputs": (*state.inputs, record)}),
        events=events,
    )


def _reserve(
    state: SessionsState, context: SessionsContext, event: InputReservationRequested
) -> AreaChange[SessionsState]:
    invocation = proven_invocation(state, event.invocation)
    session = next(
        (row for row in state.sessions if row.spec.session_id == event.invocation.session_id), None
    )
    if (
        invocation is None
        or invocation.phase != SessionPhase.ACQUIRING
        or not scope_active(context, invocation.scope)
        or session is None
        or session.invocation != event.invocation.invocation_id
    ):
        return AreaChange(state=state)
    records = tuple(
        row.model_copy(update={"reserved_to": event.invocation})
        if row.receipt is None
        and row.reserved_to is None
        and _matches(row.input, invocation, context)
        else row
        for row in state.inputs
    )
    manifest = tuple(
        row.input.input_id
        for row in sorted(
            (row for row in records if row.receipt is None and row.reserved_to == event.invocation),
            key=lambda row: (row.input.sequence, row.input.input_id.root),
        )
    )
    return AreaChange(
        state=state.model_copy(update={"inputs": records}),
        signals=(TurnInputsReserved(invocation=event.invocation, input_ids=manifest),),
    )


def _inspection(
    state: SessionsState, context: SessionsContext, invocation: Invocation
) -> AreaChange[SessionsState]:
    ref = invocation.invocation
    identity = RequestId(
        root=f"input-inspect:{len(ref.session_id.root)}:{ref.session_id.root}:"
        f"{ref.generation}:{len(ref.invocation_id.root)}:{ref.invocation_id.root}"
    )
    previous = next((row for row in context.intents.intents if row.request_id == identity), None)
    owner = attempt_for(context, invocation.scope)
    request = (
        previous.request
        if previous is not None
        else InspectTurn(
            request_id=identity,
            scope=invocation.scope,
            invocation=ref,
            admission_id=owner.admission_id if owner is not None else None,
            deadline_at=min(
                context.run.deadline_at,
                context.run.now_at + context.run.limits.reconciliation_bound,
            ),
        )
    )
    return AreaChange(state=state, requests=(request,))


def _input_proof(
    state: SessionsState,
    context: SessionsContext,
    event: InputAcceptanceObserved | InputReservationReleased,
) -> Invocation | None:
    invocation = proven_invocation(state, event.invocation)
    obs = event.observation
    intent = next(
        (row for row in context.intents.intents if row.request_id == obs.request_id), None
    )
    if (
        invocation is None
        or invocation.observation is None
        or intent is None
        or invocation.scope != obs.scope
        or intent.request.scope != invocation.scope
        or invocation.observation.request_id != obs.request_id
        or invocation.observation.sequence != obs.sequence
        or invocation.observation.accepted != obs.accepted
        or invocation.observation.terminal != obs.terminal
        or invocation.observation.status != obs.status
        or not isinstance(request_matches(intent, intent.request), Proven)
        or not isinstance(observation_for(intent, obs), Proven)
        or not isinstance(
            fresh_observation(
                () if intent.observation is None else (intent.observation,), obs, complete=True
            ),
            Proven,
        )
    ):
        return None
    owner = attempt_for(context, invocation.scope)
    if isinstance(invocation.scope.owner, AttemptId) and (
        not isinstance(current_admission(owner, invocation.scope, obs.admission_id), Proven)
    ):
        return None
    return invocation if _manifest_matches(state, context, invocation, intent.request) else None


def _manifest_matches(
    state: SessionsState, context: SessionsContext, invocation: Invocation, request: Request
) -> bool:
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        if request.turn != invocation.turn:
            return False
        inputs = request.inputs
    elif isinstance(request, ExecuteRegisteredOperation):
        receipt = next(
            (row for row in context.run.receipts if row.decision_id == request.decision_id), None
        )
        if (
            request.operation_id != invocation.registered_operation
            or receipt is None
            or not isinstance(
                accepted_receipt_for(context.run.receipts, request.decision_id, None), Proven
            )
            or not isinstance(receipt.decision, Operation)
            or receipt.decision.normalized_turn != invocation.turn
            or receipt.decision.registered_wire != request.operation
        ):
            return False
        inputs = request.inputs
    else:
        return False
    if tuple(item.input_id for item in inputs) != invocation.input_ids:
        return False
    records = {
        row.input.input_id: row for row in state.inputs if row.reserved_to == invocation.invocation
    }
    return set(records) == set(invocation.input_ids) and all(
        item.input_id in records
        and records[item.input_id].input.artifact == item.artifact
        and (
            records[item.input_id].input == item
            if isinstance(request, DispatchTurn | ResumeSessionTurn)
            else _matches(records[item.input_id].input, invocation, context)
        )
        for item in inputs
    )


def _release_record(
    record: InputRecord, context: SessionsContext, invocation: Invocation, at: float
) -> tuple[InputRecord, InputDropped | None]:
    if isinstance(record.input.target, InvocationInputTarget):
        return _drop(record, InputDropReason.INVOCATION_TERMINAL, at)
    owner = attempt_for(context, invocation.scope)
    closure = current_closure(owner, owner.closure if owner is not None else None)
    if isinstance(closure, Proven) and closure.value.disposition != "park":
        reason = (
            InputDropReason.OWNER_CANCELLED
            if closure.value.disposition == "cancel"
            else InputDropReason.OWNER_TERMINAL
        )
        return _drop(record, reason, at)
    return record.model_copy(update={"reserved_to": None}), None


def _acceptance(
    state: SessionsState,
    context: SessionsContext,
    event: InputAcceptanceObserved | InputReservationReleased,
) -> AreaChange[SessionsState]:
    invocation = _input_proof(state, context, event)
    if invocation is None:
        return AreaChange(state=state)
    obs = event.observation
    if obs.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING) or (
        obs.status == ObservationStatus.SUCCEEDED and not obs.accepted
    ):
        return _inspection(state, context, invocation)
    delivery = isinstance(event, InputAcceptanceObserved) and obs.accepted
    release = (
        isinstance(event, InputReservationReleased)
        and not obs.accepted
        and obs.terminal
        and obs.status
        in (ObservationStatus.REJECTED, ObservationStatus.FAILED, ObservationStatus.CANCELLED)
    )
    if not delivery and not release:
        return AreaChange(state=state)
    records = []
    events = []
    for original in state.inputs:
        record = original
        if (
            record.reserved_to == event.invocation
            and record.receipt is None
            and record.input.input_id in invocation.input_ids
        ):
            if delivery:
                receipt = InputDelivered(
                    input_id=record.input.input_id, invocation=event.invocation, observation=obs
                )
                record = record.model_copy(update={"receipt": receipt})
                events.append(receipt)
            else:
                record, receipt = _release_record(record, context, invocation, obs.observed_at)
                if receipt is not None:
                    events.append(receipt)
        records.append(record)
    return AreaChange(
        state=state.model_copy(update={"inputs": tuple(records)}), events=tuple(events)
    )


def _retained_wip(
    state: SessionsState, owner: AttemptView | None, ref: InvocationRef
) -> InvocationCheckpointAvailable | None:
    """The committed wip checkpoint of an invocation, as the event that announced it."""
    proofs = state.run_checkpoints if owner is None else owner.checkpoints
    rows = tuple(row for row in proofs if row.invocation == ref and row.retention == "wip")
    if len(rows) != 1:
        return None
    return InvocationCheckpointAvailable(
        invocation=ref, request_id=rows[0].request_id, revision=rows[0].revision, retention="wip"
    )


def _interrupt(
    state: SessionsState, context: SessionsContext, event: InterruptRequested
) -> AreaChange[SessionsState]:
    invocation = proven_invocation(state, event.invocation)
    if invocation is None or not scope_active(context, invocation.scope):
        return AreaChange(state=state)
    authority = next(
        (
            row
            for row in context.run.receipts
            if isinstance(row.feedback, Accepted)
            and isinstance(row.decision, Withdraw)
            and row.decision.target == event.invocation
            and row.decision.scope == invocation.scope
            and isinstance(row.decision.disposition, Interrupt)
            and row.decision.disposition.refund == event.refund
            and event.authority == RequestId(root=f"withdraw:{row.decision_id.root}")
        ),
        None,
    )
    if authority is None or not isinstance(
        accepted_receipt_for(context.run.receipts, authority.decision_id, authority.decision),
        Proven,
    ):
        return AreaChange(state=state)
    previous = next((row for row in state.interrupts if row.invocation == event.invocation), None)
    if previous is not None:
        if previous.authority != event.authority or previous.refund != event.refund:
            raise ContractValidationError("authority", "interruption identity payload conflict")
        return AreaChange(state=state)
    if (
        invocation.observation is not None
        and invocation.observation.status == ObservationStatus.SUCCEEDED
        and not invocation.observation.accepted
    ):
        return _inspection(state, context, invocation)
    owner = attempt_for(context, invocation.scope)
    charges = (
        ()
        if owner is None
        else tuple(
            row
            for row in owner.charges
            if row.kind == ChargeKind.ATTEMPT
            and row.invocation_id == event.invocation.invocation_id
            and row.historical_proof is None
        )
    )
    if event.refund and (
        len(charges) != 1 or event.refund > charges[0].charged - charges[0].refunded
    ):
        raise ContractValidationError("refund", "requires bounded live ATTEMPT charge")
    claim = InterruptClaim(
        invocation=event.invocation,
        authority=event.authority,
        refund=event.refund,
        phase="draining",
    )
    state = state.model_copy(update={"interrupts": (*state.interrupts, claim)})
    if (
        invocation.observation is None
        or not invocation.observation.terminal
        or invocation.observation.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    ):
        return AreaChange(
            state=state,
            signals=(
                InvocationCancellationRequested(
                    invocation=event.invocation, authority=event.authority
                ),
            ),
        )
    return _drain_to_checkpoint(state, context, invocation, owner, event)


def _drain_to_checkpoint(
    state: SessionsState,
    context: SessionsContext,
    invocation: Invocation,
    owner: AttemptView | None,
    event: InterruptRequested,
) -> AreaChange[SessionsState]:
    """Complete from the committed wip checkpoint, or request one to be retained."""
    retained = _retained_wip(state, owner, event.invocation)
    if retained is not None and checkpoint_matches(state, context, retained):
        return _checkpoint(state, context, retained)
    signal = (
        InvocationCheckpointRequested(
            attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
            invocation=event.invocation,
            retention="wip",
            authority=event.authority,
        )
        if owner is not None
        else RunInvocationCheckpointRequested(
            invocation=event.invocation,
            scope=invocation.scope,
            retention="wip",
            authority=event.authority,
        )
    )
    return AreaChange(state=state, signals=(signal,))


def _interrupt_completion(
    context: SessionsContext, claim: InterruptClaim, checkpoint: RevisionRef
) -> tuple[tuple[Signal, ...], InterruptCompleted]:
    receipt = next(
        (
            row
            for row in context.run.receipts
            if isinstance(row.decision, Withdraw)
            and row.decision.target == claim.invocation
            and isinstance(row.decision.disposition, Interrupt)
            and row.decision.disposition.refund == claim.refund
            and claim.authority == RequestId(root=f"withdraw:{row.decision_id.root}")
        ),
        None,
    )
    proof = accepted_receipt_for(
        context.run.receipts, receipt.decision_id if receipt is not None else None, None
    )
    signals: tuple[Signal, ...] = ()
    if isinstance(proof, Proven) and proof.value.completion is None:
        signals = (
            DecisionCompleted(
                decision_id=proof.value.decision_id, status=CompletionStatus.SUCCEEDED
            ),
        )
    return signals, InterruptCompleted(
        invocation=claim.invocation, checkpoint=checkpoint, refund=claim.refund
    )


def _replace_claim(state: SessionsState, claim: InterruptClaim) -> SessionsState:
    return state.model_copy(
        update={
            "interrupts": tuple(
                claim if row.invocation == claim.invocation else row for row in state.interrupts
            )
        }
    )


def _in_flight(state: SessionsState, scope: Scope | None) -> int:
    """Refund amount already requested by other claims but not yet applied."""
    scopes = {row.invocation: row.scope for row in state.invocations}
    return sum(
        row.refund
        for row in state.interrupts
        if row.phase == "checkpointed" and (scope is None or scopes.get(row.invocation) == scope)
    )


def _checkpoint(
    state: SessionsState, context: SessionsContext, event: InvocationCheckpointAvailable
) -> AreaChange[SessionsState]:
    claim = next((row for row in state.interrupts if row.invocation == event.invocation), None)
    invocation = proven_invocation(state, event.invocation)
    if (
        claim is None
        or claim.phase in ("checkpointed", "completed", "blocked")
        or invocation is None
        or event.retention != "wip"
    ):
        return AreaChange(state=state)
    if not checkpoint_matches(state, context, event):
        return AreaChange(state=state)
    owner = attempt_for(context, invocation.scope)
    claim = claim.model_copy(
        update={"phase": "checkpointed", "checkpoint_authority": event.request_id}
    )
    if claim.refund == 0:
        claim = claim.model_copy(update={"phase": "completed"})
        signals, completed = _interrupt_completion(context, claim, event.revision)
        return AreaChange(state=_replace_claim(state, claim), signals=signals, events=(completed,))
    charges = (
        ()
        if owner is None
        else tuple(
            row
            for row in owner.charges
            if row.kind == ChargeKind.ATTEMPT
            and row.invocation_id == event.invocation.invocation_id
            and row.historical_proof is None
        )
    )
    if (
        len(charges) != 1
        or owner is None
        or claim.refund > charges[0].charged - charges[0].refunded
        or sum(row.refunded for row in owner.charges)
        + _in_flight(state, invocation.scope)
        + claim.refund
        > owner.budget.refund_limit
        or sum(
            row.refunded
            for row in (
                *state.run_charges,
                *tuple(
                    charge for attempt in context.attempts.attempts for charge in attempt.charges
                ),
            )
        )
        + _in_flight(state, None)
        + claim.refund
        > context.run.limits.max_refunds
    ):
        # The bounds can shrink after admission; a claim that can no longer refund
        # is terminal, so replay and run teardown never wait on it.
        return AreaChange(
            state=_replace_claim(state, claim.model_copy(update={"phase": "blocked"}))
        )
    return AreaChange(
        state=_replace_claim(state, claim),
        signals=(
            AttemptChargeRefundRequested(
                attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
                charge_id=charges[0].charge_id,
                amount=claim.refund,
                reason="interrupted",
                authority=claim.authority,
                checkpoint_authority=event.request_id,
            ),
        ),
    )


def _refunded(
    state: SessionsState, context: SessionsContext, event: InvocationChargeRefunded
) -> AreaChange[SessionsState]:
    claim = next((row for row in state.interrupts if row.invocation == event.invocation), None)
    invocation = proven_invocation(state, event.invocation)
    if (
        claim is None
        or claim.phase != "checkpointed"
        or invocation is None
        or claim.authority != event.authority
        or claim.checkpoint_authority is None
    ):
        return AreaChange(state=state)
    owner = attempt_for(context, invocation.scope)
    if (
        owner is None
        or not isinstance(current_admission(owner, invocation.scope, owner.admission_id), Proven)
        or invocation.observation is None
        or not invocation.observation.terminal
        or invocation.observation.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
        or not turn_source_matches(context, invocation, invocation.observation)
    ):
        return AreaChange(state=state)
    charges = tuple(
        row
        for row in owner.charges
        if row.charge_id == event.charge_id
        and row.kind == ChargeKind.ATTEMPT
        and row.invocation_id == event.invocation.invocation_id
        and row.historical_proof is None
        and event.authority in row.refund_sources
        and row.refunded >= claim.refund
    )
    checkpoints = tuple(
        row
        for row in owner.checkpoints
        if row.request_id == claim.checkpoint_authority
        and row.invocation == event.invocation
        and row.retention == "wip"
    )
    if (
        len(charges) != 1
        or len(checkpoints) != 1
        or not checkpoint_matches(
            state,
            context,
            InvocationCheckpointAvailable(
                invocation=event.invocation,
                request_id=checkpoints[0].request_id,
                revision=checkpoints[0].revision,
                retention="wip",
            ),
        )
    ):
        return AreaChange(state=state)
    claim = claim.model_copy(update={"phase": "completed", "refunded_charge": event.charge_id})
    signals, completed = _interrupt_completion(context, claim, checkpoints[0].revision)
    return AreaChange(state=_replace_claim(state, claim), signals=signals, events=(completed,))


def _drain(
    state: SessionsState,
    context: SessionsContext,
    event: SessionDrainRequested,
) -> AreaChange[SessionsState]:
    scope = Scope(owner=event.attempt.attempt_id, generation=event.attempt.generation)
    owner = attempt_for(context, scope)
    closure = current_closure(owner, owner.closure if owner is not None else None)
    if (
        owner is None
        or not isinstance(closure, Proven)
        or closure.value.authority != event.authority
        or closure.value.disposition != event.disposition
        or event.disposition == "park"
    ):
        return AreaChange(state=state)
    records = []
    events = []
    for original in state.inputs:
        record = original
        target = record.input.target
        eligible = (
            (isinstance(target, ScopeInputTarget) and target.scope == scope)
            or (isinstance(target, ItemInputTarget) and target.item_id == owner.item_id)
            or (
                isinstance(target, InvocationInputTarget)
                and any(
                    row.invocation == target.invocation and row.scope == scope
                    for row in state.invocations
                )
            )
        )
        if eligible and record.receipt is None and record.reserved_to is None:
            record, receipt = _drop(
                record,
                InputDropReason.OWNER_CANCELLED
                if event.disposition == "cancel"
                else InputDropReason.OWNER_TERMINAL,
                context.run.now_at,
            )
            events.append(receipt)
        records.append(record)
    return AreaChange(
        state=state.model_copy(update={"inputs": tuple(records)}), events=tuple(events)
    )


def advance(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Consume input-owned ingress and committed sibling facts, preserving siblings."""
    match event:
        case SessionInputReceived():
            change = _received(state, context, event.input)
        case SteerReceived():
            events = []
            for item in event.inputs:
                received = _received(state, context, item)
                state = received.state
                events.extend(received.events)
            change = AreaChange(state=state, events=tuple(events))
        case InputReservationRequested():
            change = _reserve(state, context, event)
        case InputAcceptanceObserved() | InputReservationReleased():
            change = _acceptance(state, context, event)
        case InterruptRequested():
            change = _interrupt(state, context, event)
        case InvocationCheckpointAvailable():
            change = _checkpoint(state, context, event)
        case InvocationChargeRefunded():
            change = _refunded(state, context, event)
        case SessionDrainRequested():
            change = _drain(state, context, event)
        case _:
            raise ContractValidationError("event.kind", "event is owned by session turns")
    return change


def finish_run(state: SessionsState, context: SessionsContext) -> AreaChange[SessionsState]:
    """Finalize remaining occurrences once after the kernel proves ownership drained."""
    if context.run.status != RunStatus.TERMINAL:
        raise ContractValidationError("run.status", "input finalization requires terminal context")
    records = []
    events = []
    for original in state.inputs:
        record = original
        if record.receipt is None:
            record, receipt = _drop(record, InputDropReason.RUN_TERMINAL, context.run.now_at)
            events.append(receipt)
        records.append(record)
    return AreaChange(
        state=state.model_copy(update={"inputs": tuple(records)}), events=tuple(events)
    )
