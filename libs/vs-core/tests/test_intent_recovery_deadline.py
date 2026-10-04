"""Obsolete setup deadlines cannot retire a positively reattached session."""

from hypothesis import example, given
from hypothesis import strategies as st

from vs_core.api import (
    Access,
    CancelOwnedResource,
    ChildLease,
    CoreState,
    DispatchTurn,
    EnsureSession,
    EventId,
    Intent,
    IntentPhase,
    IntentsState,
    Invocation,
    InvocationId,
    InvocationRef,
    LifecycleClass,
    Observation,
    ObservationStatus,
    ReconciliationDeadline,
    RecoveryBarrier,
    RecoveryPhase,
    RecoveryStarted,
    RequestId,
    ResourceId,
    RoleId,
    RunStatus,
    SchemaRef,
    Scope,
    SessionId,
    SessionPhase,
    SessionSpec,
    SessionsState,
    SessionView,
    TurnSpec,
    initial_state,
    step,
)


def pending_intent(identity: str = "setup", phase: IntentPhase = IntentPhase.DISPATCHED) -> Intent:
    """Persist an exact setup request with an expired reconciliation bound."""
    request_id = RequestId(root=identity)
    request = EnsureSession(
        request_id=request_id,
        scope=Scope(owner=initial_state().run.run_id, generation=0),
        deadline_at=100.0,
        spec=SessionSpec(
            session_id=SessionId(root=identity),
            role_id=RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
    )
    return Intent(
        request_id=request_id,
        request=request,
        payload_digest=f"persisted-{identity}",
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=phase,
        reconcile_deadline_at=100.0,
    )


def observed(record: Intent, **facts: object) -> Observation:
    """Supply canonical identity before varying public observation facts."""
    return Observation.model_validate(
        {
            "event_id": EventId(root=f"fact-{record.request_id.root}"),
            "request_id": record.request_id,
            "scope": record.request.scope,
            "sequence": 1,
            "observed_at": 10.0,
            "status": ObservationStatus.UNKNOWN,
            **facts,
        }
    )


def recovering_state(*records: Intent) -> CoreState:
    """Recovery owns its barrier while preserving the persisted request ledger."""
    state = initial_state()
    return state.model_copy(
        update={
            "intents": IntentsState(intents=records, recovery=RecoveryBarrier()),
            "run": state.run.model_copy(update={"status": RunStatus.PAUSED}),
        }
    )


def reload(state: CoreState) -> CoreState:
    """Exercise public validation of each persisted state before stepping."""
    return CoreState.model_validate_json(state.model_dump_json())


@example(
    phase=IntentPhase.COMPLETED,
    ready=True,
    successor=True,
    child_pending=False,
    elapsed=0,
)
@given(
    phase=st.sampled_from([IntentPhase.DISPATCHED, IntentPhase.COMPLETED]),
    ready=st.booleans(),
    successor=st.booleans(),
    child_pending=st.booleans(),
    elapsed=st.integers(min_value=0, max_value=10_000),
)
def test_reattached_setup_deadline_preserves_live_root_and_child_cleanup(
    phase: IntentPhase,
    *,
    ready: bool,
    successor: bool,
    child_pending: bool,
    elapsed: int,
) -> None:
    """Across barriers and live successors, setup expiry has no root authority."""
    original = pending_intent(phase=phase)
    assert isinstance(original.request, EnsureSession)
    resource = ResourceId(root="conversation")
    original = original.model_copy(
        update={
            "request": original.request.model_copy(update={"required_resource": resource}),
            "observation": observed(
                original,
                resource_id=resource,
                accepted=True,
                terminal=True,
                children_complete=True,
                status=ObservationStatus.SUCCEEDED,
            ),
        }
    )
    request = original.request
    assert isinstance(request, EnsureSession)
    turn = TurnSpec(
        session=request.spec,
        invocation_id=InvocationId(root="successor"),
        workspace=request.scope,
        prompts=(),
        output_schema=SchemaRef(name="output", version=1),
        deadline_at=20_000.0,
        charge_class="paid",
    )
    live = pending_intent(identity="successor").model_copy(
        update={
            "request": DispatchTurn(
                request_id=pending_intent(identity="successor").request_id,
                scope=request.scope,
                deadline_at=20_000.0,
                turn=turn,
            ),
            "lifecycle": LifecycleClass.SESSION_TURN,
            "reconcile_deadline_at": 20_000.0,
        }
    )
    live = live.model_copy(
        update={
            "observation": observed(
                live,
                resource_id=resource,
                accepted=True,
                children_complete=True,
                status=ObservationStatus.PENDING,
            )
        }
    )
    owner = SessionView(
        spec=request.spec,
        scope=request.scope,
        generation=request.scope.generation,
        phase=SessionPhase.EXECUTING if successor else SessionPhase.IDLE,
        accepted=True,
        resource_id=resource,
    )
    invocation = Invocation(
        invocation=InvocationRef(
            session_id=request.spec.session_id,
            invocation_id=turn.invocation_id,
            generation=request.scope.generation,
        ),
        scope=request.scope,
        turn=turn,
        phase=SessionPhase.EXECUTING,
        observation=live.observation,
    )
    anchor = pending_intent(identity="independent-anchor")
    state = (
        recovering_state(original, live, anchor)
        if successor
        else recovering_state(original, anchor)
    )
    state = state.model_copy(
        update={
            "sessions": SessionsState(
                sessions=(owner,), invocations=(invocation,) if successor else ()
            )
        }
    )
    child = ChildLease(
        resource_id=ResourceId(root="unresolved-child"),
        scope=request.scope,
        source_requests=(original.request_id,),
        parent_resources=(resource,),
    )
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={"children": (child,) if child_pending else ()}
            )
        }
    )
    if ready:
        state = state.model_copy(
            update={
                "intents": state.intents.model_copy(
                    update={"recovery": RecoveryBarrier(epoch=1, phase=RecoveryPhase.READY)}
                )
            }
        )
    else:
        state = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0)).state
    result = step(
        reload(state),
        ReconciliationDeadline(request_id=original.request_id, now_at=100.0 + elapsed),
    )
    cancellations = {
        value.resource_id for value in result.requests if isinstance(value, CancelOwnedResource)
    }
    assert cancellations == ({child.resource_id} if child_pending else set())
    assert result.state.sessions == state.sessions
    retained = {intent.request_id: intent for intent in result.state.intents.intents}
    assert all(retained[intent.request_id] == intent for intent in state.intents.intents)
    assert result.state.intents.children == state.intents.children
    if not child_pending:
        assert result.requests == ()
        assert result.state.intents.recovery == state.intents.recovery


@example(missing=False, elapsed=0)
@given(
    missing=st.booleans(),
    elapsed=st.integers(min_value=0, max_value=10_000),
)
def test_setup_deadline_without_exact_resource_proof_only_blocks_and_inspects(
    *, missing: bool, elapsed: int
) -> None:
    """Missing or conflicting identity does not authorize resource cancellation."""
    original = pending_intent()
    original = original.model_copy(
        update={
            "request": original.request.model_copy(
                update={"required_resource": ResourceId(root="required-conversation")}
            ),
            "observation": observed(
                original,
                resource_id=None if missing else ResourceId(root="unrelated-conversation"),
                accepted=True,
                status=ObservationStatus.PENDING,
            ),
        }
    )
    result = step(
        reload(recovering_state(original)),
        ReconciliationDeadline(request_id=original.request_id, now_at=100.0 + elapsed),
    )
    assert all(not isinstance(value, CancelOwnedResource) for value in result.requests)
    assert {value.kind for value in result.requests} == {"block_intent", "inspect_request"}
    assert result.state.intents.recovery.phase == RecoveryPhase.BLOCKED
