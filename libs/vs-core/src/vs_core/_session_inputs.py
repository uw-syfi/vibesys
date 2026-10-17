"""Durable occurrence reservation, delivery, disposal and interruption accounting.

InputRecord is the sole input authority. This leaf changes only inputs and
interrupts, emitting canonical facts for the turn and attempt owners.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
from ._session_rows import awaiting_checkpoint as _awaiting_checkpoint
from ._session_rows import replace_claim as _replace_claim
from ._session_scope import attempt_for, dispatching_request, proven_invocation, scope_active
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
    InputId,
    InvocationInputTarget,
    ItemInputTarget,
    ObservationStatus,
    RequestId,
    RoleInputTarget,
    RunStatus,
    Scope,
    ScopeInputTarget,
)
from .types.kernel import AreaChange, DecisionCompleted
from .types.records import ExecuteRegisteredOperation, within
from .types.session_inputs import (
    InputDelivered,
    InputDropped,
    InputDropReason,
    InputMode,
    InputRecord,
    MidTurnOffer,
)
from .types.sessions import (
    DispatchTurn,
    InputAcceptanceObserved,
    InputMidTurnReleased,
    InputOfferedMidTurn,
    InputReservationReleased,
    InputReservationRequested,
    InputTakenMidTurn,
    InputWithdrawRequested,
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
    from collections.abc import Callable

    from .types.attempts import AttemptView
    from .types.common import InputTarget, InvocationRef, RevisionRef
    from .types.kernel import SessionsContext, Signal
    from .types.records import Request
    from .types.session_inputs import SessionInput
    from .types.sessions import Invocation, SessionsEvent, SessionsState
    from .types.strategy import SearchDeclaration


def _run_scope(context: SessionsContext) -> Scope:
    return Scope(owner=context.run.run_id, generation=context.run.generation)


def _addressed_to_run(item: SessionInput, context: SessionsContext) -> bool:
    target = item.target
    return isinstance(target, ScopeInputTarget) and target.scope == _run_scope(context)


def _matches(item: SessionInput, invocation: Invocation, context: SessionsContext) -> bool:
    target = item.target
    if isinstance(target, InvocationInputTarget):
        return target.invocation == invocation.invocation
    if isinstance(target, RoleInputTarget):
        # Like run-wide input, a role's input is for its next conversational turn.
        return (
            invocation.turn.session.role_id == target.role_id
            and invocation.registered_operation is None
        )
    if isinstance(target, ScopeInputTarget):
        # The run is the ancestor of every attempt scope, so an input addressed to the run
        # reaches whichever of its invocations reserves first.
        if target.scope == invocation.scope:
            return True
        # A registered operation is a mechanical turn the strategy decided on, not the next
        # conversational turn the operator addressed, so run-wide input waits for a session turn.
        return target.scope == _run_scope(context) and invocation.registered_operation is None
    owner = attempt_for(context, invocation.scope)
    return owner is not None and owner.item_id == target.item_id


def standing_guidance(
    state: SessionsState, context: SessionsContext, invocation: Invocation
) -> tuple[SessionInput, ...]:
    """The standing inputs in force for ``invocation``'s turn, in the order they were received.

    Standing input is never reserved: every matching session turn receives it while it is
    in force. A registered operation is a mechanical turn the strategy decided on, so it
    takes none.
    """
    if invocation.registered_operation is not None:
        return ()
    return tuple(
        row.input
        for row in sorted(
            state.inputs, key=lambda row: (row.input.sequence, row.input.input_id.root)
        )
        if row.input.mode is InputMode.STANDING
        and row.receipt is None
        and _matches(row.input, invocation, context)
    )


def _withdraw(
    state: SessionsState, context: SessionsContext, event: InputWithdrawRequested
) -> AreaChange[SessionsState]:
    """End a standing input with a WITHDRAWN receipt; one that already ended stays as it is."""
    record = next((row for row in state.inputs if row.input.input_id == event.input_id), None)
    if record is None or record.input.mode is not InputMode.STANDING:
        raise ContractValidationError("withdraws", "names no standing input of this run")
    if record.receipt is not None:
        return AreaChange(state=state)
    withdrawn, receipt = _drop(record, InputDropReason.WITHDRAWN, context.run.now_at)
    return AreaChange(
        state=state.model_copy(
            update={
                "inputs": tuple(
                    withdrawn if row.input.input_id == event.input_id else row
                    for row in state.inputs
                )
            }
        ),
        events=(receipt,),
    )


def check_declared_target(target: InputTarget | None, declaration: SearchDeclaration) -> None:
    """Reject an input for a role the run's search does not declare: no turn could take it."""
    if isinstance(target, RoleInputTarget) and target.role_id not in declaration.roles:
        declared = ", ".join(role.root for role in declaration.roles) or "none"
        raise ContractValidationError(
            "target.role_id",
            f"role {target.role_id.root!r} is not declared by the search (declared: {declared})",
        )


def may_have_been_read(record: InputRecord) -> bool:
    """Whether a provider took ``record``'s input mid-turn and no turn refused it since.

    So when the turn holding the offer took it, or an earlier turn took it and ended without
    delivering or refusing it.
    """
    return record.taken_by is not None or record.possibly_read


def _drop(
    record: InputRecord, reason: InputDropReason, at: float
) -> tuple[InputRecord, InputDropped]:
    """Settle ``record`` with no turn; one a provider took mid-turn may have been read."""
    if may_have_been_read(record):
        reason = InputDropReason.DELIVERY_UNKNOWN
    receipt = InputDropped(
        input_id=record.input.input_id, target=record.input.target, reason=reason, at=at
    )
    return record.model_copy(update={"receipt": receipt}), receipt


def _received(
    state: SessionsState, context: SessionsContext, item: SessionInput
) -> AreaChange[SessionsState]:
    check_declared_target(item.target, context.run.declaration)
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
        and row.offered_to is None
        and row.input.mode is not InputMode.STANDING
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
    previous = context.records.record(identity)
    owner = attempt_for(context, invocation.scope)
    request = (
        previous.request
        if previous is not None
        else InspectTurn(
            request_id=identity,
            scope=invocation.scope,
            invocation=ref,
            dispatch=dispatching_request(context.records, invocation),
            admission_id=owner.admission_id if owner is not None else None,
        )
    )
    return AreaChange(
        # The inspection also settles the acceptance a pending yield's checkpoint needs.
        state=_awaiting_checkpoint(state, context, ref, request),
        requests=(within(request, context.run.limits.reconciliation_bound),),
    )


def _input_proof(
    state: SessionsState,
    context: SessionsContext,
    event: InputAcceptanceObserved | InputReservationReleased,
) -> Invocation | None:
    invocation = proven_invocation(state, event.invocation)
    obs = event.observation
    record = context.records.record(obs.request_id)
    if (
        invocation is None
        or invocation.observation is None
        or record is None
        or invocation.scope != obs.scope
        or record.request.scope != invocation.scope
        or invocation.observation.request_id != obs.request_id
        or invocation.observation.sequence != obs.sequence
        or invocation.observation.accepted != obs.accepted
        or invocation.observation.terminal != obs.terminal
        or invocation.observation.status != obs.status
        or not isinstance(request_matches(record, record.request), Proven)
        or not isinstance(observation_for(record, obs), Proven)
        or not isinstance(
            fresh_observation(
                () if record.observation is None else (record.observation,), obs, complete=True
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
    return invocation if _manifest_matches(state, context, invocation, record.request) else None


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
    if _addressed_to_run(record.input, context) or isinstance(record.input.target, RoleInputTarget):
        # The run and a role outlive the attempt whose turn reserved the input, and a later
        # invocation (of that role) can take it, so a cancelled or ended reserver never
        # consumes it: it waits again.
        return _pending(record), None
    owner = attempt_for(context, invocation.scope)
    closure = current_closure(owner, owner.closure if owner is not None else None)
    if isinstance(closure, Proven) and closure.value.disposition != "park":
        reason = (
            InputDropReason.OWNER_CANCELLED
            if closure.value.disposition == "cancel"
            else InputDropReason.OWNER_TERMINAL
        )
        return _drop(record, reason, at)
    return _pending(record), None


def _pending(record: InputRecord) -> InputRecord:
    """The record waiting again, held by no turn.

    The offer ends, so does its take; a take that no refusal answered is kept as
    ``possibly_read``.
    """
    return record.model_copy(
        update={
            "reserved_to": None,
            "offered_to": None,
            "taken_by": None,
            "possibly_read": may_have_been_read(record),
        }
    )


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
        offered = record.offered_to == event.invocation
        if record.receipt is None and (
            offered
            or (
                record.reserved_to == event.invocation
                and record.input.input_id in invocation.input_ids
            )
        ):
            if delivery and offered and record.taken_by != event.invocation:
                # Offered, but the provider never took it (or its answer was never committed):
                # the turn ended without it, so the next turn gets it.
                record, receipt = _release_record(record, context, invocation, obs.observed_at)
                if receipt is not None:
                    events.append(receipt)
            elif delivery:
                # An offered input joined the turn only if the provider took it and the turn's
                # result did not refuse it, which releases it before this acceptance.
                receipt = InputDelivered(
                    input_id=record.input.input_id,
                    invocation=event.invocation,
                    observation=obs,
                    mid_turn=offered,
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
        wait=None,
    )
    state = _replace_claim(state, claim)
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
        if (
            eligible
            and record.receipt is None
            and record.reserved_to is None
            and record.offered_to is None
        ):
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


def _takes_mid_turn(context: SessionsContext, invocation: Invocation, item: SessionInput) -> bool:
    """Whether ``invocation`` is a running session turn that ``item`` may join."""
    return (
        invocation.phase == SessionPhase.EXECUTING
        and invocation.registered_operation is None
        and scope_active(context, invocation.scope)
        and _matches(item, invocation, context)
    )


def _awaiting_offer(record: InputRecord) -> bool:
    return (
        record.input.mode is InputMode.INTERRUPT
        and record.receipt is None
        and record.reserved_to is None
        and record.offered_to is None
    )


def mid_turn_offers(state: SessionsState, context: SessionsContext) -> tuple[MidTurnOffer, ...]:
    """The running turn each pending interrupt-mode input can be offered to now, in order.

    An input with no running turn to join waits for its next turn instead.
    """
    offers = []
    for record in sorted(
        (row for row in state.inputs if _awaiting_offer(row)),
        key=lambda row: (row.input.sequence, row.input.input_id.root),
    ):
        invocation = next(
            (row for row in state.invocations if _takes_mid_turn(context, row, record.input)), None
        )
        if invocation is not None:
            offers.append(
                MidTurnOffer(
                    input=record.input, invocation=invocation.invocation, scope=invocation.scope
                )
            )
    return tuple(offers)


def _interrupt_record(state: SessionsState, input_id: InputId) -> InputRecord:
    record = next((row for row in state.inputs if row.input.input_id == input_id), None)
    if record is None or record.input.mode is not InputMode.INTERRUPT:
        raise ContractValidationError("input_id", "names no interrupt-mode input of this run")
    return record


def _with_record(state: SessionsState, record: InputRecord) -> SessionsState:
    identity = record.input.input_id
    rows = tuple(record if row.input.input_id == identity else row for row in state.inputs)
    if not any(row.input.input_id == identity for row in state.inputs):
        rows = (*rows, record)
    return state.model_copy(update={"inputs": rows})


def _offered(
    state: SessionsState, context: SessionsContext, event: InputOfferedMidTurn
) -> AreaChange[SessionsState]:
    """Record that the shell may now ask the provider; an offer no turn can take is a no-op."""
    record = _interrupt_record(state, event.input_id)
    invocation = proven_invocation(state, event.invocation)
    if (
        not _awaiting_offer(record)
        or invocation is None
        or not _takes_mid_turn(context, invocation, record.input)
    ):
        return AreaChange(state=state)
    return AreaChange(
        state=_with_record(state, record.model_copy(update={"offered_to": event.invocation}))
    )


def _taken(
    state: SessionsState, context: SessionsContext, event: InputTakenMidTurn
) -> AreaChange[SessionsState]:
    """Record that the provider took an offer core still holds for that turn."""
    del context
    record = _interrupt_record(state, event.input_id)
    if (
        record.offered_to != event.invocation
        or record.receipt is not None
        or record.taken_by is not None
    ):
        return AreaChange(state=state)
    taken = record.model_copy(update={"taken_by": event.invocation})
    return AreaChange(state=_with_record(state, taken))


def _mid_turn_released(
    state: SessionsState, context: SessionsContext, event: InputMidTurnReleased
) -> AreaChange[SessionsState]:
    """Return an offer that did not stay in the turn to wait for the next one.

    A refusal ends this turn's take: its agent did not read the message. Any other release
    keeps the take as ``possibly_read``, because a host that died after the provider took the
    message cannot say otherwise. A take by an earlier turn is kept either way.
    """
    record = _interrupt_record(state, event.input_id)
    if record.offered_to != event.invocation or record.receipt is not None:
        return AreaChange(state=state)
    if event.refused:
        record = record.model_copy(update={"taken_by": None})
    invocation = proven_invocation(state, event.invocation)
    if invocation is None:
        return AreaChange(state=_with_record(state, _pending(record)))
    released, receipt = _release_record(record, context, invocation, context.run.now_at)
    return AreaChange(
        state=_with_record(state, released), events=() if receipt is None else (receipt,)
    )


def _received_input(
    state: SessionsState, context: SessionsContext, event: SessionInputReceived
) -> AreaChange[SessionsState]:
    return _received(state, context, event.input)


def _steers(
    state: SessionsState, context: SessionsContext, event: SteerReceived
) -> AreaChange[SessionsState]:
    events = []
    for item in event.inputs:
        received = _received(state, context, item)
        state = received.state
        events.extend(received.events)
    return AreaChange(state=state, events=tuple(events))


_HANDLERS: dict[
    type, Callable[[SessionsState, SessionsContext, Any], AreaChange[SessionsState]]
] = {
    SessionInputReceived: _received_input,
    InputWithdrawRequested: _withdraw,
    InputOfferedMidTurn: _offered,
    InputTakenMidTurn: _taken,
    InputMidTurnReleased: _mid_turn_released,
    SteerReceived: _steers,
    InputReservationRequested: _reserve,
    InputAcceptanceObserved: _acceptance,
    InputReservationReleased: _acceptance,
    InterruptRequested: _interrupt,
    InvocationCheckpointAvailable: _checkpoint,
    InvocationChargeRefunded: _refunded,
    SessionDrainRequested: _drain,
}
"""Per input-owned event type, the reducer that consumes it."""


def advance(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Consume input-owned ingress and committed sibling facts, preserving siblings."""
    handler = _HANDLERS.get(type(event))
    if handler is None:
        raise ContractValidationError("event.kind", "event is owned by session turns")
    return handler(state, context, event)


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
