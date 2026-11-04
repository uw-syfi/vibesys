"""Recovery proofs and replay through the public immutable lifecycle API."""

import json
from hashlib import sha256

from vs_core.api import (
    Access,
    DispatchTurn,
    EnsureSession,
    InvocationId,
    LifecycleClass,
    OperationDescriptor,
    OperationSchemaRef,
    RequestId,
    RequestPhase,
    RequestRecord,
    RoleId,
    SchemaRef,
    Scope,
    SessionId,
    SessionSpec,
    TurnSpec,
    Until,
    Value,
    initial_state,
)

WRITE_SCHEMA = OperationSchemaRef(
    kind="test.write",
    request_schema=SchemaRef(name="write", version=1),
    outcome_schema=SchemaRef(name="write-result", version=1),
    lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
)
WRITE_DESCRIPTOR = OperationDescriptor(**WRITE_SCHEMA.model_dump(), inspect=True, cancel=True)


def pending_record(
    identity: str = "original", phase: RequestPhase = RequestPhase.DISPATCHED
) -> RequestRecord:
    """A persisted operation whose external acceptance has no positive proof."""
    state = initial_state()
    request_id = RequestId(root=identity)
    request = EnsureSession(
        request_id=request_id,
        scope=Scope(owner=state.run.run_id, generation=0),
        spec=SessionSpec(
            session_id=SessionId(root=identity),
            role_id=RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
    )
    return RequestRecord(
        request_id=request_id,
        request=request,
        payload_digest=fixture_digest(request),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=phase,
        until=Until(at=100.0),
        addressed_by=(),
    )


def fixture_digest(value: Value) -> str:
    """Canonical bytes for scalar and ordered fixture contracts."""
    return sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def turn_record() -> RequestRecord:
    """An honest session-turn request with a durable invocation identity."""
    original = pending_record()
    assert isinstance(original.request, EnsureSession)
    turn = TurnSpec(
        session=original.request.spec,
        invocation_id=InvocationId(root="turn"),
        workspace=original.request.scope,
        prompts=(),
        output_schema=SchemaRef(name="turn-output", version=1),
        charge_class="paid",
    )
    record = original.model_copy(
        update={
            "request": DispatchTurn(
                request_id=original.request_id,
                scope=original.request.scope,
                turn=turn,
            ),
            "lifecycle": LifecycleClass.SESSION_TURN,
        }
    )

    return record.model_copy(update={"payload_digest": fixture_digest(record.request)})


"""The longest a live host goes between proofs that it is alive (lease renewals or commits)."""
