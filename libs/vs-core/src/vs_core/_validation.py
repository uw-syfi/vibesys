"""Strict kernel rejection before charging or preparing record."""

from __future__ import annotations

from math import isfinite
from typing import TYPE_CHECKING

from ._adoption import fences_root_mutation
from ._operator_actions import cancelled_items
from ._proofs import Proven, descriptor_matches
from ._run_park import drain_bound
from ._values import canonical_json, deeply_immutable
from .types.attempts import AttemptPhase
from .types.common import (
    AttemptRef,
    InvocationRef,
    LifecycleCapability,
    OperationDescriptor,
    OperationNormalizationKind,
    OperationRef,
    RejectionCode,
    RunStatus,
    WorkspaceMode,
)
from .types.records import RecoveryPhase
from .types.strategy import (
    Cancel,
    Command,
    Finish,
    Interrupt,
    Measure,
    Operation,
    Park,
    ProposeWinner,
    Rejected,
    RequestTurn,
    Settle,
    StartAttempt,
    Submission,
    Wait,
    Withdraw,
)
from .types.until import WhileAlive, WhileOpen, Within

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.kernel import CoreState, DecisionSubmitted, RunState


def _reject(
    decision: Command, code: RejectionCode, path: tuple[str | int, ...], detail: str
) -> Rejected:
    return Rejected(decision_id=decision.decision_id, code=code, path=path, detail=detail)


def takes_command(run: RunState, command: Command, *, stopping: bool = False) -> bool:
    """Whether the run's status lets core take ``command`` now.

    An open run takes any command. A run that is not open (paused, closing, blocked, or with
    its result proposed) still takes a ``Finish`` and a ``Withdraw`` until it is terminal, except
    that a run in a pause (draining or parked) holds its search still: only the operator's stop
    (``stopping``) ends it, and a stop ends the run whatever it is doing.
    """
    if run.status == RunStatus.RUNNING and run.result is None:
        return True
    if stopping and isinstance(command, Finish):
        return run.status != RunStatus.TERMINAL
    return isinstance(command, Finish | Withdraw) and run.status not in (
        RunStatus.TERMINAL,
        RunStatus.DRAINING,
        RunStatus.PARKED,
    )


def admits_command(state: CoreState, command: Command, *, stopping: bool = False) -> bool:
    """Whether core takes ``command`` now: its status takes it, or a pause's drain admits it.

    The one decision gate. ``validate_decision`` refuses a command it fails, and the shell
    holds (``deferred``) a command it fails while the run is paused, so the two cannot disagree.
    A drain admits the successor of a turn it admitted (``drain_bound``); the session gates then
    admit that turn (``admitted_work_proceeds``).
    """
    return (
        takes_command(state.run, command, stopping=stopping)
        or drain_bound(state, command) is not None
    )


def validate_decision(
    state: CoreState,
    event: DecisionSubmitted,
    *,
    check_revision: bool = True,
    stopping: bool = False,
) -> Rejected | None:
    submission = event.decision
    decision = event.command
    if check_revision and event.expected_revision != state.revision:
        return _reject(
            decision, RejectionCode.STALE_VIEW, ("expected_revision",), "view revision changed"
        )
    if not admits_command(state, decision, stopping=stopping):
        return _reject(
            decision, RejectionCode.CLOSED_SCOPE, ("scope",), "run not accepting decisions"
        )
    rejection = (
        validate_scope(state, decision)
        or _validate_root_fence(state, decision)
        or _cancelled_item(state, decision)
    )
    if rejection is not None:
        return rejection
    if isinstance(decision, Finish):
        rejection = _validate_stop_result(state, decision)
        if rejection is not None:
            return rejection
    return _validate_until(state, submission, decision) or validate_offer(state, decision)


def _cancelled_item(state: CoreState, decision: Command) -> Rejected | None:
    """No attempt starts or reopens at a work item the operator cancelled (``cancel_item``)."""
    if isinstance(decision, StartAttempt):
        item, path = decision.item_id, ("item_id",)
    elif isinstance(decision, Operation) and decision.normalized_scope_reopen is not None:
        reopened = decision.normalized_scope_reopen.attempt
        item = next(
            (
                attempt.item_id
                for attempt in state.attempts.attempts
                if (attempt.attempt_id, attempt.generation)
                == (reopened.attempt_id, reopened.generation)
            ),
            None,
        )
        path = ("normalized_scope_reopen", "attempt")
    else:
        return None
    if item is None or item not in cancelled_items(state.run):
        return None
    return _reject(decision, RejectionCode.CLOSED_SCOPE, path, "the operator cancelled the item")


def _validate_until(state: CoreState, submission: Submission, decision: Command) -> Rejected | None:
    """Runtime backstop for the types: a wait has a positive limit and the run has time left.

    Withdrawing an attempt and proposing the winner need no time left.

    The types already make a bare request, a missing ``until`` and a watch used as the
    ``until`` unrepresentable; this catches values built around them.
    """
    if isinstance(submission, Finish):
        return None
    if not isinstance(submission, Wait):
        return _reject(
            decision, RejectionCode.UNKNOWN_KIND, ("kind",), "only Wait and Finish start work"
        )
    if not isinstance(submission.until, Within) or not all(
        isinstance(watch, WhileOpen | WhileAlive) for watch in submission.until.watches
    ):
        return _reject(decision, RejectionCode.UNKNOWN_KIND, ("until",), "until must be a Within")
    if not submission.until.limit > 0 or not isfinite(submission.until.limit):
        return _reject(decision, RejectionCode.BUDGET, ("until", "limit"), "limit must be positive")
    # Closing out (settling a workspace, adopting the winner) is allowed after the deadline:
    # the run ends by it, within the cleanup bound its caller chose.
    closing = isinstance(submission.request, Withdraw | ProposeWinner)
    if not closing and not state.run.deadline_at > state.run.now_at:
        return _reject(
            decision,
            RejectionCode.BUDGET,
            ("until", "limit"),
            "run has no time left for the request",
        )
    return None


def _validate_stop_result(state: CoreState, decision: Finish) -> Rejected | None:
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


def _validate_withdraw(state: CoreState, decision: Withdraw) -> Rejected | None:
    """The target a disposition acts on, and the revision a settlement keeps."""
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
    return _unheld_candidate(state, decision)


def _unheld_candidate(state: CoreState, decision: Withdraw) -> Rejected | None:
    """A settlement may keep only a revision its attempt holds: a checkpoint or its base.

    The workspace owner retains a revision only if it took or was given it. A settlement
    that named any other revision would be accepted here and rejected at closure, and a
    rejected retention is never released, so the attempt would stay closing forever. The
    search is told at once instead, and its workspace settles plainly.
    """
    proposal = decision.disposition
    if (
        not isinstance(proposal, Settle)
        or proposal.retention == "discard"
        or proposal.candidate is None
    ):
        return None
    owner = next((row for row in state.attempts.attempts if _is_target(row, decision)), None)
    if owner is None or proposal.candidate == owner.workspace.base:
        return None
    if any(row.revision == proposal.candidate for row in owner.checkpoints):
        return None
    return _reject(
        decision,
        RejectionCode.EVIDENCE,
        ("disposition", "candidate"),
        "the candidate is neither a checkpoint of the attempt nor its base revision",
    )


def _is_target(owner: AttemptView, decision: Withdraw) -> bool:
    return (
        isinstance(decision.target, AttemptRef)
        and owner.attempt_id == decision.target.attempt_id
        and owner.generation == decision.target.generation
    )


def _required_capability(decision: Command) -> LifecycleCapability | None:
    """The host capability a decision depends on, if any."""
    if isinstance(decision, Withdraw):
        if isinstance(decision.disposition, Park):
            return "park"
        if isinstance(decision.disposition, Interrupt):
            return "interrupt"
    if isinstance(decision, Measure) and decision.plan.purpose == "profile":
        return "profile-capture"
    return None


def validate_offer(state: CoreState, decision: Command) -> Rejected | None:
    if (
        isinstance(decision, Finish)
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
    if isinstance(decision, Withdraw) and (refused := _validate_withdraw(state, decision)):
        return refused
    capability = _required_capability(decision)
    if capability is not None and capability not in state.run.capabilities.lifecycle:
        return _reject(
            decision, RejectionCode.CAPABILITY, ("disposition", "kind"), f"unsupported {capability}"
        )
    if isinstance(decision, Operation):
        return validate_operation(state, decision)
    return None


def _validate_root_fence(state: CoreState, decision: Command) -> Rejected | None:
    """No decision takes the root workspace while an adoption is rewriting it."""
    if not fences_root_mutation(state.settlement, state.records):
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


def validate_scope(state: CoreState, decision: Command) -> Rejected | None:
    if state.records.recovery.phase != RecoveryPhase.READY and not isinstance(
        decision, Finish | Withdraw
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
