"""Obsolete setup deadlines cannot retire a positively reattached session."""

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_core.api import (
    Access,
    BlockRequest,
    CancelOwnedResource,
    ChildLease,
    CoreState,
    DispatchTurn,
    EnsureSession,
    EventId,
    FailureReason,
    Invocation,
    InvocationId,
    InvocationRef,
    LifecycleClass,
    Observation,
    ObservationStatus,
    ReconciliationDeadline,
    RecordsState,
    RecoveryBarrier,
    RecoveryPhase,
    RecoveryStarted,
    RequestId,
    RequestPhase,
    RequestRecord,
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
    Until,
    initial_state,
    step,
)
from vs_sim.api.testing import examples

from .restart import restarts

OWED = (RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING)


def pending_record(
    identity: str = "setup", phase: RequestPhase = RequestPhase.DISPATCHED
) -> RequestRecord:
    """Persist an exact setup request with an expired reconciliation bound."""
    request_id = RequestId(root=identity)
    request = EnsureSession(
        request_id=request_id,
        scope=Scope(owner=initial_state().run.run_id, generation=0),
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
        payload_digest=f"persisted-{identity}",
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=phase,
        until=Until(at=100.0),
    )


def observed(record: RequestRecord, **facts: object) -> Observation:
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


def recovering_state(*records: RequestRecord) -> CoreState:
    """Recovery owns its barrier while preserving the persisted request ledger."""
    state = initial_state()
    return state.model_copy(
        update={
            "records": RecordsState(hosts=(), records=records, recovery=RecoveryBarrier()),
            "run": state.run.model_copy(update={"status": RunStatus.PAUSED}),
        }
    )


def reload(state: CoreState) -> CoreState:
    """Exercise public validation of each persisted state before stepping."""
    if not restarts():
        return state
    return CoreState.model_validate_json(state.model_dump_json())


@example(
    phase=RequestPhase.COMPLETED,
    ready=True,
    successor=True,
    child_pending=False,
    elapsed=0,
)
@example(
    phase=RequestPhase.DISPATCHED,
    ready=True,
    successor=True,
    child_pending=False,
    elapsed=0,
)
@settings(max_examples=examples(pr=4, nightly=100))
@given(
    phase=st.sampled_from([RequestPhase.DISPATCHED, RequestPhase.COMPLETED]),
    ready=st.booleans(),
    successor=st.booleans(),
    child_pending=st.booleans(),
    elapsed=st.integers(min_value=0, max_value=10_000),
)
def test_a_passed_limit_ends_an_owed_root_even_when_reattached_and_cleans_its_children(
    phase: RequestPhase,
    *,
    ready: bool,
    successor: bool,
    child_pending: bool,
    elapsed: int,
) -> None:
    """Across barriers and live successors, a passed limit ends the record that owes a result."""
    original = pending_record(phase=phase)
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
        charge_class="paid",
    )
    live = pending_record(identity="successor").model_copy(
        update={
            "request": DispatchTurn(
                request_id=pending_record(identity="successor").request_id,
                scope=request.scope,
                turn=turn,
            ),
            "lifecycle": LifecycleClass.SESSION_TURN,
            "until": Until(at=20_000.0),
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
        until=Until(at=100.0),
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
    anchor = pending_record(identity="independent-anchor")
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
            "records": state.records.model_copy(
                update={"children": (child,) if child_pending else ()}
            )
        }
    )
    if ready:
        state = state.model_copy(
            update={
                "records": state.records.model_copy(
                    update={"recovery": RecoveryBarrier(epoch=1, phase=RecoveryPhase.READY)}
                )
            }
        )
    else:
        state = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0)).state
    # The restart moved the limit past its downtime; the deadline comes at or after it.
    limit = next(row for row in state.records.records if row.request_id == original.request_id)
    result = step(
        reload(state),
        ReconciliationDeadline(request_id=original.request_id, now_at=limit.until.at + elapsed),
    )
    cancellations = {
        value.resource_id for value in result.requests if isinstance(value, CancelOwnedResource)
    }
    assert cancellations == ({child.resource_id} if child_pending else set())
    assert result.state.sessions == state.sessions
    retained = {record.request_id: record for record in result.state.records.records}
    for record in state.records.records:
        after = retained[record.request_id]
        if record.request_id == original.request_id and record.phase in OWED:
            # The limit ends a record whose result is still owed, reattached or not.
            assert after.failure is not None
            assert after.failure.reason == FailureReason.TIMED_OUT
            record_after = record.model_copy(
                update={"phase": RequestPhase.BLOCKED, "failure": after.failure}
            )
        else:
            # A record that already concluded keeps its result; only its child is cleaned up.
            record_after = record
        assert after == record_after
    assert result.state.records.children == state.records.children
    if not child_pending:
        assert all(isinstance(value, BlockRequest) for value in result.requests)
        assert bool(result.requests) == (phase in OWED)
        assert result.state.records.recovery == state.records.recovery
    # No spin: a second deadline for the same record changes nothing and publishes nothing.
    again = step(
        reload(result.state),
        ReconciliationDeadline(request_id=original.request_id, now_at=100.0 + elapsed + 1.0),
    )
    assert again.state.records.records == result.state.records.records
    assert again.events == ()


@example(missing=False, elapsed=0)
@settings(max_examples=examples(pr=4, nightly=100))
@given(
    missing=st.booleans(),
    elapsed=st.integers(min_value=0, max_value=10_000),
)
def test_setup_deadline_without_exact_resource_proof_only_blocks_and_inspects(
    *, missing: bool, elapsed: int
) -> None:
    """Missing or conflicting identity does not authorize resource cancellation."""
    original = pending_record()
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
    assert {value.kind for value in result.requests} == {"block_request", "inspect_request"}
    assert result.state.records.recovery.phase == RecoveryPhase.BLOCKED
