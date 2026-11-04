"""Total value-only proof predicates. Only Proven grants a required fact.

Presence is checked before identity, payload, status and completeness. Within
identity checks the ProofField declaration order determines failure precedence.
Predicates never infer a canonical expectation from the evidence being checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ._values import canonical_json, deeply_immutable, digest
from .types.common import (
    AttemptId,
    AttemptRef,
    Capabilities,
    ChargeKind,
    DecisionId,
    DependencyStatus,
    ExecuteRegisteredOperation,
    InvocationRef,
    LifecycleClass,
    Observation,
    ObservationStatus,
    OperationDescriptor,
    OperationId,
    OperationNormalizationKind,
    OperationWire,
    RequestBase,
    RevisionRef,
    RunStatus,
    Scope,
    StopMode,
    escalate_stop,
)
from .types.evaluation import (
    MeasurementIdentity,
    OwnedJob,
    PreparedSubmissionReceipt,
    RegisteredOwnedJob,
    SubmissionBudget,
    SubmitMeasurement,
)
from .types.records import (
    RecordsState,
    Request,
    RequestPhase,
    RequestPrepared,
    RequestRecord,
    request_lifecycle,
)
from .types.scheduling import AdmissionRequest, AttemptReopenRequest, Slot
from .types.sessions import CloseSession, Invocation, SessionPhase, SessionView, TurnSpec
from .types.strategy import Accepted, Command, Finish, Operation

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .types.attempts import AttemptClosure, AttemptView
    from .types.kernel import DecisionReceipt, RunState


class ProofReason(StrEnum):
    """Closed reasons why a required fact is unavailable."""

    ABSENT_RECEIPT = "absent_receipt"
    NOT_ACCEPTED = "not_accepted"
    ABSENT_DECLARATION = "absent_declaration"
    ABSENT_EPISODE = "absent_episode"
    ABSENT_REQUEST = "absent_request"
    ABSENT_OBSERVATION = "absent_observation"
    ABSENT_INVOCATION = "absent_invocation"
    ABSENT_SESSION = "absent_session"
    ABSENT_RESOURCE = "absent_resource"
    ABSENT_CHARGE = "absent_charge"
    ABSENT_CHECKPOINT = "absent_checkpoint"
    ABSENT_PUBLICATION = "absent_publication"
    UNRESOLVED = "unresolved"
    INCOMPLETE_MANIFEST = "incomplete_manifest"
    INCOMPLETE_HISTORY = "incomplete_history"
    EMPTY_REQUIRED = "empty_required"


class ProofField(StrEnum):
    """Closed mismatches in deterministic identity-first order."""

    RECEIPT_ID = "receipt_id"
    FEEDBACK_ID = "feedback_id"
    DECISION_ID = "decision_id"
    REQUEST_ID = "request_id"
    INVOCATION_ID = "invocation_id"
    SESSION_ID = "session_id"
    RESOURCE_ID = "resource_id"
    SCOPE = "scope"
    GENERATION = "generation"
    ADMISSION_ID = "admission_id"
    PAYLOAD = "payload"
    DIGEST = "digest"
    SCHEMA = "schema"
    LIFECYCLE = "lifecycle"
    NORMALIZATION = "normalization"
    REVISION = "revision"
    RETENTION = "retention"
    SEQUENCE = "sequence"
    STATUS = "status"
    MANIFEST = "manifest"
    CURRENCY = "currency"
    AMOUNT = "amount"
    CURSOR = "cursor"
    PUBLICATION = "publication"
    DISPOSITION = "disposition"


class _ImplicitProofTruthError(TypeError):
    def __init__(self) -> None:
        super().__init__("proof verdict requires an explicit Proven/Missing/Mismatch match")


class _ProofEnumError(TypeError):
    def __init__(self, field: str) -> None:
        super().__init__(f"proof {field} requires its closed enum")


class _ExplicitVerdict:
    def __bool__(self) -> bool:
        raise _ImplicitProofTruthError


@dataclass(frozen=True)
class Proven[T](_ExplicitVerdict):
    """The exact matched fact; no additional leaf policy is implied."""

    value: T


@dataclass(frozen=True)
class Missing(_ExplicitVerdict):
    """A required fact is absent or cannot establish authority."""

    reason: ProofReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ProofReason):
            raise _ProofEnumError("reason")


@dataclass(frozen=True)
class Mismatch(_ExplicitVerdict):
    """Evidence disagrees with the independently established expectation."""

    field: ProofField

    def __post_init__(self) -> None:
        if not isinstance(self.field, ProofField):
            raise _ProofEnumError("field")


type Verdict[T] = Proven[T] | Missing | Mismatch


_FIELD_ORDER = {field: position for position, field in enumerate(ProofField)}


def _identity_mismatch(checks: tuple[tuple[ProofField, object, object], ...]) -> Mismatch | None:
    """Compare independent fields in declared order without verdict truthiness."""
    ordered = sorted(checks, key=lambda check: _FIELD_ORDER[check[0]])
    return next(
        (Mismatch(field) for field, actual, expected in ordered if actual != expected), None
    )


def accepted_receipt_for(
    receipts: tuple[DecisionReceipt, ...],
    identity: DecisionId | None,
    canonical: Command | None,
) -> Verdict[DecisionReceipt]:
    """Prove accepted identity; a supplied ingress decision also proves payload equality.

    ID-only ingress validates the retained canonical command's internal integrity.
    It grants no independently expected payload, request or normalization binding.
    """
    if identity is None:
        return Missing(ProofReason.ABSENT_RECEIPT)
    rows = tuple(receipt for receipt in receipts if receipt.decision_id == identity)
    if not rows:
        return Missing(ProofReason.ABSENT_RECEIPT)
    if len(rows) != 1:
        return Mismatch(ProofField.RECEIPT_ID)
    receipt = rows[0]
    if receipt.decision is None:
        return Missing(ProofReason.ABSENT_RECEIPT)
    if not isinstance(receipt.feedback, Accepted):
        return Missing(ProofReason.NOT_ACCEPTED)
    checks = (
        (ProofField.FEEDBACK_ID, receipt.feedback.decision_id, identity),
        (ProofField.DECISION_ID, receipt.decision.decision_id, identity),
    )
    if canonical is not None:
        checks += (
            (ProofField.DECISION_ID, canonical.decision_id, identity),
            (ProofField.SCOPE, receipt.decision.scope.owner, canonical.scope.owner),
            (ProofField.GENERATION, receipt.decision.scope.generation, canonical.scope.generation),
        )
    fields = _identity_mismatch(checks)
    return fields if fields is not None else _receipt_payload(receipt, canonical)


def _receipt_payload(
    receipt: DecisionReceipt, canonical: Command | None
) -> Verdict[DecisionReceipt]:
    try:
        if receipt.decision is None:
            return Missing(ProofReason.ABSENT_RECEIPT)
        if not deeply_immutable(receipt.decision):
            return Mismatch(ProofField.PAYLOAD)
        if canonical is not None and (
            not deeply_immutable(canonical)
            or canonical_json(receipt.decision) != canonical_json(canonical)
        ):
            return Mismatch(ProofField.PAYLOAD)
        # Without an independent payload expectation, this proves only the
        # persisted command's integrity. ID-only ingress grants no payload binding.
        if receipt.payload_digest != digest(receipt.decision):
            return Mismatch(ProofField.DIGEST)
    except (TypeError, ValueError):
        return Mismatch(ProofField.PAYLOAD)
    return Proven(receipt)


def nonempty_required[T](proofs: tuple[Verdict[T], ...]) -> Verdict[tuple[T, ...]]:
    """Prove every declared requirement without filtering any failed fact."""
    if not proofs:
        return Missing(ProofReason.EMPTY_REQUIRED)
    values = []
    for proof in proofs:
        if not isinstance(proof, Proven):
            return proof
        values.append(proof.value)
    return Proven(tuple(values))


def current_admission(
    attempt: AttemptView | None, scope: Scope, episode: DecisionId | None
) -> Verdict[DecisionId]:
    """Prove current owner and generation against a nonmissing admission."""
    if attempt is None:
        return Missing(ProofReason.ABSENT_DECLARATION)
    if episode is None or attempt.admission_id is None:
        return Missing(ProofReason.ABSENT_EPISODE)
    if attempt.attempt_id != scope.owner:
        return Mismatch(ProofField.SCOPE)
    if attempt.generation != scope.generation:
        return Mismatch(ProofField.GENERATION)
    if attempt.admission_id != episode:
        return Mismatch(ProofField.ADMISSION_ID)
    return Proven(episode)


def current_closure(
    attempt: AttemptView | None, closure: AttemptClosure | None
) -> Verdict[AttemptClosure]:
    """Prove the exact recorded closure of the current nonmissing admission."""
    if attempt is None:
        return Missing(ProofReason.ABSENT_DECLARATION)
    if closure is None or attempt.closure is None:
        return Missing(ProofReason.ABSENT_REQUEST)
    if attempt.admission_id is None:
        return Missing(ProofReason.ABSENT_EPISODE)
    recorded = attempt.closure
    fields = _identity_mismatch(
        (
            (ProofField.REQUEST_ID, closure.authority, recorded.authority),
            (ProofField.ADMISSION_ID, closure.admission_id, attempt.admission_id),
            (ProofField.ADMISSION_ID, recorded.admission_id, attempt.admission_id),
            (ProofField.PAYLOAD, closure.requested_at, recorded.requested_at),
            (ProofField.DISPOSITION, closure.disposition, recorded.disposition),
        )
    )
    return fields if fields is not None else Proven(closure)


def request_matches(
    record: RequestRecord | None, expected: Request | None
) -> Verdict[RequestRecord]:
    """Prove complete canonical outbox payload, including its recorded episode."""
    if record is None or expected is None or expected.request_id is None:
        return Missing(ProofReason.ABSENT_REQUEST)
    request = record.request
    if request.request_id is None:
        return Missing(ProofReason.ABSENT_REQUEST)
    if isinstance(request.scope.owner, AttemptId) and (
        request.admission_id is None or expected.admission_id is None
    ):
        return Missing(ProofReason.ABSENT_EPISODE)
    fields = _identity_mismatch(
        (
            (ProofField.REQUEST_ID, record.request_id, expected.request_id),
            (ProofField.REQUEST_ID, request.request_id, expected.request_id),
            (ProofField.SCOPE, request.scope.owner, expected.scope.owner),
            (ProofField.GENERATION, request.scope.generation, expected.scope.generation),
            (ProofField.ADMISSION_ID, request.admission_id, expected.admission_id),
        )
    )
    return fields if fields is not None else _request_payload(record, expected)


def _request_payload(record: RequestRecord, expected: Request) -> Verdict[RequestRecord]:
    try:
        if canonical_json(record.request) != canonical_json(expected):
            return Mismatch(ProofField.PAYLOAD)
        if record.payload_digest != digest(expected):
            return Mismatch(ProofField.DIGEST)
    except (TypeError, ValueError):
        return Mismatch(ProofField.PAYLOAD)
    if record.lifecycle != request_lifecycle(expected):
        return Mismatch(ProofField.LIFECYCLE)
    return Proven(record)


def observation_for(
    record: RequestRecord | None, observation: Observation | None
) -> Verdict[Observation]:
    """Correlate target facts in their own source sequence and recorded episode."""
    if record is None or record.request.request_id is None:
        return Missing(ProofReason.ABSENT_REQUEST)
    if observation is None:
        return Missing(ProofReason.ABSENT_OBSERVATION)
    request = record.request
    if isinstance(request.scope.owner, AttemptId) and (
        request.admission_id is None or observation.admission_id is None
    ):
        return Missing(ProofReason.ABSENT_EPISODE)
    fields = _identity_mismatch(
        (
            (ProofField.REQUEST_ID, observation.request_id, record.request_id),
            (ProofField.REQUEST_ID, request.request_id, record.request_id),
            (ProofField.SCOPE, observation.scope.owner, request.scope.owner),
            (ProofField.GENERATION, observation.scope.generation, request.scope.generation),
            (ProofField.ADMISSION_ID, observation.admission_id, request.admission_id),
        )
    )
    return fields if fields is not None else Proven(observation)


def fresh_observation(
    history: tuple[Observation, ...], incoming: Observation | None, *, complete: bool
) -> Verdict[Observation]:
    """Complete source history permits newer or identical replay, without rebinding."""
    if incoming is None:
        return Missing(ProofReason.ABSENT_OBSERVATION)
    if not complete:
        return Missing(ProofReason.INCOMPLETE_HISTORY)
    rows = tuple(row for row in history if row.request_id == incoming.request_id)
    if not rows:
        return Proven(incoming)
    return _fresh_source(rows, incoming)


def _fresh_source(rows: tuple[Observation, ...], incoming: Observation) -> Verdict[Observation]:
    """Presence, then enum-ordered identity over every row, then sequence conflicts.

    No failure depends on row order: every historical row must carry the
    incoming identity before any sequence watermark is chosen.
    """
    if any(
        isinstance(row.scope.owner, AttemptId) and row.admission_id is None
        for row in (*rows, incoming)
    ):
        return Missing(ProofReason.ABSENT_EPISODE)
    failures = [
        failure
        for row in rows
        if (
            failure := _identity_mismatch(
                (
                    (ProofField.SCOPE, incoming.scope.owner, row.scope.owner),
                    (ProofField.GENERATION, incoming.scope.generation, row.scope.generation),
                    (ProofField.ADMISSION_ID, incoming.admission_id, row.admission_id),
                )
            )
        )
        is not None
    ]
    if failures:
        order = tuple(ProofField)
        return min(failures, key=lambda failure: order.index(failure.field))
    if any(
        row.sequence == other.sequence and row != other
        for index, row in enumerate(rows)
        for other in rows[index + 1 :]
    ):
        return Mismatch(ProofField.SEQUENCE)
    latest = max(rows, key=lambda row: row.sequence)
    if incoming.sequence < latest.sequence or (
        incoming.sequence == latest.sequence and incoming != latest
    ):
        return Mismatch(ProofField.SEQUENCE)
    return Proven(incoming)


def invocation_for(
    invocations: tuple[Invocation, ...], expected: TurnSpec | InvocationRef, scope: Scope
) -> Verdict[Invocation]:
    """Prove one exact invocation, its session, scope, generation and turn."""
    identity = expected.invocation_id
    rows = tuple(row for row in invocations if row.invocation.invocation_id == identity)
    if not rows:
        return Missing(ProofReason.ABSENT_INVOCATION)
    if len(rows) != 1:
        return Mismatch(ProofField.INVOCATION_ID)
    row = rows[0]
    session = expected.session.session_id if isinstance(expected, TurnSpec) else expected.session_id
    # The invocation generation is the physical session generation; it is a
    # different namespace from the owner scope generation. A TurnSpec carries no
    # session generation, so only an InvocationRef can pin it.
    generation = (
        row.invocation.generation if isinstance(expected, TurnSpec) else expected.generation
    )
    fields = _identity_mismatch(
        (
            (ProofField.INVOCATION_ID, row.turn.invocation_id, identity),
            (ProofField.SESSION_ID, row.invocation.session_id, session),
            (ProofField.SESSION_ID, row.turn.session.session_id, session),
            (ProofField.SCOPE, row.scope.owner, scope.owner),
            (ProofField.GENERATION, row.scope.generation, scope.generation),
            (ProofField.GENERATION, row.invocation.generation, generation),
        )
    )
    if fields is not None:
        return fields
    if isinstance(expected, TurnSpec) and row.turn != expected:
        return Mismatch(ProofField.PAYLOAD)
    return Proven(row)


def descriptor_matches(
    registry: tuple[OperationDescriptor, ...],
    offered: Capabilities,
    wire: OperationWire | None,
    lifecycle: LifecycleClass,
    normalization: OperationNormalizationKind,
) -> Verdict[OperationDescriptor]:
    """Prove durable and offered declaration independently of codec availability."""
    if wire is None:
        return Missing(ProofReason.ABSENT_DECLARATION)
    rows = tuple(row for row in registry if row.kind == wire.schema_ref.kind)
    offers = tuple(row for row in offered.operations if row.kind == wire.schema_ref.kind)
    if not rows or not offers:
        return Missing(ProofReason.ABSENT_DECLARATION)
    if len(rows) != 1 or len(offers) != 1:
        return Mismatch(ProofField.SCHEMA)
    row = rows[0]
    fields = _identity_mismatch(
        (
            (ProofField.SCHEMA, row.request_schema, wire.schema_ref.request_schema),
            (ProofField.SCHEMA, row.outcome_schema, wire.schema_ref.outcome_schema),
            (ProofField.LIFECYCLE, row.lifecycle, lifecycle),
            (ProofField.LIFECYCLE, wire.schema_ref.lifecycle, lifecycle),
            (ProofField.NORMALIZATION, row.normalization, normalization),
            (ProofField.RESOURCE_ID, row.resource_pool, offers[0].resource_pool),
            (ProofField.SCHEMA, row.request_schema, offers[0].request_schema),
            (ProofField.SCHEMA, row.outcome_schema, offers[0].outcome_schema),
            (
                ProofField.SCHEMA,
                (row.inspect, row.cancel, row.watch),
                (offers[0].inspect, offers[0].cancel, offers[0].watch),
            ),
            (ProofField.LIFECYCLE, row.lifecycle, offers[0].lifecycle),
            (ProofField.NORMALIZATION, row.normalization, offers[0].normalization),
            (ProofField.REVISION, row.revision_authority, offers[0].revision_authority),
        )
    )
    return fields if fields is not None else Proven(row)


def operation_for(
    receipts: tuple[DecisionReceipt, ...],
    expected: Operation | RequestPrepared | ExecuteRegisteredOperation | None,
) -> Verdict[Operation]:
    """Prove accepted registered origin against ingress or owning normalization."""
    presence = _operation_expectation(expected)
    if not isinstance(presence, Proven):
        return presence
    expected = presence.value
    request = expected.request if isinstance(expected, RequestPrepared) else expected
    proof = accepted_receipt_for(
        receipts, request.decision_id, expected if isinstance(expected, Operation) else None
    )
    if not isinstance(proof, Proven):
        return proof
    operation = proof.value.decision
    if not isinstance(operation, Operation):
        return Mismatch(ProofField.PAYLOAD)
    binding = _operation_binding(operation)
    if not isinstance(binding, Proven):
        return binding
    if isinstance(expected, Operation):
        return _operation_ingress(operation, expected)
    return (
        _operation_request(proof.value, operation, request, expected)
        if isinstance(request, ExecuteRegisteredOperation)
        else Mismatch(ProofField.PAYLOAD)
    )


def _operation_expectation(
    expected: Operation | RequestPrepared | ExecuteRegisteredOperation | None,
) -> Verdict[Operation | RequestPrepared | ExecuteRegisteredOperation]:
    if expected is None:
        return Missing(ProofReason.ABSENT_REQUEST)
    if isinstance(expected, Operation) and (
        expected.registered_wire is None
        or (
            expected.request.lifecycle == LifecycleClass.SESSION_TURN
            and expected.registered_turn is None
        )
    ):
        return Missing(ProofReason.ABSENT_DECLARATION)
    return Proven(expected)


def _operation_ingress(operation: Operation, expected: Operation) -> Verdict[Operation]:
    binding = _operation_binding(expected)
    if not isinstance(binding, Proven):
        return binding
    if operation.registered_wire != expected.registered_wire:
        return Mismatch(ProofField.SCHEMA)
    return Proven(operation)


def _operation_binding(operation: Operation) -> Verdict[Operation]:
    if operation.registered_wire is None:
        return Missing(ProofReason.ABSENT_DECLARATION)
    if (
        operation.request.lifecycle == LifecycleClass.SESSION_TURN
        and operation.registered_turn is None
    ):
        return Missing(ProofReason.ABSENT_DECLARATION)
    try:
        if (
            not deeply_immutable(operation.request)
            or canonical_json(operation.request) != operation.registered_wire.payload_json
        ):
            return Mismatch(ProofField.PAYLOAD)
    except (TypeError, ValueError):
        return Mismatch(ProofField.PAYLOAD)
    if (
        operation.normalized_turn != operation.registered_turn
        or operation.normalized_measurement != operation.registered_measurement
        or operation.normalized_scope_reopen != operation.registered_scope_reopen
    ):
        return Mismatch(ProofField.NORMALIZATION)
    return Proven(operation)


def _operation_request(
    receipt: DecisionReceipt,
    operation: Operation,
    request: ExecuteRegisteredOperation,
    expected: RequestPrepared | ExecuteRegisteredOperation,
) -> Verdict[Operation]:
    if isinstance(expected, ExecuteRegisteredOperation):
        if request.request_id is None:
            return Missing(ProofReason.ABSENT_REQUEST)
        # The durable receipt includes deferred requests. Accepted.request_ids
        # records the immutable initial output, before later registration.
        if request.request_id not in receipt.request_ids:
            return Mismatch(ProofField.REQUEST_ID)
    fields = _identity_mismatch(
        (
            (
                ProofField.DECISION_ID,
                request.operation_id,
                OperationId(root=f"operation:{operation.decision_id.root}"),
            ),
            (ProofField.SCOPE, request.scope.owner, operation.scope.owner),
            (ProofField.GENERATION, request.scope.generation, operation.scope.generation),
            (ProofField.PAYLOAD, request.operation, operation.registered_wire),
            (
                ProofField.LIFECYCLE,
                expected.lifecycle
                if isinstance(expected, RequestPrepared)
                else request.operation.schema_ref.lifecycle,
                request.operation.schema_ref.lifecycle,
            ),
        )
    )
    if fields is not None:
        return fields
    if isinstance(expected, RequestPrepared) and (
        expected.normalized_turn != operation.normalized_turn
        or expected.normalized_measurement != operation.normalized_measurement
        or expected.normalized_scope_reopen != operation.normalized_scope_reopen
    ):
        return Mismatch(ProofField.NORMALIZATION)
    return Proven(operation)


def dependencies_for(request: RequestBase, records: RecordsState) -> Verdict[RequestBase]:
    """Prove optional dependencies; known denials outrank unfinished obligations.

    Inspect every declared obligation. Within concrete denials, declaration order
    is deterministic. An unfinished obligation cannot hide a later known failure.
    """
    proofs = tuple(_request_dependency(records.find(identity)) for identity in request.depends_on)
    failures = tuple(proof for proof in proofs if not isinstance(proof, Proven))
    if not failures:
        return Proven(request)
    denial = next(
        (
            proof
            for proof in failures
            if not (isinstance(proof, Missing) and proof.reason == ProofReason.UNRESOLVED)
        ),
        None,
    )
    return denial if denial is not None else Missing(ProofReason.UNRESOLVED)


def request_dependency_status(
    request: Request, receipts: tuple[DecisionReceipt, ...], records: RecordsState
) -> DependencyStatus:
    """Whether a prepared request may be dispatched: now, later, or never.

    ``FAILED`` is final: a decision that was not accepted, or a dependency that completed
    without success, never changes. ``PENDING`` still has a producer, the dependency itself.
    """
    if request.decision_id is not None and not isinstance(
        accepted_receipt_for(receipts, request.decision_id, None), Proven
    ):
        return DependencyStatus.FAILED
    proof = dependencies_for(request, records)
    if isinstance(proof, Proven):
        return DependencyStatus.SUCCEEDED
    if isinstance(proof, Missing) and proof.reason in (
        ProofReason.UNRESOLVED,
        ProofReason.ABSENT_REQUEST,
    ):
        return DependencyStatus.PENDING
    return DependencyStatus.FAILED


_NOT_YET_KNOWN = (ProofReason.UNRESOLVED, ProofReason.ABSENT_REQUEST, ProofReason.ABSENT_RECEIPT)


def dependency_failed(request: Request, records: RecordsState) -> bool:
    """Whether a decision or request this one depends on has ended without succeeding.

    Only a dependency that has concluded counts: one that is unfinished, or not yet recorded,
    may still succeed. A concluded failure is final, so the request can never be dispatched.
    """
    proofs = tuple(_request_dependency(records.find(identity)) for identity in request.depends_on)
    return any(
        (isinstance(proof, Mismatch) and proof.field == ProofField.STATUS)
        or (isinstance(proof, Missing) and proof.reason not in _NOT_YET_KNOWN)
        for proof in proofs
    )


def _request_dependency(rows: tuple[RequestRecord, ...]) -> Verdict[RequestRecord]:
    if not rows:
        return Missing(ProofReason.ABSENT_REQUEST)
    if len(rows) != 1:
        return Mismatch(ProofField.REQUEST_ID)
    row = rows[0]
    proof = request_matches(row, row.request)
    if not isinstance(proof, Proven):
        return proof
    # An observation is required only for the completed lifecycle stage.
    # Prepared and dispatched requests remain unresolved while outcomes arrive.
    if row.phase != RequestPhase.COMPLETED:
        return Missing(ProofReason.UNRESOLVED)
    observation = observation_for(row, row.observation)
    if not isinstance(observation, Proven):
        return observation
    return (
        Proven(row)
        if observation.value.accepted
        and observation.value.terminal
        and observation.value.status == ObservationStatus.SUCCEEDED
        else Mismatch(ProofField.STATUS)
    )


def admission_remaining(
    attempts: Iterable[AttemptView], max_attempts: int, reserved: int = 0
) -> int:
    """Admission budget still available: the limit minus charged-net-of-refunds.

    The one definition of admission capacity, clamped at zero. Scheduling
    decides what fits and Attempts decides what may register by this same
    number, so a start that fits only after a refund is accepted by both or
    by neither. ``reserved`` is budget held by queued, unregistered starts.
    """
    consumed = sum(
        charge.charged - charge.refunded
        for owner in attempts
        for charge in owner.charges
        if charge.kind == ChargeKind.ADMISSION
    )
    return max(0, max_attempts - consumed - reserved)


def continues_admitted_work(run: RunState) -> bool:
    """Whether the run carries on with work it already admitted: it is running or paused.

    The one definition of what a pause holds. A pause holds intake only: core takes no
    decision but a Finish or a Withdraw (``takes_command``) and admits no queued start
    (scheduling). Work already under way finishes, and what it yields is delivered: a turn's
    evaluation call and its wait, a job's end, the resume of a wait whose jobs ended. The
    turn that resume asks for is a decision the shell holds until the run is resumed. A gate
    that refused these while paused would drop them for good, because a resume re-sends
    only held decisions and nothing else re-delivers a fact that came while paused.

    A run that is draining an operator's pause (``RunStatus.DRAINING``) is not in this set: a
    gate that reads it admits that run's work only through its own narrower rule (a permit, a
    scope in flight). ``admitted_work_proceeds`` is the broader question the session gates ask.
    """
    return run.status in (RunStatus.RUNNING, RunStatus.PAUSED)


def admitted_work_proceeds(run: RunState) -> bool:
    """Whether work the run already admitted goes on: it runs, is paused for quota, or drains a pause.

    The one answer for the session gates (a scope's ownership, a continuation's liveness). The
    drain of an operator's pause (``RunStatus.DRAINING``) lets admitted work finish: a turn that
    was requested before the pause still gets its input reserved, and a turn the pause admitted
    as a successor is still admitted. A gate that refused them would end the turn for good, since
    nothing re-requests it. What starts is the drain's decision, not this predicate's:
    ``dispatch_allowed`` holds an effect back, and ``takes_command`` holds a new decision.

    Not ``stop_draining``, which is the drain of a stop (``RunStatus.CLOSING``).
    """
    return continues_admitted_work(run) or run.status is RunStatus.DRAINING


def stop_draining(run: RunState) -> bool:
    """Whether the run is CLOSING under a proved Finish and its stop is a drain.

    The one definition of the drain rule of a stop: it starts no new requests, but
    the already accepted queue still starts and runs to completion (legacy
    FinishSearch). A cancel starts nothing. Scheduling admits under this
    rule and Attempts accepts those admissions under the same predicate. It is not the
    drain of an operator's pause (``RunStatus.DRAINING``), which ``admitted_work_proceeds`` admits.
    """
    match committed_stop(run):
        case Proven():
            return run.status == RunStatus.CLOSING and stop_mode(run) is StopMode.DRAIN
        case _:
            return False


def requested_stop_mode(run: RunState) -> StopMode | None:
    """The strongest mode the run's standing stop asks for; None before any stop or after a lift."""
    held: StopMode | None = None
    for control in run.controls:
        if control.action == "stop" and control.mode is not None:
            held = escalate_stop(held, control.mode)
        elif control.action == "resume":
            held = None  # a resume lifts the stop (`_run_pause.stop_standing`)
    return held


def stop_mode(run: RunState) -> StopMode | None:
    """How the run's stop treats work in flight; None while nothing stops the run.

    The one definition of the run's stop mode: the strongest of its committed Finish's
    mode and every stop control it recorded (``escalate_stop``). A stop only escalates,
    so a cancel recorded after a drain Finish committed turns the drain into a cancel,
    and a later drain never weakens a cancel. The first committed Finish keeps the run's
    result (``committed_stop``); only the mode escalates.
    """
    held = requested_stop_mode(run)
    match committed_stop(run):
        case Proven(value=stop):
            return escalate_stop(held, StopMode(stop.mode))
        case _:
            return held


def committed_stop(run: RunState) -> Verdict[Finish]:
    """Select the first committed Finish, preserving that command's dependencies.

    A Finish stays committed after dependency failure rewrites its feedback to
    Rejected: completion is only recorded for an accepted decision. A failed
    first commitment denies finality; a later Finish never replaces it.
    """
    receipt = next(
        (
            row
            for row in run.receipts
            if isinstance(row.decision, Finish)
            and (isinstance(row.feedback, Accepted) or row.completion is not None)
        ),
        None,
    )
    if receipt is None:
        return Missing(ProofReason.ABSENT_RECEIPT)
    if not isinstance(receipt.feedback, Accepted):
        return Mismatch(ProofField.DISPOSITION)
    proof = accepted_receipt_for(run.receipts, receipt.decision_id, None)
    if not isinstance(proof, Proven):
        return proof
    stop = receipt.decision
    if not isinstance(stop, Finish):
        return Mismatch(ProofField.PAYLOAD)
    if run.result is None:
        return Missing(ProofReason.UNRESOLVED)
    fields = _identity_mismatch(
        (
            (ProofField.SCOPE, stop.scope.owner, run.run_id),
            (ProofField.GENERATION, stop.scope.generation, run.generation),
            (ProofField.STATUS, run.status in (RunStatus.CLOSING, RunStatus.TERMINAL), True),
            (ProofField.DISPOSITION, stop.result, run.result),
        )
    )
    return fields if fields is not None else Proven(stop)


def resolved_observation(observation: Observation | None) -> Verdict[Observation]:
    """Prove release semantics only; source and owner correlation remain required."""
    if observation is None:
        return Missing(ProofReason.ABSENT_OBSERVATION)
    if (
        not observation.terminal
        or not observation.released
        or observation.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    ):
        return Missing(ProofReason.UNRESOLVED)
    if not observation.children_complete:
        return Missing(ProofReason.INCOMPLETE_MANIFEST)
    return Proven(observation)


def _release_source(observation: Observation, source: RequestRecord | None) -> Verdict[Observation]:
    """Recorded requests, rather than current owners, identify cleanup episodes.

    Permanent rule: the Records ledger is the only release authority, so an owner
    whose canonical request is absent is denied. There is no separate certified
    release source; a migrated owner must be given a canonical record instead.
    """
    if source is None:
        # A copied request id in a lease or watermark cannot certify the absent
        # payload, digest and lifecycle, so every owner needs its canonical request.
        return Missing(ProofReason.ABSENT_REQUEST)
    if source.request.request_id is None:
        return Missing(ProofReason.ABSENT_REQUEST)
    if isinstance(source.request.scope.owner, AttemptId) and (
        source.request.admission_id is None or observation.admission_id is None
    ):
        return Missing(ProofReason.ABSENT_EPISODE)
    mismatch = _identity_mismatch(
        (
            (ProofField.REQUEST_ID, source.request.request_id, source.request_id),
            (ProofField.REQUEST_ID, observation.request_id, source.request_id),
            (ProofField.SCOPE, observation.scope.owner, source.request.scope.owner),
            (ProofField.GENERATION, observation.scope.generation, source.request.scope.generation),
            (ProofField.ADMISSION_ID, observation.admission_id, source.request.admission_id),
        )
    )
    if mismatch is not None:
        return mismatch
    canonical = request_matches(source, source.request)
    return Proven(observation) if isinstance(canonical, Proven) else canonical


def _release_nonownership(observation: Observation) -> bool:
    return not observation.accepted and observation.status in (
        ObservationStatus.REJECTED,
        ObservationStatus.FAILED,
        ObservationStatus.CANCELLED,
    )


def _released_record(owner: RequestRecord) -> Verdict[Observation]:
    observation = owner.observation
    if observation is None:
        return Missing(ProofReason.ABSENT_OBSERVATION)
    if (
        owner.lifecycle in (LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN)
        and observation.resource_id is None
        and not _release_nonownership(observation)
    ):
        return Missing(ProofReason.ABSENT_RESOURCE)
    source = _release_source(observation, owner)
    if not isinstance(source, Proven):
        return source
    return resolved_observation(observation)


def _released_job(
    owner: OwnedJob | RegisteredOwnedJob, sources: tuple[RequestRecord, ...]
) -> Verdict[Observation]:
    observation = owner.observation
    if observation is None:
        return Missing(ProofReason.ABSENT_OBSERVATION)
    if owner.resource_id is None and not _release_nonownership(observation):
        return Missing(ProofReason.ABSENT_RESOURCE)
    request_id = owner.request_id if isinstance(owner, RegisteredOwnedJob) else owner.submission_id
    matches = tuple(source for source in sources if source.request_id == request_id)
    if len(matches) > 1:
        return Mismatch(ProofField.REQUEST_ID)
    source = _release_source(observation, matches[0] if matches else None)
    if not isinstance(source, Proven):
        return source
    binding = _job_release_binding(owner, observation, matches[0] if matches else None)
    return resolved_observation(observation) if isinstance(binding, Proven) else binding


def _job_release_binding(
    owner: OwnedJob | RegisteredOwnedJob, observation: Observation, source: RequestRecord | None
) -> Verdict[Observation]:
    request_id = owner.request_id if isinstance(owner, RegisteredOwnedJob) else owner.submission_id
    mismatch = _identity_mismatch(
        (
            (ProofField.REQUEST_ID, observation.request_id, request_id),
            (ProofField.RESOURCE_ID, observation.resource_id, owner.resource_id),
            (ProofField.SCOPE, observation.scope.owner, owner.scope.owner),
            (ProofField.GENERATION, observation.scope.generation, owner.scope.generation),
        )
    )
    if mismatch is not None:
        return mismatch
    if source is not None:
        request = source.request
        if isinstance(owner, OwnedJob) and (
            not isinstance(request, SubmitMeasurement) or request.plan != owner.plan
        ):
            return Mismatch(ProofField.PAYLOAD)
        if isinstance(owner, RegisteredOwnedJob) and (
            not isinstance(request, ExecuteRegisteredOperation)
            or request.operation_id != owner.operation_id
        ):
            return Mismatch(ProofField.PAYLOAD)
    if observation.status != owner.status:
        return Mismatch(ProofField.STATUS)
    if not owner.terminal or not owner.released:
        return Missing(ProofReason.UNRESOLVED)
    return Proven(observation)


def _released_session(
    owner: SessionView, sources: tuple[RequestRecord, ...]
) -> Verdict[Observation]:
    matches = tuple(
        source
        for source in sources
        if isinstance(source.request, CloseSession)
        and source.request.session_id == owner.spec.session_id
        and source.request.scope == owner.scope
    )
    if not matches:
        return Missing(ProofReason.ABSENT_REQUEST)
    if len(matches) != 1:
        return Mismatch(ProofField.REQUEST_ID)
    proof = _released_record(matches[0])
    if not isinstance(proof, Proven):
        return proof
    return _session_release_binding(owner, proof.value)


def _session_release_binding(owner: SessionView, observation: Observation) -> Verdict[Observation]:
    # A negative CloseSession outcome does not prove nonacceptance of the
    # original acquisition. Without its lease, the session remains unresolved.
    if owner.resource_id is None:
        return Missing(ProofReason.ABSENT_RESOURCE)
    mismatch = _identity_mismatch(
        (
            (ProofField.RESOURCE_ID, observation.resource_id, owner.resource_id),
            (ProofField.GENERATION, owner.generation, owner.scope.generation),
        )
    )
    if mismatch is not None:
        return mismatch
    if owner.phase != SessionPhase.TERMINAL:
        return Missing(ProofReason.UNRESOLVED)
    return Proven(observation)


def blocked_unnamed(record: RequestRecord) -> bool:
    """A blocked record that was never accepted and named nothing owns nothing to release.

    Reconciliation blocked it after its executor answered without a conclusion. With the
    launch unaccepted and no resource or children in that answer there is no identity left
    to cancel or wait on, so the block ends the record's claim on the run's closure; the strategy has
    already been told (``RequestBlocked``). A blocked record that named a resource still
    holds closure until that resource is proven released.
    """
    observation = record.observation
    return (
        record.phase == RequestPhase.BLOCKED
        and observation is not None
        and not observation.accepted
        and observation.resource_id is None
        and not observation.children
    )


def released_owner(
    owner: RequestRecord | OwnedJob | RegisteredOwnedJob | SessionView | None,
    sources: tuple[RequestRecord, ...],
) -> Verdict[Observation]:
    """Prove physical release in the source's recorded episode, preserving child debt."""
    if owner is None:
        return Missing(ProofReason.ABSENT_REQUEST)
    if isinstance(owner, RequestRecord):
        return _released_record(owner)
    if isinstance(owner, (OwnedJob, RegisteredOwnedJob)):
        return _released_job(owner, sources)
    return _released_session(owner, sources)


def _submission_identity(
    request: SubmitMeasurement | ExecuteRegisteredOperation,
    receipts: tuple[DecisionReceipt, ...],
) -> Verdict[MeasurementIdentity]:
    if isinstance(request, ExecuteRegisteredOperation):
        operation = operation_for(receipts, request)
        if not isinstance(operation, Proven):
            return operation
        identity = operation.value.registered_measurement
        return Proven(identity) if identity is not None else Missing(ProofReason.ABSENT_DECLARATION)
    plan = request.plan
    if not isinstance(plan.candidate, RevisionRef):
        return Missing(ProofReason.ABSENT_CHECKPOINT)
    try:
        return Proven(MeasurementIdentity.from_plan(plan, plan.candidate))
    except (TypeError, ValueError):
        return Mismatch(ProofField.PAYLOAD)


def submission_budget_for(
    request: SubmitMeasurement | ExecuteRegisteredOperation | None,
    budgets: tuple[SubmissionBudget, ...],
    receipts: tuple[DecisionReceipt, ...],
) -> Verdict[SubmissionBudget]:
    """A budget proves ownership only for the owning canonical measurement."""
    if request is None or request.request_id is None:
        return Missing(ProofReason.ABSENT_REQUEST)
    identity = _submission_identity(request, receipts)
    if not isinstance(identity, Proven):
        return identity
    matches = tuple(
        budget
        for budget in budgets
        if any(
            isinstance(receipt, PreparedSubmissionReceipt)
            and receipt.request_id == request.request_id
            for receipt in budget.receipts
        )
    )
    if not matches:
        return Missing(ProofReason.ABSENT_RECEIPT)
    if len(matches) != 1:
        return Mismatch(ProofField.REQUEST_ID)
    budget = matches[0]
    mismatch = _identity_mismatch(
        (
            (ProofField.SCOPE, budget.scope.owner, request.scope.owner),
            (ProofField.GENERATION, budget.scope.generation, request.scope.generation),
            (ProofField.NORMALIZATION, budget.identity, identity.value),
        )
    )
    return mismatch if mismatch is not None else Proven(budget)


def occupied_episode(slots: tuple[Slot, ...], request: AdmissionRequest) -> Verdict[Slot]:
    """Prove one exact capacity episode, never authorize queued registration."""
    target = (
        request.attempt
        if isinstance(request, AttemptReopenRequest)
        else AttemptRef(attempt_id=request.attempt_id, generation=request.generation)
    )
    candidates = tuple(
        slot
        for slot in slots
        if slot.attempt.attempt_id == target.attempt_id or slot.admission_id == request.decision_id
    )
    if not candidates:
        return Missing(ProofReason.ABSENT_RESOURCE)
    if len(candidates) != 1:
        return Mismatch(ProofField.MANIFEST)
    slot = candidates[0]
    mismatch = _identity_mismatch(
        (
            (ProofField.SCOPE, slot.attempt.attempt_id, target.attempt_id),
            (ProofField.GENERATION, slot.attempt.generation, target.generation),
            (ProofField.ADMISSION_ID, slot.admission_id, request.decision_id),
        )
    )
    if mismatch is not None:
        return mismatch
    if (
        slot.pools != request.pools
        or len(set(slot.pools)) != len(slot.pools)
        or len(set(request.pools)) != len(request.pools)
    ):
        return Mismatch(ProofField.MANIFEST)
    return Proven(slot)
