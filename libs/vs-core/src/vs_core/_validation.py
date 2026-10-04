"""Strict kernel rejection before charging or preparing intent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._values import canonical_json, deeply_immutable
from .types.attempts import AttemptPhase
from .types.common import (
    AttemptRef,
    CompletionStatus,
    InvocationRef,
    OperationDescriptor,
    OperationNormalizationKind,
    OperationRef,
    OperationSchemaRef,
    RejectionCode,
    RunStatus,
)
from .types.intents import RecoveryPhase
from .types.strategy import (
    Cancel,
    Decision,
    Interrupt,
    Operation,
    Park,
    Rejected,
    RequestTurn,
    Stop,
    Withdraw,
)

if TYPE_CHECKING:
    from .types.kernel import CoreState, DecisionSubmitted


def _reject(
    decision: Decision, code: RejectionCode, path: tuple[str | int, ...], detail: str
) -> Rejected:
    return Rejected(decision_id=decision.decision_id, code=code, path=path, detail=detail)


def validate_decision(
    state: CoreState, event: DecisionSubmitted, *, check_revision: bool = True
) -> Rejected | None:
    decision = event.decision
    if check_revision and event.expected_revision != state.revision:
        return _reject(
            decision, RejectionCode.STALE_VIEW, ("expected_revision",), "view revision changed"
        )
    if (state.run.status != RunStatus.RUNNING or state.run.result is not None) and not (
        isinstance(decision, Stop | Withdraw) and state.run.status != RunStatus.TERMINAL
    ):
        return _reject(
            decision, RejectionCode.CLOSED_SCOPE, ("scope",), "run not accepting decisions"
        )
    rejection = validate_scope(state, decision)
    if rejection is not None:
        return rejection
    for dependency in decision.depends_on:
        receipt = next(
            (receipt for receipt in state.run.receipts if receipt.decision_id == dependency),
            None,
        )
        if (
            receipt is None
            or isinstance(receipt.feedback, Rejected)
            or receipt.completion in (CompletionStatus.FAILED, CompletionStatus.CANCELLED)
        ):
            return _reject(
                decision, RejectionCode.DEPENDENCY, ("depends_on",), "dependency not accepted"
            )
    if (
        isinstance(decision, Stop)
        and decision.result.outcome == "success"
        and not state.settlement.settlements
        and not state.run.requirements.allow_empty_queue_success
    ):
        return _reject(
            decision,
            RejectionCode.EVIDENCE,
            ("result", "outcome"),
            "zero completed work cannot claim success",
        )
    return validate_offer(state, decision)


def validate_offer(state: CoreState, decision: Decision) -> Rejected | None:
    if (
        isinstance(decision, Stop)
        and state.run.result is not None
        and decision.result != state.run.result
    ):
        return _reject(
            decision,
            RejectionCode.IDENTITY_CONFLICT,
            ("result",),
            "accepted stop result is immutable",
        )
    turn = (
        decision.turn
        if isinstance(decision, RequestTurn)
        else (decision.normalized_turn if isinstance(decision, Operation) else None)
    )
    if (
        turn is not None
        and decision.scope.owner == state.run.run_id
        and turn.charge_class == "paid"
    ):
        return _reject(
            decision,
            RejectionCode.OWNERSHIP,
            ("turn", "charge_class"),
            "run-owned turns cannot consume attempt charges",
        )
    if isinstance(decision, Withdraw):
        target_valid = (
            isinstance(decision.target, InvocationRef)
            if isinstance(decision.disposition, Interrupt)
            else isinstance(decision.target, AttemptRef | OperationRef)
            if isinstance(decision.disposition, Cancel)
            else isinstance(decision.target, AttemptRef)
        )
        if not target_valid:
            return _reject(
                decision, RejectionCode.OWNERSHIP, ("target",), "target/disposition mismatch"
            )
    capability = None
    if isinstance(decision, Withdraw):
        if isinstance(decision.disposition, Park):
            capability = "park"
        elif isinstance(decision.disposition, Interrupt):
            capability = "interrupt"
    if capability is not None and capability not in state.run.capabilities.lifecycle:
        return _reject(
            decision, RejectionCode.CAPABILITY, ("disposition", "kind"), f"unsupported {capability}"
        )
    if isinstance(decision, Operation):
        return validate_operation(state, decision)
    return None


def validate_scope(state: CoreState, decision: Decision) -> Rejected | None:
    if state.intents.recovery.phase != RecoveryPhase.READY and not isinstance(
        decision, Stop | Withdraw
    ):
        return _reject(
            decision,
            RejectionCode.CLOSED_SCOPE,
            ("recovery",),
            "ordinary decisions require ready recovery",
        )
    if decision.scope.owner == state.run.run_id:
        generation = state.run.generation
    else:
        owner = next(
            (
                attempt
                for attempt in state.attempts.attempts
                if attempt.attempt_id == decision.scope.owner
            ),
            None,
        )
        if owner is None:
            return _reject(decision, RejectionCode.OWNERSHIP, ("scope", "owner"), "unknown owner")
        if owner.phase in (
            AttemptPhase.TERMINAL,
            AttemptPhase.BLOCKED,
            AttemptPhase.CLOSING,
            AttemptPhase.PARKED,
        ):
            return _reject(
                decision, RejectionCode.CLOSED_SCOPE, ("scope", "owner"), "attempt scope closed"
            )
        generation = owner.generation
    if decision.scope.generation != generation:
        return _reject(
            decision,
            RejectionCode.GENERATION,
            ("scope", "generation"),
            "stale ownership generation",
        )
    return None


def _validate_operation_semantics(
    state: CoreState, decision: Operation, offered: OperationDescriptor
) -> Rejected | None:
    """Validate semantic scope after registered payload/schema proofs succeed."""
    if (
        offered.normalization == OperationNormalizationKind.SCOPE_REOPEN
        and decision.scope.owner != state.run.run_id
    ):
        return _reject(
            decision,
            RejectionCode.OWNERSHIP,
            ("scope", "owner"),
            "scope reopening requires a run-scoped operation",
        )
    if offered.lifecycle != decision.request.lifecycle:
        return _reject(
            decision,
            RejectionCode.UNKNOWN_SCHEMA,
            ("request", "lifecycle"),
            "operation lifecycle mismatch",
        )
    return None


def validate_operation(state: CoreState, decision: Operation) -> Rejected | None:
    offered = next(
        (
            descriptor
            for descriptor in state.run.capabilities.operations
            if descriptor.kind == decision.request.kind
        ),
        None,
    )
    if offered is None:
        return _reject(
            decision,
            RejectionCode.UNDECLARED_OPERATION,
            ("request", "kind"),
            "operation not declared and offered",
        )
    registered = next(
        (descriptor for descriptor in state.registry if descriptor.kind == decision.request.kind),
        None,
    )
    wire = decision.registered_wire
    if registered is None or wire is None:
        return _reject(
            decision,
            RejectionCode.UNKNOWN_SCHEMA,
            ("request", "schema"),
            "registered codec ingress required",
        )
    schema = OperationSchemaRef(
        kind=registered.kind,
        request_schema=registered.request_schema,
        outcome_schema=registered.outcome_schema,
        lifecycle=registered.lifecycle,
    )
    if (
        not deeply_immutable(decision.request)
        or wire.payload_json != canonical_json(decision.request)
        or decision.normalized_turn != decision.registered_turn
        or decision.normalized_measurement != decision.registered_measurement
        or decision.normalized_scope_reopen != decision.registered_scope_reopen
    ):
        return _reject(
            decision,
            RejectionCode.UNKNOWN_SCHEMA,
            ("request", "payload"),
            "registered codec proof does not match current payload",
        )
    if wire.schema_ref != schema or registered != offered:
        return _reject(
            decision,
            RejectionCode.UNKNOWN_SCHEMA,
            ("request", "schema"),
            "registered operation schema mismatch",
        )
    return _validate_operation_semantics(state, decision, offered)
