"""Registered outcome proofs must match the canonical request's descriptor."""

from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel

import vs_core.api as core


class ReadOutcome(core.Value):
    """Field-free owning-library response keeps this regression schema focused."""


class ReadRequest(core.OperationRequest):
    kind: Literal["test.owner.read"] = "test.owner.read"
    lifecycle: Literal[core.LifecycleClass.QUERY] = core.LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = ReadOutcome


def registry(descriptor: core.OperationDescriptor) -> core.OperationRegistry:
    return core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=ReadRequest, outcome_model=ReadOutcome
            ),
        )
    )


@pytest.mark.parametrize("schema_field", ["request_schema", "outcome_schema"])
def test_registered_root_outcome_rejects_another_owner_descriptor(schema_field: str) -> None:
    descriptor = core.OperationDescriptor(
        kind="test.owner.read",
        lifecycle=core.LifecycleClass.QUERY,
        request_schema=core.SchemaRef(name="read", version=1),
        outcome_schema=core.SchemaRef(name="read-result", version=1),
    )
    owner = registry(descriptor)
    foreign = registry(
        descriptor.model_copy(update={schema_field: core.SchemaRef(name="foreign", version=1)})
    )
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    request_id = core.RequestId(root="read")
    request = core.ExecuteRegisteredOperation(
        request_id=request_id,
        scope=scope,
        deadline_at=100.0,
        operation_id=core.OperationId(root="read"),
        operation=owner.encode(ReadRequest()),
        retry_limit=0,
    )
    intent = core.Intent(
        request_id=request_id,
        request=request,
        payload_digest="read-payload",
        lifecycle=core.LifecycleClass.QUERY,
        phase=core.IntentPhase.DISPATCHED,
        reconcile_deadline_at=100.0,
    )
    state = state.model_copy(
        update={
            "registry": owner.descriptors,
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
    event = foreign.validate_event(
        core.RequestObserved(
            observation=core.Observation(
                event_id=core.EventId(root="read-result"),
                request_id=request_id,
                scope=scope,
                sequence=1,
                observed_at=1.0,
                status=core.ObservationStatus.SUCCEEDED,
            ),
            operation_schema=foreign.encode(ReadRequest()).schema_ref,
            outcome=ReadOutcome(),
        )
    )
    assert event.outcome_is_registered
    with pytest.raises(core.ContractError, match="canonical operation descriptor"):
        core.step(state, event)
