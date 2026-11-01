"""Recovery commands retain canonical target identity across persistence replay."""

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .restart import unaddressed


def state_with_root() -> tuple[core.CoreState, core.RequestRecord]:
    """An accepted live operation with no typed owner remains cleanup authority.

    Only a registered operation's resource is cancelled by ``CancelOwnedResource``: every
    other resource is released by the area that owns it.
    """
    state = core.initial_state()
    identity = core.RequestId(root="root")
    scope = core.Scope(owner=state.run.run_id, generation=0)
    request = core.ExecuteRegisteredOperation(
        request_id=identity,
        scope=scope,
        operation_id=core.OperationId(root="operation"),
        operation=core.OperationWire(
            schema_ref=core.OperationSchemaRef(
                kind="test.write",
                request_schema=core.SchemaRef(name="write", version=1),
                outcome_schema=core.SchemaRef(name="write-result", version=1),
                lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
            ),
            payload_json="{}",
        ),
        retry_limit=0,
    )
    root = core.RequestRecord(
        request_id=identity,
        request=request,
        payload_digest="root",
        phase=core.RequestPhase.DISPATCHED,
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        until=core.Until(at=100.0),
        observation=core.Observation(
            event_id=core.EventId(root="live-root"),
            request_id=identity,
            scope=scope,
            sequence=1,
            observed_at=10.0,
            status=core.ObservationStatus.PENDING,
            accepted=True,
            resource_id=core.ResourceId(root="physical"),
        ),
        addressed_by=(),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"status": core.RunStatus.PAUSED}),
            "records": core.RecordsState(hosts=(), records=(root,)),
        }
    )
    return state, root


def persisted_command(root: core.RequestRecord, request: core.Request) -> core.RequestRecord:
    """Model a previously committed cleanup or inspection request."""
    assert request.request_id is not None
    return root.model_copy(
        update={
            "request_id": request.request_id,
            "request": request,
            "payload_digest": request.request_id.root,
            "observation": None,
        }
    )


@given(generation=st.integers(min_value=0, max_value=5))
def test_prepared_query_remains_safe_without_live_mutation_admission(generation: int) -> None:
    """Read-only inspection recovery does not invent or require write authority."""
    state, root = state_with_root()
    request = core.InspectRequest(
        request_id=core.RequestId(root="read-only"),
        scope=root.request.scope.model_copy(update={"generation": generation}),
        target=core.RequestId(root="historical-target"),
    )
    record = persisted_command(root, request).model_copy(
        update={"phase": core.RequestPhase.PREPARED, "lifecycle": core.LifecycleClass.QUERY}
    )
    state = state.model_copy(
        update={"records": state.records.model_copy(update={"records": (root, record)})}
    )
    event = core.RecoveryStarted(epoch=1, now_at=10.0)
    result = core.step(state, event)
    assert result == core.step(core.CoreState.model_validate_json(state.model_dump_json()), event)
    check = next(
        row for row in result.state.records.recovery.checks if row.target == record.request_id
    )
    assert check.resolution == "safe-prepared"
    assert not any(
        isinstance(row, core.InspectRequest) and row.target == record.request_id
        for row in result.requests
    )
    assert tuple(map(unaddressed, result.state.records.records[:2])) == (root, record)
    assert result.state.run.receipts == state.run.receipts
