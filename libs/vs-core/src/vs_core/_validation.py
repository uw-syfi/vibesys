"""Strict kernel rejection before charging or preparing intent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._adoption import fences_root_mutation
from ._proofs import Proven, accepted_receipt_for, descriptor_matches
from ._values import canonical_json, deeply_immutable
from .types.attempts import AttemptPhase
from .types.common import (
    AttemptRef,
    CompletionStatus,
    InvocationRef,
    LifecycleCapability,
    OperationDescriptor,
    OperationNormalizationKind,
    OperationRef,
    RejectionCode,
    RunStatus,
    WorkspaceMode,
)
from .types.intents import RecoveryPhase
from .types.strategy import (
    Cancel,
    Decision,
    Interrupt,
    Measure,
    Operation,
    Park,
    Rejected,
    RequestTurn,
    StartAttempt,
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
    rejection = validate_scope(state, decision) or _validate_root_fence(state, decision)
    if rejection is not None:
        return rejection
    for dependency in decision.depends_on:
        proof = accepted_receipt_for(state.run.receipts, dependency, None)
        if not isinstance(proof, Proven) or proof.value.completion in (
            CompletionStatus.FAILED,
            CompletionStatus.CANCELLED,
        ):
            return _reject(
                decision, RejectionCode.DEPENDENCY, ("depends_on",), "dependency not accepted"
            )
    if isinstance(decision, Stop):
        rejection = _validate_stop_result(state, decision)
        if rejection is not None:
            return rejection
    return validate_offer(state, decision)


def _validate_stop_result(state: CoreState, decision: Stop) -> Rejected | None:
    """A claimed success needs completed work, and a named winner needs its verified adoption."""
    result = decision.result
    if result.outcome != "success":
        return None
    if not state.settlement.settlements and not state.run.requirements.allow_empty_queue_success:
        return _reject(
            decision,
            RejectionCode.EVIDENCE,
            ("result", "outcome"),
            "zero completed work cannot claim success",
        )
    adoption = state.settlement.adoption
    if result.selection is not None and not (
        adoption is not None and adoption.verified and adoption.selection == result.selection
    ):
        return _reject(
            decision,
            RejectionCode.EVIDENCE,
            ("result", "selection"),
            "a successful result must name the verified adopted selection",
        )
    return None


def _required_capability(decision: Decision) -> LifecycleCapability | None:
    """The host capability a decision depends on, if any."""
    if isinstance(decision, Withdraw):
        if isinstance(decision.disposition, Park):
            return "park"
        if isinstance(decision.disposition, Interrupt):
            return "interrupt"
    if isinstance(decision, Measure) and decision.plan.purpose == "profile":
        return "profile-capture"
    return None


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
    capability = _required_capability(decision)
    if capability is not None and capability not in state.run.capabilities.lifecycle:
        return _reject(
            decision, RejectionCode.CAPABILITY, ("disposition", "kind"), f"unsupported {capability}"
        )
    if isinstance(decision, Operation):
        return validate_operation(state, decision)
    return None


def _validate_root_fence(state: CoreState, decision: Decision) -> Rejected | None:
    """No decision takes the root workspace while an adoption is rewriting it."""
    if not fences_root_mutation(state.settlement, state.intents):
        return None
    if isinstance(decision, StartAttempt):
        takes_root = decision.workspace.mode == WorkspaceMode.EXCLUSIVE_ROOT
    elif isinstance(decision, Operation) and decision.normalized_scope_reopen is not None:
        reopened = decision.normalized_scope_reopen.attempt
        takes_root = any(
            attempt.workspace.mode == WorkspaceMode.EXCLUSIVE_ROOT
            and (attempt.attempt_id, attempt.generation)
            == (reopened.attempt_id, reopened.generation)
            for attempt in state.attempts.attempts
        )
    else:
        takes_root = False
    if not takes_root:
        return None
    return _reject(
        decision, RejectionCode.DEPENDENCY, ("workspace",), "root workspace adoption in progress"
    )


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
    declaration = descriptor_matches(
        state.registry,
        state.run.capabilities,
        wire,
        decision.request.lifecycle,
        offered.normalization,
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
    if not isinstance(declaration, Proven):
        return _reject(
            decision,
            RejectionCode.UNKNOWN_SCHEMA,
            ("request", "schema"),
            "registered operation schema mismatch",
        )
    return _validate_operation_semantics(state, decision, offered)
