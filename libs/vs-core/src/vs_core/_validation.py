"""Strict kernel rejection before charging or preparing intent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._values import canonical_json
from .types.attempts import AttemptPhase
from .types.common import (
    AttemptRef,
    InvocationRef,
    OperationRef,
    OperationSchemaRef,
    RejectionCode,
    RunStatus,
)
from .types.strategy import Cancel, Decision, Interrupt, Operation, Park, Rejected, Stop, Withdraw

if TYPE_CHECKING:
    from .types.kernel import CoreState, DecisionSubmitted


def _reject(
    decision: Decision, code: RejectionCode, path: tuple[str | int, ...], detail: str
) -> Rejected:
    return Rejected(decision_id=decision.decision_id, code=code, path=path, detail=detail)


def validate_decision(state: CoreState, event: DecisionSubmitted) -> Rejected | None:
    decision = event.decision
    if event.expected_revision != state.revision:
        return _reject(
            decision, RejectionCode.STALE_VIEW, ("expected_revision",), "view revision changed"
        )
    if state.run.status != RunStatus.RUNNING and not (
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
        if receipt is None or isinstance(receipt.feedback, Rejected):
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
        wire.payload_json != canonical_json(decision.request)
        or decision.normalized_turn != decision.registered_turn
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
    if offered.lifecycle != decision.request.lifecycle:
        return _reject(
            decision,
            RejectionCode.UNKNOWN_SCHEMA,
            ("request", "lifecycle"),
            "operation lifecycle mismatch",
        )
    return None
