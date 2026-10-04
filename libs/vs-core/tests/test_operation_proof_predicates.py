"""Generated operation declarations, accepted origins and invocation facts."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    Proven,
    descriptor_matches,
    invocation_for,
    operation_for,
)

from .test_recovery_proof_regressions import _digest, _registered_turn


@st.composite
def operation_facts(draw: st.DrawFn) -> tuple[core.CoreState, core.Intent]:
    return _registered_turn(draw(st.text(alphabet="abc123", min_size=1, max_size=12)))


@pytest.mark.parametrize(
    "field",
    [
        "exact",
        "absent_request",
        "absent_receipt",
        "absent_decision",
        "duplicate",
        "rejected",
        "feedback_id",
        "decision_id",
        "digest",
        "membership",
        "feedback_membership",
        "operation_id",
        "scope",
        "generation",
        "payload",
        "deadline",
        "normalization",
        "codec",
    ],
)
@given(facts=operation_facts())
def test_operation_proof_checks_every_origin_field(
    field: str,
    facts: tuple[core.CoreState, core.Intent],
) -> None:
    state, record = facts
    request = record.request
    assert isinstance(request, core.ExecuteRegisteredOperation)
    receipt = state.run.receipts[0]
    decision = receipt.decision
    assert isinstance(decision, core.Operation)
    expected_verdict = Proven(decision)
    receipts = (receipt,)
    expected = request
    if field == "absent_request":
        expected = None
        expected_verdict = Missing(ProofReason.ABSENT_REQUEST)
    elif field == "absent_receipt":
        receipts = ()
        expected_verdict = Missing(ProofReason.ABSENT_RECEIPT)
    elif field == "absent_decision":
        receipt = receipt.model_copy(update={"decision": None})
        expected_verdict = Missing(ProofReason.ABSENT_RECEIPT)
    elif field == "duplicate":
        receipts = (receipt, receipt)
        expected_verdict = Mismatch(ProofField.RECEIPT_ID)
    elif field in (
        "rejected",
        "feedback_id",
        "decision_id",
        "digest",
        "membership",
        "feedback_membership",
    ):
        receipt, expected_verdict = _wrong_receipt(receipt, decision, field)
    elif field in ("operation_id", "scope", "generation", "payload", "deadline"):
        expected, expected_verdict = _wrong_request(request, field)
    elif field in ("normalization", "codec"):
        decision = (
            decision.model_copy(update={"normalized_turn": None})
            if field == "normalization"
            else core.Operation(
                decision_id=decision.decision_id,
                scope=decision.scope,
                request=decision.request,
                deadline_at=decision.deadline_at,
            )
        )
        receipt = receipt.model_copy(
            update={"decision": decision, "payload_digest": _digest(decision)}
        )
        expected_verdict = (
            Mismatch(ProofField.NORMALIZATION)
            if field == "normalization"
            else Missing(ProofReason.ABSENT_DECLARATION)
        )
    if field not in ("absent_receipt", "duplicate"):
        receipts = (receipt,)
    assert operation_for(receipts, expected) == expected_verdict


def _wrong_receipt(
    receipt: core.DecisionReceipt,
    decision: core.Operation,
    field: str,
) -> tuple[core.DecisionReceipt, Missing | Mismatch]:
    if field == "rejected":
        receipt = receipt.model_copy(
            update={
                "feedback": core.Rejected(
                    decision_id=receipt.decision_id,
                    code=core.RejectionCode.CAPABILITY,
                    path=(),
                    detail="denied",
                )
            }
        )
        expected_verdict = Missing(ProofReason.NOT_ACCEPTED)
    elif field == "feedback_id":
        receipt = receipt.model_copy(
            update={
                "feedback": receipt.feedback.model_copy(
                    update={
                        "decision_id": core.DecisionId(root="foreign"),
                    }
                )
            }
        )
        expected_verdict = Mismatch(ProofField.FEEDBACK_ID)
    elif field == "decision_id":
        decision = decision.model_copy(update={"decision_id": core.DecisionId(root="foreign")})
        receipt = receipt.model_copy(update={"decision": decision})
        expected_verdict = Mismatch(ProofField.DECISION_ID)
    elif field == "digest":
        receipt = receipt.model_copy(update={"payload_digest": "foreign"})
        expected_verdict = Mismatch(ProofField.DIGEST)
    elif field in ("membership", "feedback_membership"):
        if field == "membership":
            receipt = receipt.model_copy(update={"request_ids": ()})
        else:
            receipt = receipt.model_copy(
                update={"feedback": receipt.feedback.model_copy(update={"request_ids": ()})}
            )
        expected_verdict = Mismatch(ProofField.REQUEST_ID)
    return receipt, expected_verdict


def _wrong_request(
    request: core.ExecuteRegisteredOperation,
    field: str,
) -> tuple[core.ExecuteRegisteredOperation, Mismatch]:
    fields = {
        "operation_id": ("operation_id", core.OperationId(root="foreign"), ProofField.DECISION_ID),
        "scope": (
            "scope",
            request.scope.model_copy(update={"owner": core.AttemptId(root="foreign")}),
            ProofField.SCOPE,
        ),
        "generation": (
            "scope",
            request.scope.model_copy(update={"generation": request.scope.generation + 1}),
            ProofField.GENERATION,
        ),
        "payload": (
            "operation",
            request.operation.model_copy(update={"payload_json": "{}"}),
            ProofField.PAYLOAD,
        ),
        "deadline": ("deadline_at", 99.0, ProofField.PAYLOAD),
    }
    name, wrong, verdict_field = fields[field]
    return request.model_copy(update={name: wrong}), Mismatch(verdict_field)


@st.composite
def descriptor_facts(draw: st.DrawFn) -> core.OperationDescriptor:
    lifecycle = draw(st.sampled_from(list(core.LifecycleClass)))
    return core.OperationDescriptor(
        kind=draw(st.text(alphabet="abc123", min_size=1, max_size=12)),
        request_schema=core.SchemaRef(
            name="request", version=draw(st.integers(min_value=1, max_value=10))
        ),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
        lifecycle=lifecycle,
        resource_pool=core.PoolId(root="pool")
        if lifecycle == core.LifecycleClass.OWNED_JOB
        else None,
    )


@pytest.mark.parametrize(
    "field",
    [
        "exact",
        "absent_wire",
        "absent_registry",
        "absent_offer",
        "duplicate_registry",
        "duplicate_offer",
        "request_schema",
        "outcome_schema",
        "lifecycle",
        "normalization",
        "inspect",
        "cancel",
        "watch",
    ],
)
@given(descriptor=descriptor_facts())
def test_descriptor_proof_requires_durable_and_offered_exact_declarations(
    field: str,
    descriptor: core.OperationDescriptor,
) -> None:
    if field == "normalization":
        descriptor = core.OperationDescriptor(
            kind=descriptor.kind,
            request_schema=descriptor.request_schema,
            outcome_schema=descriptor.outcome_schema,
            lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
            inspect=True,
        )
    wire = core.OperationWire(
        schema_ref=core.OperationSchemaRef(
            kind=descriptor.kind,
            lifecycle=descriptor.lifecycle,
            request_schema=descriptor.request_schema,
            outcome_schema=descriptor.outcome_schema,
        ),
        payload_json="{}",
    )
    registry = (descriptor,)
    offers = registry
    expected = Proven(descriptor)
    if field == "absent_wire":
        wire = None
        expected = Missing(ProofReason.ABSENT_DECLARATION)
    elif field in ("absent_registry", "absent_offer"):
        registry = () if field == "absent_registry" else registry
        offers = () if field == "absent_offer" else offers
        expected = Missing(ProofReason.ABSENT_DECLARATION)
    elif field in ("duplicate_registry", "duplicate_offer"):
        registry = (descriptor, descriptor) if field == "duplicate_registry" else registry
        offers = (descriptor, descriptor) if field == "duplicate_offer" else offers
        expected = Mismatch(ProofField.SCHEMA)
    elif field != "exact":
        wrong = _wrong_descriptor(descriptor, field)
        registry = (wrong,)
        expected = Mismatch(
            ProofField.RESOURCE_ID
            if field == "lifecycle" and descriptor.resource_pool is not None
            else ProofField.LIFECYCLE
            if field == "lifecycle"
            else ProofField.NORMALIZATION
            if field == "normalization"
            else ProofField.SCHEMA
        )
    assert (
        descriptor_matches(
            registry,
            core.Capabilities(operations=offers),
            wire,
            descriptor.lifecycle,
            descriptor.normalization,
        )
        == expected
    )


def _wrong_descriptor(descriptor: core.OperationDescriptor, field: str) -> core.OperationDescriptor:
    if field in ("request_schema", "outcome_schema"):
        return descriptor.model_copy(update={field: core.SchemaRef(name="foreign", version=2)})
    if field == "lifecycle":
        lifecycle = (
            core.LifecycleClass.QUERY
            if descriptor.lifecycle != core.LifecycleClass.QUERY
            else core.LifecycleClass.IDEMPOTENT_WRITE
        )
        return core.OperationDescriptor(
            kind=descriptor.kind,
            request_schema=descriptor.request_schema,
            outcome_schema=descriptor.outcome_schema,
            lifecycle=lifecycle,
        )
    if field == "normalization":
        # Reopen descriptors are schema-valid inspectable idempotent writes.
        return descriptor.model_copy(
            update={"normalization": core.OperationNormalizationKind.SCOPE_REOPEN}
        )
    return descriptor.model_copy(update={field: True})


@st.composite
def invocation_facts(draw: st.DrawFn) -> core.Invocation:
    state, _ = _registered_turn()
    generation = draw(st.integers(min_value=0, max_value=20))
    row = state.sessions.invocations[0]
    scope = row.scope.model_copy(update={"generation": generation})
    turn = row.turn.model_copy(update={"workspace": scope})
    return row.model_copy(
        update={
            "scope": scope,
            "turn": turn,
            "invocation": row.invocation.model_copy(update={"generation": generation}),
        }
    )


@pytest.mark.parametrize(
    "field",
    [
        "exact",
        "absent",
        "duplicate",
        "turn_id",
        "session_id",
        "turn_session",
        "scope",
        "generation",
        "invocation_generation",
        "payload",
    ],
)
@given(row=invocation_facts())
def test_invocation_proof_checks_every_identity_and_complete_turn(
    field: str, row: core.Invocation
) -> None:
    canonical = row.turn
    scope = row.scope
    rows = (row,)
    expected = Proven(row)
    if field == "absent":
        rows = ()
        expected = Missing(ProofReason.ABSENT_INVOCATION)
    elif field == "duplicate":
        rows = (row, row)
        expected = Mismatch(ProofField.INVOCATION_ID)
    elif field != "exact":
        row, verdict_field = _wrong_invocation(row, field)
        rows = (row,)
        expected = Mismatch(verdict_field)
    assert invocation_for(rows, canonical, scope) == expected


def _wrong_invocation(row: core.Invocation, field: str) -> tuple[core.Invocation, ProofField]:
    if field == "turn_id":
        return row.model_copy(
            update={
                "turn": row.turn.model_copy(
                    update={"invocation_id": core.InvocationId(root="foreign")}
                )
            }
        ), ProofField.INVOCATION_ID
    if field == "session_id":
        return row.model_copy(
            update={
                "invocation": row.invocation.model_copy(
                    update={"session_id": core.SessionId(root="foreign")}
                )
            }
        ), ProofField.SESSION_ID
    if field == "turn_session":
        return row.model_copy(
            update={
                "turn": row.turn.model_copy(
                    update={
                        "session": row.turn.session.model_copy(
                            update={"session_id": core.SessionId(root="foreign")}
                        )
                    }
                )
            }
        ), ProofField.SESSION_ID
    if field in ("scope", "generation"):
        scope = row.scope.model_copy(
            update={"owner": core.AttemptId(root="foreign")}
            if field == "scope"
            else {"generation": row.scope.generation + 1}
        )
        return row.model_copy(
            update={"scope": scope}
        ), ProofField.SCOPE if field == "scope" else ProofField.GENERATION
    if field == "invocation_generation":
        return row.model_copy(
            update={
                "invocation": row.invocation.model_copy(
                    update={"generation": row.invocation.generation + 1}
                )
            }
        ), ProofField.GENERATION
    return row.model_copy(
        update={"turn": row.turn.model_copy(update={"deadline_at": 99.0})}
    ), ProofField.PAYLOAD


@pytest.mark.parametrize(
    "field", ["exact", "proposal_lifecycle", "proposal_normalization", "absent_ingress_codec"]
)
@given(facts=operation_facts())
def test_proposals_and_ingress_do_not_substitute_origin_or_normalization(
    field: str,
    facts: tuple[core.CoreState, core.Intent],
) -> None:
    state, record = facts
    receipt = state.run.receipts[0]
    assert isinstance(receipt.decision, core.Operation)
    if field == "absent_ingress_codec":
        expected = core.Operation(
            decision_id=receipt.decision.decision_id,
            scope=receipt.decision.scope,
            request=receipt.decision.request,
            deadline_at=receipt.decision.deadline_at,
            normalized_turn=state.sessions.invocations[0].turn,
        )
        verdict = Missing(ProofReason.ABSENT_DECLARATION)
    else:
        expected = core.RequestPrepared(
            request=record.request,
            lifecycle=core.LifecycleClass.SESSION_TURN,
            normalized_turn=state.sessions.invocations[0].turn,
        )
        verdict = Proven(receipt.decision)
        if field == "proposal_lifecycle":
            expected = expected.model_copy(
                update={"lifecycle": core.LifecycleClass.IDEMPOTENT_WRITE}
            )
            verdict = Mismatch(ProofField.LIFECYCLE)
        elif field == "proposal_normalization":
            expected = expected.model_copy(update={"normalized_turn": None})
            verdict = Mismatch(ProofField.NORMALIZATION)
    assert operation_for((receipt,), expected) == verdict


@pytest.mark.parametrize("field", ["resource_pool", "revision_authority"])
@given(suffix=st.text(alphabet="abc123", min_size=1, max_size=12))
def test_descriptor_resource_and_revision_authority_are_exact(field: str, suffix: str) -> None:
    lifecycle = (
        core.LifecycleClass.OWNED_JOB
        if field == "resource_pool"
        else core.LifecycleClass.IDEMPOTENT_WRITE
    )
    descriptor = core.OperationDescriptor(
        kind=suffix,
        lifecycle=lifecycle,
        request_schema=core.SchemaRef(name="request", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
        resource_pool=core.PoolId(root="exact") if field == "resource_pool" else None,
        revision_authority=core.RevisionAuthority.SNAPSHOT
        if field == "revision_authority"
        else core.RevisionAuthority.NONE,
    )
    wire = core.OperationWire(
        schema_ref=core.OperationSchemaRef(
            kind=descriptor.kind,
            lifecycle=lifecycle,
            request_schema=descriptor.request_schema,
            outcome_schema=descriptor.outcome_schema,
        ),
        payload_json="{}",
    )
    wrong = (
        core.PoolId(root="foreign") if field == "resource_pool" else core.RevisionAuthority.RETAIN
    )
    offered = descriptor.model_copy(update={field: wrong})
    assert descriptor_matches(
        (descriptor,),
        core.Capabilities(operations=(offered,)),
        wire,
        lifecycle,
        descriptor.normalization,
    ) == Mismatch(
        ProofField.RESOURCE_ID if field == "resource_pool" else ProofField.REVISION,
    )
