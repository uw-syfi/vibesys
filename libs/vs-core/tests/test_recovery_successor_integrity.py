"""Recovery commands retain canonical target identity across persistence replay."""

from typing import TypedDict

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


class CommandFields(TypedDict):
    request_id: core.RequestId
    scope: core.Scope
    admission_id: core.DecisionId | None
    deadline_at: float
    target: core.RequestId


def state_with_root() -> tuple[core.CoreState, core.Intent]:
    """An accepted live root with no typed owner remains cleanup authority."""
    state = core.initial_state()
    identity = core.RequestId(root="root")
    scope = core.Scope(owner=state.run.run_id, generation=0)
    request = core.EnsureSession(
        request_id=identity,
        scope=scope,
        deadline_at=100.0,
        spec=core.SessionSpec(
            session_id=core.SessionId(root="session"),
            role_id=core.RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=core.Access.WRITE_ARTIFACTS,
        ),
    )
    root = core.Intent(
        request_id=identity,
        request=request,
        payload_digest="root",
        phase=core.IntentPhase.DISPATCHED,
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        reconcile_deadline_at=100.0,
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
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"status": core.RunStatus.PAUSED}),
            "intents": core.IntentsState(intents=(root,)),
        }
    )
    return state, root


def persisted_command(root: core.Intent, request: core.Request) -> core.Intent:
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


@given(
    case=st.sampled_from(
        [
            (command, fault)
            for command in ("block", "inspect", "cancel")
            for fault in ("target", "scope", "admission", "kind", "resource")
            if (command, fault) != ("block", "resource")
        ]
    ),
)
def test_deadline_rejects_conflicting_committed_successor(case: tuple[str, str]) -> None:
    """A deterministic successor ID never permits a different target or lease."""
    command, fault = case
    state, root = state_with_root()
    event = core.ReconciliationDeadline(request_id=root.request_id, now_at=100.0)
    first = core.step(state, event)
    requests: dict[str, core.Request] = {row.kind: row for row in first.requests}
    successor = requests[
        {"block": "block_intent", "inspect": "inspect_request", "cancel": "cancel_owned_resource"}[
            command
        ]
    ]
    updates: dict[str, object] = {
        "target": core.RequestId(root="foreign"),
        "scope": core.Scope(owner=root.request.scope.owner, generation=1),
        "admission_id": core.DecisionId(root="foreign-episode"),
        "resource_id": core.ResourceId(root="foreign-lease"),
    }
    if fault == "kind":
        changed = root.request.model_copy(update={"request_id": successor.request_id})
    else:
        key = (
            "admission_id"
            if fault == "admission"
            else "resource_id"
            if fault == "resource"
            else fault
        )
        changed = successor.model_copy(update={key: updates[key]})
    record = persisted_command(root, changed)
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (root, record)})}
    )
    before = state.model_dump_json()
    with pytest.raises(core.ContractError, match="successor identity conflict"):
        core.step(state, event)
    assert state.model_dump_json() == before


@given(
    command=st.sampled_from(["inspect", "cancel", "block"]),
    fault=st.sampled_from(["cycle", "missing", "scope", "admission"]),
)
def test_deadline_rejects_corrupt_reconciliation_ancestry(command: str, fault: str) -> None:
    """Inspection and cleanup deadlines cannot cross canonical scope or episode."""
    state, root = state_with_root()
    identity = core.RequestId(root="successor")
    target = (
        identity
        if fault == "cycle"
        else core.RequestId(root="missing")
        if fault == "missing"
        else root.request_id
    )
    common: CommandFields = {
        "request_id": identity,
        "scope": root.request.scope.model_copy(update={"generation": 1})
        if fault == "scope"
        else root.request.scope,
        "admission_id": core.DecisionId(root="foreign") if fault == "admission" else None,
        "deadline_at": 100.0,
        "target": target,
    }
    requests = {
        "inspect": core.InspectRequest(**common),
        "cancel": core.CancelOwnedResource(**common, resource_id=core.ResourceId(root="physical")),
        "block": core.BlockIntent(**common, diagnostic="unresolved"),
    }
    record = persisted_command(root, requests[command])
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (root, record)})}
    )
    event = core.ReconciliationDeadline(request_id=identity, now_at=100.0)
    before = state.model_dump_json()
    with pytest.raises(core.ContractError, match=r"cyclic|missing canonical|conflicts with target"):
        core.step(state, event)
    assert state.model_dump_json() == before


@given(generation=st.integers(min_value=0, max_value=5))
def test_prepared_query_remains_safe_without_live_mutation_admission(generation: int) -> None:
    """Read-only inspection recovery does not invent or require write authority."""
    state, root = state_with_root()
    request = core.InspectRequest(
        request_id=core.RequestId(root="read-only"),
        scope=root.request.scope.model_copy(update={"generation": generation}),
        deadline_at=100.0,
        target=core.RequestId(root="historical-target"),
    )
    record = persisted_command(root, request).model_copy(
        update={"phase": core.IntentPhase.PREPARED, "lifecycle": core.LifecycleClass.QUERY}
    )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (root, record)})}
    )
    event = core.RecoveryStarted(epoch=1, now_at=10.0)
    result = core.step(state, event)
    assert result == core.step(core.CoreState.model_validate_json(state.model_dump_json()), event)
    check = next(
        row for row in result.state.intents.recovery.checks if row.target == record.request_id
    )
    assert check.resolution == "safe-prepared"
    assert not any(
        isinstance(row, core.InspectRequest) and row.target == record.request_id
        for row in result.requests
    )
    assert result.state.intents.intents[:2] == (root, record)
    assert result.state.run.receipts == state.run.receipts
