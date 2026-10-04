"""Recovery proofs and replay through the public immutable lifecycle API."""

from typing import ClassVar, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    Accepted,
    Access,
    ArtifactId,
    ArtifactRef,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptRef,
    AttemptsState,
    AttemptView,
    BlockIntent,
    CancelOwnedResource,
    ChargeId,
    ChargeKind,
    ChargeReceipt,
    ChildLease,
    CloseAttemptScope,
    ContinuationId,
    ContractError,
    CoreState,
    DecisionId,
    DecisionReceipt,
    DiscardWorkspace,
    DispatchAuthorized,
    DispatchTurn,
    EnsureSession,
    EvaluationState,
    EventCursor,
    EventId,
    ExecuteRegisteredOperation,
    HostFence,
    HostId,
    InputDelivered,
    InputDropped,
    InputDropReason,
    InputId,
    InputRecord,
    InspectRequest,
    Intent,
    IntentPhase,
    IntentsState,
    Invocation,
    InvocationId,
    InvocationInputTarget,
    InvocationRef,
    ItemId,
    LifecycleClass,
    Limits,
    Observation,
    ObservationStatus,
    Operation,
    OperationDescriptor,
    OperationId,
    OperationNormalizationKind,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    OperationSchemaRef,
    OperationWire,
    PoolId,
    ReconciliationDeadline,
    RecoveryBarrier,
    RecoveryPhase,
    RecoveryStarted,
    RegisteredOwnedJob,
    RequestId,
    ResourceId,
    RoleId,
    RunEnvelope,
    RunStatus,
    SchedulingState,
    SchemaRef,
    Scope,
    ScopedAdmissionReopen,
    ScopedAdmissionReopenOutcome,
    ScopeInputTarget,
    ScopeReopenNormalization,
    SessionId,
    SessionInput,
    SessionPhase,
    SessionSpec,
    SessionsState,
    SessionView,
    Slot,
    StrategyState,
    TurnSpec,
    Value,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    project,
    step,
)


class RegisteredOutcome(Value):
    """Small immutable outcome contract for generic lifecycle fixtures."""

    accepted: bool


class RegisteredJobRequest(OperationRequest):
    """An actual owned-job payload bound by the public operation codec."""

    kind: Literal["cluster.owned"] = "cluster.owned"
    lifecycle: Literal[LifecycleClass.OWNED_JOB] = LifecycleClass.OWNED_JOB
    outcome_model: ClassVar[type[BaseModel]] = RegisteredOutcome


class RegisteredTurnRequest(OperationRequest):
    """An actual agent operation whose lifecycle belongs to sessions."""

    kind: Literal["agent.turn"] = "agent.turn"
    lifecycle: Literal[LifecycleClass.SESSION_TURN] = LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = RegisteredOutcome
    turn: TurnSpec


def normalize_registered_turn(request: OperationRequest) -> TurnSpec:
    """The registered payload owns its explicit pure session-turn normalization."""
    assert isinstance(request, RegisteredTurnRequest)
    return request.turn


def normalize_scope_reopen(request: OperationRequest) -> ScopeReopenNormalization:
    """Use only the frozen reopening payload as normalization authority."""
    assert isinstance(request, ScopedAdmissionReopen)
    return ScopeReopenNormalization(
        attempt=request.attempt,
        continuation_id=request.continuation_id,
        park_authority=request.park_authority,
        resolved_cancelled_jobs=request.resolved_cancelled_jobs,
    )


REGISTERED_REQUESTS = {
    "cluster.owned": RegisteredJobRequest,
    "agent.turn": RegisteredTurnRequest,
    "evaluation.scope.reopen": ScopedAdmissionReopen,
}


def pending_intent(
    identity: str = "original", phase: IntentPhase = IntentPhase.DISPATCHED
) -> Intent:
    """A persisted operation whose external acceptance has no positive proof."""
    state = initial_state()
    request_id = RequestId(root=identity)
    request = EnsureSession(
        request_id=request_id,
        scope=Scope(owner=state.run.run_id, generation=0),
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


def recovering_state(*records: Intent) -> CoreState:
    """Reloaded state starts behind the required barrier, retaining all ledgers."""
    state = initial_state()
    return state.model_copy(
        update={
            "intents": IntentsState(intents=records, recovery=RecoveryBarrier()),
            "run": state.run.model_copy(update={"status": RunStatus.PAUSED}),
        }
    )


def reload(state: CoreState) -> CoreState:
    """Reload an atomic envelope through the public registered persistence codec."""
    codec = OperationRegistry(
        tuple(
            OperationRegistration(
                descriptor=descriptor,
                request_model=REGISTERED_REQUESTS[descriptor.kind],
                outcome_model=REGISTERED_REQUESTS[descriptor.kind].outcome_model,
                normalize_turn=normalize_registered_turn
                if descriptor.lifecycle == LifecycleClass.SESSION_TURN
                else None,
                normalize_scope_reopen=normalize_scope_reopen
                if descriptor.normalization == OperationNormalizationKind.SCOPE_REOPEN
                else None,
            )
            for descriptor in state.registry
        )
    )
    envelope = RunEnvelope[StrategyState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        core=state,
        fence=HostFence(host_id=HostId(root="recovery-host"), epoch=state.intents.recovery.epoch),
        event_cursor=EventCursor(sequence=state.revision),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        strategy=StrategyState(schema_version=state.run.declaration.state_schema.version),
    )
    encoded = codec.encode_envelope(envelope)
    decoded = codec.decode_envelope(RunEnvelope[StrategyState], encoded)
    assert decoded == envelope
    assert codec.encode_envelope(decoded) == encoded
    return decoded.core


def observed(record: Intent, **facts: object) -> Observation:
    """An exact persisted root observation with supplied time and identity."""
    return Observation.model_validate(
        {
            "event_id": EventId(root=f"observation-{record.request_id.root}"),
            "request_id": record.request_id,
            "scope": record.request.scope,
            "sequence": 1,
            "observed_at": 10.0,
            "status": ObservationStatus.UNKNOWN,
            **facts,
        }
    )


@pytest.mark.parametrize(
    "phase", [IntentPhase.DISPATCHED, IntentPhase.RECONCILING, IntentPhase.BLOCKED]
)
def test_recovery_inspects_unfinished_authority_without_replaying_payload(
    phase: IntentPhase,
) -> None:
    # Legacy L:569 recovery and O:488 unknown provider acceptance.
    original = pending_intent(phase=phase)
    state = recovering_state(original)
    result = step(reload(state), RecoveryStarted(epoch=3, now_at=12.0))
    barrier = result.state.intents.recovery
    assert barrier.epoch == 3
    assert barrier.phase == RecoveryPhase.RECOVERING
    assert len(barrier.checks) == 1
    check = barrier.checks[0]
    assert check.target == original.request_id
    assert check.resolution == "pending"
    assert len(result.requests) == 1
    inspection = result.requests[0]
    assert isinstance(inspection, InspectRequest)
    assert inspection.target == original.request_id
    assert inspection.request_id == check.inspection
    assert result.state.intents.intents[0] == original
    assert result.state.run.status == RunStatus.PAUSED
    assert result.state.run.now_at == 12.0
    assert project(result.state).scheduling.charged == project(state).scheduling.charged
    assert result.events == ()
    assert reload(result.state) == result.state


@example(events=[(0, 0), (1, 0)])
@given(st.lists(st.tuples(st.integers(0, 5), st.integers(0, 99)), min_size=1, max_size=30))
def test_startup_sequences_preserve_ownership_and_deduplicate_inspection(
    events: list[tuple[int, int]],
) -> None:
    """Duplicate, reordered and stale epochs retain one inspection per target/epoch."""
    original = pending_intent()
    state = recovering_state(original)
    epoch = 0
    now_at = 0.0
    inspections: set[RequestId] = set()
    for supplied_epoch, supplied_time in events:
        event = RecoveryStarted(epoch=supplied_epoch, now_at=float(supplied_time))
        before = state.model_dump_json()
        result = step(state, event)
        assert result == step(reload(state), event)
        assert state.model_dump_json() == before
        epoch = max(epoch, supplied_epoch)
        now_at = max(now_at, float(supplied_time))
        assert result.state.intents.recovery.epoch == epoch
        assert result.state.run.now_at == now_at
        assert result.state.intents.intents[0] == original
        assert result.state.run.status == RunStatus.PAUSED
        assert result.events == ()
        for request in result.requests:
            assert isinstance(request, InspectRequest)
            assert request.target == original.request_id
            assert request.request_id is not None
            assert request.request_id not in inspections
            inspections.add(request.request_id)
        assert len(inspections) <= 6
        checks = result.state.intents.recovery.checks
        assert checks[0].target == original.request_id
        assert checks[0].resolution == "pending"
        assert all(check.resolution == "safe-prepared" for check in checks[1:])
        assert len(checks) <= 7
        assert project(result.state).scheduling.charged == 0
        assert project(result.state).scheduling.refunded == 0
        state = reload(result.state)


def test_query_completion_without_target_proof_cannot_complete_recovery() -> None:
    original = pending_intent(phase=IntentPhase.COMPLETED)
    original = original.model_copy(update={"observation": observed(original)})
    result = step(recovering_state(original), RecoveryStarted(epoch=1, now_at=20.0))
    assert result.state.intents.recovery.phase == RecoveryPhase.RECOVERING
    assert result.state.intents.recovery.checks[0].resolution == "pending"
    assert isinstance(result.requests[0], InspectRequest)
    assert result.state.intents.intents[0] == original


@given(st.integers(min_value=0, max_value=99))
def test_reconciliation_before_deadline_never_authorizes_cleanup(now_at: int) -> None:
    original = pending_intent()
    started = step(recovering_state(original), RecoveryStarted(epoch=1, now_at=0.0))
    result = step(
        reload(started.state),
        ReconciliationDeadline(request_id=original.request_id, now_at=float(now_at)),
    )
    assert result.requests == ()
    assert result.events == ()
    assert result.state.intents == started.state.intents
    assert result.state.run.status == RunStatus.PAUSED


@pytest.mark.parametrize("now_at", [100.0, 101.0, 1000.0])
def test_deadline_blocks_ambiguity_without_fabricating_terminal_ledger_fact(now_at: float) -> None:
    original = pending_intent()
    started = step(recovering_state(original), RecoveryStarted(epoch=1, now_at=0.0))
    event = ReconciliationDeadline(request_id=original.request_id, now_at=now_at)
    result = step(reload(started.state), event)
    assert result.state.intents.recovery.phase == RecoveryPhase.BLOCKED
    assert result.state.intents.recovery.checks[0].resolution == "blocked"
    assert result.state.intents.intents[0] == original
    assert result.state.run.status == RunStatus.PAUSED
    blocks = [request for request in result.requests if isinstance(request, BlockIntent)]
    inspections = [request for request in result.requests if isinstance(request, InspectRequest)]
    assert len(blocks) == 1
    assert blocks[0].target == original.request_id
    assert len(inspections) == 1
    assert inspections[0].target == original.request_id
    assert inspections[0].request_id == result.state.intents.recovery.checks[0].inspection
    repeated = step(reload(result.state), event)
    assert repeated.requests == ()
    assert repeated.state.intents == result.state.intents
    assert repeated.events == ()


def test_unknown_deadline_target_rejects_without_mutating_input() -> None:
    state = recovering_state(pending_intent())
    before = state.model_dump_json()
    with pytest.raises(ContractError):
        step(state, ReconciliationDeadline(request_id=RequestId(root="foreign"), now_at=100.0))
    assert state.model_dump_json() == before


def test_cas_fence_reducer_side_requires_committed_recovery_before_dispatch() -> None:
    original = pending_intent(phase=IntentPhase.PREPARED)
    state = recovering_state(original)
    with pytest.raises(ContractError, match="recovery"):
        step(reload(state), DispatchAuthorized(request_id=original.request_id))
    assert state.intents.intents == (original,)


def test_child_ownership_survives_parent_deadline_and_cleanup_requests() -> None:
    original = pending_intent()
    root = ResourceId(root="parent")
    child = ResourceId(root="late-child")
    original = original.model_copy(
        update={"observation": observed(original, resource_id=root, accepted=True)}
    )
    state = recovering_state(original)
    lease = ChildLease(
        resource_id=child,
        scope=original.request.scope,
        source_requests=(original.request_id,),
        parent_resources=(root,),
    )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"children": (lease,)})}
    )
    started = step(reload(state), RecoveryStarted(epoch=1, now_at=0.0))
    result = step(
        reload(started.state),
        ReconciliationDeadline(request_id=original.request_id, now_at=100.0),
    )
    assert result.state.intents.children == (lease,)
    cancelled = {
        request.resource_id
        for request in result.requests
        if isinstance(request, CancelOwnedResource)
    }
    assert cancelled == {root, child}
    assert result.state.intents.intents[0] == original
    assert result.state.run.status == RunStatus.PAUSED
    assert reload(result.state) == result.state


@given(
    phase=st.sampled_from(list(IntentPhase)),
    accepted=st.booleans(),
    terminal=st.booleans(),
    released=st.booleans(),
    complete=st.booleans(),
)
def test_unknown_acceptance_flags_never_replace_positive_recovery_proof(
    phase: IntentPhase,
    *,
    accepted: bool,
    terminal: bool,
    released: bool,
    complete: bool,
) -> None:
    """F3/F4/F5: ambiguous acknowledgement never authorizes reuse or release."""
    original = pending_intent(phase=phase)
    original = original.model_copy(
        update={
            "observation": observed(
                original,
                resource_id=ResourceId(root="possibly-accepted"),
                accepted=accepted,
                terminal=terminal,
                released=released,
                children_complete=complete,
            ),
        }
    )
    state = recovering_state(original)
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    check = result.state.intents.recovery.checks[0]
    assert check.resolution == "pending"
    assert result.state.intents.recovery.phase == RecoveryPhase.RECOVERING
    assert len(result.requests) == 1
    assert isinstance(result.requests[0], InspectRequest)
    assert result.state.intents.intents[0] == original
    assert result.state.intents.children == state.intents.children
    assert result.state.scheduling == state.scheduling
    assert result.state.attempts == state.attempts
    assert result.state.sessions == state.sessions
    assert reload(result.state) == result.state


@given(
    st.lists(
        st.tuples(st.sampled_from(["startup", "deadline"]), st.integers(0, 4), st.integers(0, 150)),
        min_size=1,
        max_size=30,
    )
)
def test_recovery_deadline_sequences_bound_requests_without_terminal_receipts(
    sequence: list[tuple[str, int, int]],
) -> None:
    """Crash/replay at every supplied event keeps accounting and parent facts."""
    original = pending_intent()
    state = recovering_state(original)
    seen: set[RequestId] = set()
    for kind, epoch, now_at in sequence:
        event = (
            RecoveryStarted(epoch=epoch, now_at=float(now_at))
            if kind == "startup"
            else ReconciliationDeadline(request_id=original.request_id, now_at=float(now_at))
        )
        result = step(state, event)
        assert result == step(reload(state), event)
        assert result.state.intents.intents[0] == original
        assert result.state.intents.recovery.phase != RecoveryPhase.READY
        assert result.state.run.status == RunStatus.PAUSED
        assert result.events == ()
        assert result.state.run.receipts == state.run.receipts
        assert result.state.scheduling == state.scheduling
        assert result.state.attempts == state.attempts
        assert result.state.sessions == state.sessions
        for request in result.requests:
            assert isinstance(request, InspectRequest | BlockIntent)
            assert request.target == original.request_id
            assert request.request_id is not None
            assert request.request_id not in seen
            seen.add(request.request_id)
            if isinstance(request, BlockIntent):
                assert result.state.run.now_at >= original.reconcile_deadline_at
        assert len(seen) <= 15
        state = reload(result.state)


@given(st.text(alphabet="abc123", min_size=1, max_size=20))
def test_user_request_identity_prefix_never_hides_unresolved_authority(suffix: str) -> None:
    """Generated reconciliation IDs are not a reserved user identity namespace."""
    original = pending_intent(identity=f"recovery:{suffix}")
    anchor = pending_intent(identity="anchor")
    result = step(recovering_state(original, anchor), RecoveryStarted(epoch=1, now_at=0.0))
    checks = {check.target: check for check in result.state.intents.recovery.checks}
    assert original.request_id in checks
    assert checks[original.request_id].resolution == "pending"
    assert {
        request.target for request in result.requests if isinstance(request, InspectRequest)
    } == {
        original.request_id,
        anchor.request_id,
    }
    assert result.state.intents.intents[:2] == (original, anchor)


@pytest.mark.parametrize("released", [False, True])
def test_completed_but_live_session_with_incomplete_manifest_cannot_complete_recovery(
    *,
    released: bool,
) -> None:
    """A successful acquisition command is not complete lease correspondence."""
    original = pending_intent(phase=IntentPhase.COMPLETED)
    observation = observed(original, accepted=True, terminal=True, released=released)
    observation = observation.model_copy(update={"status": ObservationStatus.SUCCEEDED})
    original = original.model_copy(update={"observation": observation})
    result = step(
        recovering_state(original, pending_intent(identity="anchor")),
        RecoveryStarted(epoch=1, now_at=10.0),
    )
    check = next(
        check
        for check in result.state.intents.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == "pending"
    assert any(
        isinstance(request, InspectRequest) and request.target == original.request_id
        for request in result.requests
    )
    assert result.state.intents.intents[0] == original


def test_parent_release_does_not_hide_live_child_at_deadline() -> None:
    """Child ownership is independent even after a conclusive parent release."""
    original = pending_intent(phase=IntentPhase.COMPLETED)
    root = ResourceId(root="released-parent")
    child = ResourceId(root="still-live")
    observation = observed(
        original,
        resource_id=root,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
        children=(child,),
    ).model_copy(update={"status": ObservationStatus.SUCCEEDED})
    original = original.model_copy(update={"observation": observation})
    lease = ChildLease(
        resource_id=child,
        scope=original.request.scope,
        source_requests=(original.request_id,),
        parent_resources=(root,),
    )
    state = recovering_state(original, pending_intent(identity="anchor"))
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"children": (lease,)})}
    )
    started = step(state, RecoveryStarted(epoch=1, now_at=10.0))
    result = step(
        reload(started.state),
        ReconciliationDeadline(request_id=original.request_id, now_at=100.0),
    )
    assert {
        request.resource_id
        for request in result.requests
        if isinstance(request, CancelOwnedResource)
    } == {child}
    assert result.state.intents.children == (lease,)
    assert result.state.intents.intents[0] == original
    assert result.state.run.status == RunStatus.PAUSED


def turn_intent() -> Intent:
    """An honest session-turn request with a durable invocation identity."""
    original = pending_intent()
    assert isinstance(original.request, EnsureSession)
    turn = TurnSpec(
        session=original.request.spec,
        invocation_id=InvocationId(root="turn"),
        workspace=original.request.scope,
        prompts=(),
        output_schema=SchemaRef(name="turn-output", version=1),
        deadline_at=100.0,
        charge_class="paid",
    )
    return original.model_copy(
        update={
            "request": DispatchTurn(
                request_id=original.request_id,
                scope=original.request.scope,
                deadline_at=100.0,
                turn=turn,
            ),
            "lifecycle": LifecycleClass.SESSION_TURN,
        }
    )


@given(
    case=st.sampled_from(
        ["missing", "resource", "request", "scope", "generation", "invocation", "session", "exact"]
    )
)
def test_session_turn_recovery_requires_exact_typed_owner(case: str) -> None:
    """F3: known acceptance reattaches only the same invocation and physical lease."""
    original = turn_intent()
    assert isinstance(original.request, DispatchTurn)
    request = original.request
    resource = ResourceId(root="session-resource")
    observation = observed(original, resource_id=resource, accepted=True, children_complete=True)
    observation = observation.model_copy(update={"status": ObservationStatus.PENDING})
    original = original.model_copy(update={"observation": observation})
    state = recovering_state(original, pending_intent(identity="anchor"))
    owner_observation = observation.model_copy(
        update={
            "resource_id": ResourceId(root="foreign-resource") if case == "resource" else resource,
            "request_id": RequestId(root="foreign-request")
            if case == "request"
            else original.request_id,
        }
    )
    invocation = Invocation(
        invocation=InvocationRef(
            session_id=SessionId(root="foreign-session")
            if case == "session"
            else request.turn.session.session_id,
            invocation_id=InvocationId(root="foreign-invocation")
            if case == "invocation"
            else request.turn.invocation_id,
            generation=1 if case == "generation" else 0,
        ),
        scope=original.request.scope.model_copy(update={"generation": 1})
        if case == "scope"
        else original.request.scope,
        turn=request.turn,
        phase=SessionPhase.EXECUTING,
        observation=owner_observation,
    )
    state = state.model_copy(
        update={"sessions": SessionsState(invocations=() if case == "missing" else (invocation,))}
    )
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check
        for check in result.state.intents.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == ("reattached" if case == "exact" else "pending")
    assert result.state.sessions == state.sessions
    assert result.state.intents.intents[0] == original
    assert result.state.intents.recovery.phase == RecoveryPhase.RECOVERING
    assert not any(isinstance(request, CancelOwnedResource) for request in result.requests)


@given(
    case=st.sampled_from(
        ["exact", "missing", "resource", "generation", "unaccepted", "required", "provisional"]
    )
)
def test_session_lease_reattachment_preserves_conversation_identity(case: str) -> None:
    """F5 reopen: a fresh or mismatched lease cannot stand in for required_resource."""
    original = pending_intent(phase=IntentPhase.COMPLETED)
    assert isinstance(original.request, EnsureSession)
    resource = ResourceId(root="physical-session")
    request = original.request.model_copy(
        update={
            "required_resource": ResourceId(root="other-conversation")
            if case == "required"
            else resource
        }
    )
    observation = observed(
        original, resource_id=resource, accepted=True, terminal=True, children_complete=True
    )
    observation = observation.model_copy(update={"status": ObservationStatus.SUCCEEDED})
    original = original.model_copy(update={"request": request, "observation": observation})
    owner = SessionView(
        spec=request.spec,
        scope=request.scope,
        generation=1 if case == "generation" else 0,
        phase=SessionPhase.ACQUIRING if case == "provisional" else SessionPhase.IDLE,
        accepted=case not in ("unaccepted", "provisional"),
        pending_intents=(original.request_id,) if case == "provisional" else (),
        resource_id=None
        if case == "provisional"
        else ResourceId(root="other-resource")
        if case == "resource"
        else resource,
    )
    state = recovering_state(original, pending_intent(identity="anchor"))
    state = state.model_copy(
        update={"sessions": SessionsState(sessions=() if case == "missing" else (owner,))}
    )
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check
        for check in result.state.intents.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == ("reattached" if case in ("exact", "provisional") else "pending")
    assert result.state.sessions == state.sessions
    assert result.state.intents.intents[0] == original


@given(
    status=st.sampled_from(
        [ObservationStatus.SUCCEEDED, ObservationStatus.FAILED, ObservationStatus.CANCELLED]
    )
)
def test_completed_query_resolves_itself_without_resolving_pending_target(
    status: ObservationStatus,
) -> None:
    """Recovery distinguishes query completion from the inspected target's facts."""
    query = pending_intent(identity="query", phase=IntentPhase.COMPLETED)
    request = InspectRequest(
        request_id=query.request_id,
        scope=query.request.scope,
        deadline_at=100.0,
        target=RequestId(root="external-target"),
    )
    query = query.model_copy(
        update={
            "request": request,
            "lifecycle": LifecycleClass.QUERY,
            "observation": observed(query, status=status, terminal=True),
        }
    )
    target = pending_intent(identity="pending-target")
    result = step(recovering_state(query, target), RecoveryStarted(epoch=1, now_at=11.0))
    checks = {check.target: check for check in result.state.intents.recovery.checks}
    assert checks[query.request_id].resolution == "terminal"
    assert checks[target.request_id].resolution == "pending"
    assert result.state.intents.recovery.phase == RecoveryPhase.RECOVERING
    assert [
        request.target for request in result.requests if isinstance(request, InspectRequest)
    ] == [target.request_id]


def test_accepted_owned_job_without_resource_identity_cannot_fabricate_terminal_proof() -> None:
    """An owned-job acknowledgement requires an external identity or nonacceptance."""
    original = pending_intent(phase=IntentPhase.COMPLETED)
    schema = OperationSchemaRef(
        kind="cluster.owned",
        request_schema=SchemaRef(name="owned-request", version=1),
        outcome_schema=SchemaRef(name="owned-outcome", version=1),
        lifecycle=LifecycleClass.OWNED_JOB,
    )
    descriptor = OperationDescriptor(
        **schema.model_dump(), resource_pool=PoolId(root="jobs"), inspect=True, cancel=True
    )
    request = ExecuteRegisteredOperation(
        request_id=original.request_id,
        scope=original.request.scope,
        deadline_at=100.0,
        operation_id=OperationId(root="owned"),
        operation=OperationWire(schema_ref=schema, payload_json="{}"),
        retry_limit=0,
    )
    original = original.model_copy(
        update={
            "request": request,
            "lifecycle": LifecycleClass.OWNED_JOB,
            "observation": observed(
                original,
                status=ObservationStatus.SUCCEEDED,
                accepted=True,
                terminal=True,
                released=True,
                children_complete=True,
            ),
        }
    )
    state = recovering_state(original, pending_intent(identity="anchor"))
    state = state.model_copy(update={"registry": (descriptor,)})
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=10.0))
    check = next(
        check
        for check in result.state.intents.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == "pending"
    assert any(
        isinstance(request, InspectRequest) and request.target == original.request_id
        for request in result.requests
    )


def test_child_release_requires_its_exact_source_request() -> None:
    """A foreign root's terminal acknowledgement cannot clear this child lease."""
    original = pending_intent()
    child = ResourceId(root="child")
    observation = observed(
        original,
        request_id=RequestId(root="foreign"),
        resource_id=child,
        terminal=True,
        released=True,
        children_complete=True,
        status=ObservationStatus.SUCCEEDED,
    )
    lease = ChildLease(
        resource_id=child,
        scope=original.request.scope,
        source_requests=(original.request_id,),
        observation=observation,
    )
    state = recovering_state(original)
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"children": (lease,)})}
    )
    started = step(reload(state), RecoveryStarted(epoch=1, now_at=10.0))
    assert started.state.intents.recovery.checks[0].resolution == "pending"
    result = step(
        reload(started.state), ReconciliationDeadline(request_id=original.request_id, now_at=100.0)
    )
    assert {
        request.resource_id
        for request in result.requests
        if isinstance(request, CancelOwnedResource)
    } == {child}
    assert result.state.intents.children == (lease,)


@given(st.lists(st.lists(st.integers(0, 5), max_size=6), min_size=1, max_size=4))
def test_recovery_reconstructs_shared_descendant_proofs_canonically(
    manifests: list[list[int]],
) -> None:
    """Persisted child manifests deduplicate discoveries without losing ancestry."""
    originals = []
    for index, manifest in enumerate(manifests):
        record = pending_intent(identity=f"parent-request-{index}")
        record = record.model_copy(
            update={
                "observation": observed(
                    record,
                    resource_id=ResourceId(root=f"parent-resource-{index}"),
                    accepted=True,
                    children=tuple(ResourceId(root=f"child-{number}") for number in manifest),
                ),
            }
        )
        originals.append(record)
    state = recovering_state(*originals)
    event = RecoveryStarted(epoch=2, now_at=11.0)
    result = step(reload(state), event)
    assert result == step(state, event)
    children = result.state.intents.children
    expected_children = sorted({number for manifest in manifests for number in manifest})
    assert [child.resource_id.root for child in children] == [
        f"child-{number}" for number in expected_children
    ]
    for number, child in zip(expected_children, children, strict=True):
        parents = [index for index, manifest in enumerate(manifests) if number in manifest]
        assert child.source_requests == tuple(
            RequestId(root=f"parent-request-{index}") for index in parents
        )
        assert child.parent_resources == tuple(
            ResourceId(root=f"parent-resource-{index}") for index in parents
        )
        assert child.observation is None
    assert result.state.intents.intents[: len(originals)] == tuple(originals)
    inspections = [request for request in result.requests if isinstance(request, InspectRequest)]
    assert len(inspections) == len(originals) + sum(len(set(manifest)) for manifest in manifests)
    assert len({request.request_id for request in inspections}) == len(inspections)
    assert all(check.resolution == "pending" for check in result.state.intents.recovery.checks)
    replay = step(reload(result.state), event)
    assert replay.requests == ()
    assert replay.state.intents == result.state.intents
    assert replay.events == ()


@given(
    st.tuples(
        st.text(alphabet="abc123", min_size=1, max_size=6),
        st.text(alphabet="abc123", min_size=1, max_size=6),
        st.text(alphabet="abc123", min_size=1, max_size=6),
    )
)
def test_composite_resource_ids_cannot_alias_another_targets_cleanup(
    pieces: tuple[str, str, str],
) -> None:
    """Colon boundaries distinguish (a:b,c) from (a,b:c) cleanup identities."""
    first, middle, last = pieces
    originals = (
        pending_intent(identity=f"{first}:{middle}"),
        pending_intent(identity=first),
    )
    resources = (ResourceId(root=last), ResourceId(root=f"{middle}:{last}"))
    records = tuple(
        original.model_copy(
            update={"observation": observed(original, accepted=True, resource_id=resource)}
        )
        for original, resource in zip(originals, resources, strict=True)
    )
    state = step(recovering_state(*records), RecoveryStarted(epoch=1, now_at=11.0)).state
    cancels = []
    for original, resource in zip(records, resources, strict=True):
        event = ReconciliationDeadline(request_id=original.request_id, now_at=100.0)
        result = step(reload(state), event)
        requests = [
            request for request in result.requests if isinstance(request, CancelOwnedResource)
        ]
        assert len(requests) == 1
        assert requests[0].resource_id == resource
        assert requests[0].target == original.request_id
        cancels.append(requests[0])
        state = reload(result.state)
    assert cancels[0].request_id != cancels[1].request_id
    assert state.intents.intents[:2] == records


def with_sibling_history(state: CoreState, amounts: tuple[int, int]) -> CoreState:
    """Nonempty persisted accounting and mutually exclusive historical input receipts."""
    charged, refunded = amounts
    artifact = ArtifactRef(artifact_id=ArtifactId(root="historical-proof"), digest="proof-digest")
    receipt = ChargeReceipt(
        charge_id=ChargeId(root="admission-history"),
        kind=ChargeKind.ADMISSION,
        charged=charged,
        refunded=refunded,
        refund_sources=(RequestId(root="historical-refund"),) if refunded else (),
        historical_proof=artifact,
    )
    owner = AttemptView(
        attempt_id=AttemptId(root="current-owner"),
        item_id=ItemId(root="current-item"),
        generation=2,
        phase=AttemptPhase.ACTIVE,
        workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline),
        budget=AttemptBudget(),
        admission_id=DecisionId(root="current-admission"),
        charges=(receipt,),
    )
    historical = turn_intent()
    assert isinstance(historical.request, DispatchTurn)
    invocation = InvocationRef(
        session_id=historical.request.turn.session.session_id,
        invocation_id=historical.request.turn.invocation_id,
        generation=0,
    )
    acceptance = observed(
        historical,
        request_id=RequestId(root="historical-turn"),
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        resource_id=ResourceId(root="historical-session"),
        terminal=True,
        released=True,
        children_complete=True,
    )
    inputs = tuple(
        SessionInput(
            input_id=InputId(root=name),
            target=ScopeInputTarget(scope=historical.request.scope)
            if name == "pending"
            else InvocationInputTarget(invocation=invocation),
            artifact=artifact,
            received_at=0.0,
            sequence=index,
        )
        for index, name in enumerate(("delivered", "dropped", "pending"))
    )
    records = (
        InputRecord(
            input=inputs[0],
            reserved_to=invocation,
            receipt=InputDelivered(
                input_id=inputs[0].input_id, invocation=invocation, observation=acceptance
            ),
        ),
        InputRecord(
            input=inputs[1],
            receipt=InputDropped(
                input_id=inputs[1].input_id,
                target=inputs[1].target,
                reason=InputDropReason.INVOCATION_TERMINAL,
                at=10.0,
            ),
        ),
        InputRecord(input=inputs[2]),
    )
    sessions = SessionsState(
        invocations=(
            Invocation(
                invocation=invocation,
                scope=historical.request.scope,
                turn=historical.request.turn,
                phase=SessionPhase.TERMINAL,
                observation=acceptance,
            ),
        ),
        inputs=records,
        run_charges=(
            ChargeReceipt(
                charge_id=ChargeId(root="turn-history"),
                kind=ChargeKind.TURN,
                invocation_id=invocation.invocation_id,
                charged=1,
                historical_proof=artifact,
            ),
        ),
    )
    return state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": sessions,
            "run": state.run.model_copy(
                update={"limits": Limits(max_attempts=200, max_refunds=200)}
            ),
        }
    )


@given(
    amounts=st.integers(1, 100).flatmap(
        lambda charged: st.tuples(st.just(charged), st.integers(0, charged))
    ),
    sequence=st.lists(
        st.tuples(st.sampled_from(["startup", "deadline"]), st.integers(0, 4), st.integers(0, 150)),
        min_size=1,
        max_size=20,
    ),
)
def test_recovery_preserves_nonempty_sibling_accounting_and_terminal_inputs(
    amounts: tuple[int, int], sequence: list[tuple[str, int, int]]
) -> None:
    """Legacy composed-recovery bounds and delivery/drop finality remain authoritative."""
    original = pending_intent()
    state = with_sibling_history(recovering_state(original), amounts)
    initial_attempts = state.attempts
    initial_sessions = state.sessions
    for kind, epoch, now_at in sequence:
        event = (
            RecoveryStarted(epoch=epoch, now_at=float(now_at))
            if kind == "startup"
            else ReconciliationDeadline(request_id=original.request_id, now_at=float(now_at))
        )
        result = step(reload(state), event)
        assert result == step(state, event)
        assert result.state.attempts == initial_attempts
        assert result.state.sessions == initial_sessions
        assert result.state.run.receipts == ()
        assert result.events == ()
        projection = project(result.state)
        assert projection.scheduling.charged == amounts[0]
        assert projection.scheduling.refunded == amounts[1]
        assert 0 <= projection.scheduling.refunded <= projection.scheduling.charged
        assert len([record for record in projection.inputs if record.receipt is not None]) == 2
        assert projection.inputs[0].receipt == initial_sessions.inputs[0].receipt
        assert projection.inputs[1].receipt == initial_sessions.inputs[1].receipt
        assert projection.inputs[2].receipt is None
        state = reload(result.state)


@pytest.mark.parametrize("case", ["orphan", "foreign-scope", "different-episode"])
def test_recovery_rejects_child_sources_without_canonical_ownership(case: str) -> None:
    """A child cannot derive authority from an absent, foreign or reused episode."""
    first = pending_intent(identity="first")
    second = pending_intent(identity="second")
    if case == "foreign-scope":
        second = second.model_copy(
            update={
                "request": second.request.model_copy(
                    update={"scope": second.request.scope.model_copy(update={"generation": 1})}
                )
            }
        )
    elif case == "different-episode":
        first = first.model_copy(
            update={
                "request": first.request.model_copy(update={"admission_id": DecisionId(root="old")})
            }
        )
        second = second.model_copy(
            update={
                "request": second.request.model_copy(
                    update={"admission_id": DecisionId(root="new")}
                )
            }
        )
    sources = (
        (RequestId(root="missing"),) if case == "orphan" else (first.request_id, second.request_id)
    )
    lease = ChildLease(
        resource_id=ResourceId(root="child"), scope=first.request.scope, source_requests=sources
    )
    state = recovering_state(first, second)
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"children": (lease,)})}
    )
    before = state.model_dump_json()
    with pytest.raises(ContractError, match="children"):
        step(reload(state), RecoveryStarted(epoch=1, now_at=0.0))
    assert state.model_dump_json() == before


@given(
    status=st.sampled_from(list(ObservationStatus)),
    terminal=st.booleans(),
    released=st.booleans(),
    complete=st.booleans(),
)
def test_known_resource_deadline_requires_conclusive_release_before_skipping_cancel(
    status: ObservationStatus, *, terminal: bool, released: bool, complete: bool
) -> None:
    """F5: raw released flags and cancellation acknowledgement do not prove drain."""
    original = pending_intent(phase=IntentPhase.COMPLETED)
    resource = ResourceId(root="known-resource")
    original = original.model_copy(
        update={
            "observation": observed(
                original,
                status=status,
                resource_id=resource,
                accepted=True,
                terminal=terminal,
                released=released,
                children_complete=complete,
            ),
        }
    )
    state = recovering_state(original, pending_intent(identity="anchor"))
    started = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    result = step(
        reload(started.state), ReconciliationDeadline(request_id=original.request_id, now_at=100.0)
    )
    positive_release = (
        terminal
        and released
        and complete
        and status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    )
    resources = {
        request.resource_id
        for request in result.requests
        if isinstance(request, CancelOwnedResource)
    }
    assert resources == (set() if positive_release else {resource})
    assert result.state.intents.intents[0] == original
    assert result.state.sessions == state.sessions
    assert result.state.attempts == state.attempts


@given(st.integers(min_value=101, max_value=1500))
def test_reordered_time_cannot_expire_new_inspection_and_cancellation_bounds(
    logical_now: int,
) -> None:
    """Restart preserves the overdue original deadline and uses current supplied time."""
    original = pending_intent()
    resource = ResourceId(root="live-resource")
    original = original.model_copy(
        update={"observation": observed(original, accepted=True, resource_id=resource)}
    )
    state = recovering_state(original)
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"now_at": float(logical_now), "deadline_at": 2000.0}
            )
        }
    )
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=1.0))
    assert result.state.run.now_at == float(logical_now)
    assert all(
        request.deadline_at == logical_now + state.run.limits.reconciliation_bound
        for request in result.requests
    )
    assert result.state.intents.intents[0].reconcile_deadline_at == original.reconcile_deadline_at
    blocked = step(
        reload(result.state), ReconciliationDeadline(request_id=original.request_id, now_at=100.0)
    )
    assert blocked.state.intents.recovery.phase == RecoveryPhase.BLOCKED
    for request in blocked.requests:
        bound = (
            state.run.limits.cancellation_bound
            if isinstance(request, CancelOwnedResource)
            else state.run.limits.reconciliation_bound
        )
        assert request.deadline_at == logical_now + bound
    assert any(isinstance(request, CancelOwnedResource) for request in blocked.requests)
    assert blocked.state.intents.intents[0] == original


def registered_intent(
    lifecycle: LifecycleClass, identity: str = "registered"
) -> tuple[Intent, OperationDescriptor]:
    """Canonical registry encoding for job and agent lifecycle payloads."""
    match lifecycle:
        case LifecycleClass.OWNED_JOB:
            payload = RegisteredJobRequest()
        case LifecycleClass.SESSION_TURN:
            original_turn = turn_intent()
            assert isinstance(original_turn.request, DispatchTurn)
            payload = RegisteredTurnRequest(turn=original_turn.request.turn)
        case _:
            raise AssertionError(lifecycle)
    descriptor = OperationDescriptor(
        kind=payload.kind,
        lifecycle=payload.lifecycle,
        request_schema=SchemaRef(name=f"{payload.kind}-request", version=1),
        outcome_schema=SchemaRef(name=f"{payload.kind}-outcome", version=1),
        resource_pool=PoolId(root="jobs") if lifecycle == LifecycleClass.OWNED_JOB else None,
        inspect=True,
        cancel=True,
    )
    codec = OperationRegistry(
        (
            OperationRegistration(
                descriptor=descriptor,
                request_model=type(payload),
                outcome_model=RegisteredOutcome,
                normalize_turn=normalize_registered_turn
                if lifecycle == LifecycleClass.SESSION_TURN
                else None,
            ),
        )
    )
    original = pending_intent(identity=identity)
    request = ExecuteRegisteredOperation(
        request_id=original.request_id,
        scope=original.request.scope,
        deadline_at=100.0,
        operation_id=OperationId(root=f"operation-{identity}"),
        operation=codec.encode(payload),
        retry_limit=0,
    )
    return original.model_copy(update={"request": request, "lifecycle": lifecycle}), descriptor


@given(
    lifecycle=st.sampled_from(list(LifecycleClass)),
    phase=st.sampled_from(list(IntentPhase)),
    flags=st.tuples(st.booleans(), st.booleans(), st.booleans(), st.booleans()),
)
def test_ambiguous_acceptance_never_replays_any_lifecycle_class(
    lifecycle: LifecycleClass, phase: IntentPhase, flags: tuple[bool, bool, bool, bool]
) -> None:
    """F3/F4/F5 mechanism: Unknown cannot become accepted, terminated or released."""
    terminal, released, accepted, complete = flags
    if lifecycle == LifecycleClass.SESSION_TURN:
        original = turn_intent()
        registry = ()
    elif lifecycle == LifecycleClass.OWNED_JOB:
        original, descriptor = registered_intent(lifecycle)
        registry = (descriptor,)
    elif lifecycle == LifecycleClass.QUERY:
        original = pending_intent()
        request = InspectRequest(
            request_id=original.request_id,
            scope=original.request.scope,
            target=RequestId(root="external"),
            deadline_at=100.0,
        )
        original = original.model_copy(update={"request": request, "lifecycle": lifecycle})
        registry = ()
    else:
        original = pending_intent()
        registry = ()
    original = original.model_copy(
        update={
            "phase": phase,
            "observation": observed(
                original,
                accepted=accepted,
                terminal=terminal,
                released=released,
                children_complete=complete,
            ),
        }
    )
    state = recovering_state(original, pending_intent(identity="anchor"))
    state = with_sibling_history(state.model_copy(update={"registry": registry}), (3, 1))
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check
        for check in result.state.intents.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == "pending"
    assert any(
        isinstance(request, InspectRequest) and request.target == original.request_id
        for request in result.requests
    )
    assert all(isinstance(request, InspectRequest) for request in result.requests)
    assert result.state.intents.intents[0] == original
    assert result.state.sessions == state.sessions
    assert result.state.attempts == state.attempts
    assert result.events == ()


@pytest.mark.parametrize("cleanup", ["discard", "close"])
def test_stale_retirement_recovery_cannot_release_newer_generation_or_episode(cleanup: str) -> None:
    """Translate legacy test_stale_withdrawal_recovery_cannot_release_a_newer_generation."""
    original = pending_intent(identity="old-retirement")
    old_attempt = AttemptRef(attempt_id=AttemptId(root="current-owner"), generation=1)
    scope = Scope(owner=old_attempt.attempt_id, generation=old_attempt.generation)
    request_model = DiscardWorkspace if cleanup == "discard" else CloseAttemptScope
    request = request_model(
        request_id=original.request_id,
        scope=scope,
        attempt=old_attempt,
        admission_id=DecisionId(root="old-admission"),
        deadline_at=100.0,
    )
    original = original.model_copy(update={"request": request})
    original = original.model_copy(
        update={
            "observation": observed(
                original,
                admission_id=request.admission_id,
                resource_id=ResourceId(root="old-resource"),
                accepted=True,
            )
        }
    )
    state = with_sibling_history(recovering_state(original), (2, 1))
    owner = state.attempts.attempts[0]
    assert owner.admission_id is not None
    slot = Slot(
        attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
        admission_id=owner.admission_id,
        admitted_at=0.0,
    )
    state = state.model_copy(update={"scheduling": SchedulingState(slots=(slot,))})
    recovered = step(reload(state), RecoveryStarted(epoch=2, now_at=11.0))
    assert all(isinstance(request, InspectRequest) for request in recovered.requests)
    blocked = step(
        reload(recovered.state),
        ReconciliationDeadline(request_id=original.request_id, now_at=100.0),
    )
    assert blocked.state.attempts == state.attempts
    assert blocked.state.scheduling == state.scheduling
    assert blocked.state.attempts.attempts[0].generation == 2
    assert blocked.state.attempts.attempts[0].phase == AttemptPhase.ACTIVE
    assert all(
        output.scope == scope and output.admission_id == request.admission_id
        for output in blocked.requests
    )
    assert all(
        isinstance(request, BlockIntent | InspectRequest | CancelOwnedResource)
        for request in blocked.requests
    )
    assert blocked.events == ()
    assert blocked.state.intents.intents[0] == original


@given(
    case=st.sampled_from(
        [
            "exact",
            "foreign-operation",
            "foreign-generation",
            "missing-normalization",
            "foreign-turn",
            "foreign-session",
            "foreign-invocation",
        ]
    )
)
def test_registered_session_turn_requires_its_normalized_owner_identity(case: str) -> None:
    """Registered agent work reattaches through its declared session owner."""
    original, descriptor = registered_intent(LifecycleClass.SESSION_TURN)
    assert isinstance(original.request, ExecuteRegisteredOperation)
    operation_id = original.request.operation_id
    decision_id = DecisionId(root=operation_id.root)
    original = original.model_copy(
        update={"request": original.request.model_copy(update={"decision_id": decision_id})}
    )
    physical = ResourceId(root="registered-session")
    observation = observed(
        original,
        status=ObservationStatus.PENDING,
        accepted=True,
        resource_id=physical,
        children_complete=True,
    )
    original = original.model_copy(update={"observation": observation})
    turn = turn_intent()
    assert isinstance(turn.request, DispatchTurn)
    owner = Invocation(
        invocation=InvocationRef(
            session_id=SessionId(root="foreign-session")
            if case == "foreign-session"
            else turn.request.turn.session.session_id,
            invocation_id=InvocationId(root="foreign-invocation")
            if case == "foreign-invocation"
            else turn.request.turn.invocation_id,
            generation=1 if case == "foreign-generation" else 0,
        ),
        scope=original.request.scope,
        turn=turn.request.turn.model_copy(update={"deadline_at": 99.0})
        if case == "foreign-turn"
        else turn.request.turn,
        phase=SessionPhase.EXECUTING,
        registered_operation=OperationId(root="foreign")
        if case == "foreign-operation"
        else operation_id,
        observation=observation,
    )
    state = recovering_state(original, pending_intent(identity="anchor"))
    state = state.model_copy(
        update={"registry": (descriptor,), "sessions": SessionsState(invocations=(owner,))}
    )
    codec = OperationRegistry(
        (
            OperationRegistration(
                descriptor=descriptor,
                request_model=RegisteredTurnRequest,
                outcome_model=RegisteredOutcome,
                normalize_turn=normalize_registered_turn,
            ),
        )
    )
    assert isinstance(original.request, ExecuteRegisteredOperation)
    decision = codec.validate_decision(
        Operation(
            decision_id=decision_id,
            scope=original.request.scope,
            request=codec.decode(original.request.operation),
            deadline_at=100.0,
        )
    )
    receipt = DecisionReceipt(
        decision_id=decision_id,
        decision=decision,
        payload_digest="persisted-normalization",
        feedback=Accepted(decision_id=decision_id, request_ids=(original.request_id,)),
        request_ids=(original.request_id,),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"receipts": () if case == "missing-normalization" else (receipt,)}
            )
        }
    )
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check
        for check in result.state.intents.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == ("reattached" if case == "exact" else "pending")
    assert result.state.sessions == state.sessions


@example(case="foreign-episode")
@given(
    case=st.sampled_from(
        [
            "exact",
            "no-observation",
            "unknown",
            "unaccepted",
            "incomplete",
            "foreign-request",
            "foreign-scope",
            "foreign-episode",
        ]
    )
)
def test_discovered_child_transfers_only_to_an_exact_proven_typed_owner(case: str) -> None:
    """Recovery transfers a live descendant atomically, preserving owner facts."""
    parent = pending_intent(identity="parent", phase=IntentPhase.COMPLETED)
    old_admission = DecisionId(root="old-episode")
    scope = Scope(owner=AttemptId(root="child-owner"), generation=0)
    parent = parent.model_copy(
        update={
            "request": parent.request.model_copy(
                update={"scope": scope, "admission_id": old_admission}
            )
        }
    )
    child = ResourceId(root="child")
    parent = parent.model_copy(
        update={
            "observation": observed(
                parent,
                accepted=True,
                admission_id=old_admission,
                resource_id=ResourceId(root="parent-resource"),
                status=ObservationStatus.SUCCEEDED,
                terminal=True,
                released=True,
                children_complete=True,
                children=(child,),
            ),
        }
    )
    submission, descriptor = registered_intent(
        LifecycleClass.OWNED_JOB, identity="child-submission"
    )
    assert isinstance(submission.request, ExecuteRegisteredOperation)
    operation_id = submission.request.operation_id
    admission_id = DecisionId(root="new-episode") if case == "foreign-episode" else old_admission
    submission = submission.model_copy(
        update={
            "request": submission.request.model_copy(
                update={"scope": scope, "admission_id": admission_id}
            )
        }
    )
    canonical = observed(
        submission,
        status=ObservationStatus.PENDING,
        resource_id=child,
        accepted=True,
        children_complete=True,
        admission_id=admission_id,
    )
    submission = submission.model_copy(update={"observation": canonical})
    proof = canonical.model_copy(
        update={
            "status": ObservationStatus.UNKNOWN if case == "unknown" else ObservationStatus.PENDING,
            "accepted": case != "unaccepted",
            "children_complete": case != "incomplete",
            "request_id": RequestId(root="foreign")
            if case == "foreign-request"
            else submission.request_id,
        }
    )
    owner = RegisteredOwnedJob(
        operation_id=operation_id,
        request_id=submission.request_id,
        resource_id=child,
        scope=parent.request.scope.model_copy(update={"generation": 1})
        if case == "foreign-scope"
        else parent.request.scope,
        resource_pool=PoolId(root="jobs"),
        status=ObservationStatus.PENDING,
        observation=None if case == "no-observation" else proof,
    )
    state = recovering_state(parent, submission, pending_intent(identity="anchor"))
    state = state.model_copy(
        update={"registry": (descriptor,), "evaluation": EvaluationState(registered_jobs=(owner,))}
    )
    if case == "foreign-scope":
        before = state.model_dump_json()
        with pytest.raises(ContractError, match="children"):
            step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
        assert state.model_dump_json() == before
        return
    for epoch in (1, 2):
        result = step(reload(state), RecoveryStarted(epoch=epoch, now_at=11.0))
        check = next(
            check
            for check in result.state.intents.recovery.checks
            if check.target == parent.request_id
        )
        assert check.resolution == ("terminal" if case == "exact" else "pending")
        if case == "exact":
            assert result.state.intents.children == ()
        else:
            assert len(result.state.intents.children) == 1
            lease = result.state.intents.children[0]
            assert lease.resource_id == child
            assert lease.source_requests == (parent.request_id,)
            assert lease.observation is None
            assert any(
                isinstance(request, InspectRequest)
                and request.resource_id == child
                and request.target == parent.request_id
                for request in result.requests
            )
        assert result.state.evaluation == state.evaluation
        assert result.state.intents.intents[:2] == (parent, submission)
        assert result.state.intents.recovery.phase == RecoveryPhase.RECOVERING
        assert result.events == ()
        state = reload(result.state)


@given(admission=st.sampled_from(["unknown", "closed", "reopened"]))
def test_scope_reopen_unknown_outcome_preserves_fences_without_resume(admission: str) -> None:
    """F5 reopen: successful command execution cannot resolve Unknown admission."""
    descriptor = OperationDescriptor(
        kind="evaluation.scope.reopen",
        request_schema=SchemaRef(name="scope-reopen", version=1),
        outcome_schema=SchemaRef(name="scope-reopened", version=1),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        normalization=OperationNormalizationKind.SCOPE_REOPEN,
        inspect=True,
    )
    codec = OperationRegistry(
        (
            OperationRegistration(
                descriptor=descriptor,
                request_model=ScopedAdmissionReopen,
                outcome_model=ScopedAdmissionReopenOutcome,
                normalize_scope_reopen=normalize_scope_reopen,
            ),
        )
    )
    state = with_sibling_history(recovering_state(pending_intent(identity="anchor")), (2, 1))
    owner = state.attempts.attempts[0]
    assert owner.admission_id is not None
    target = AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)
    payload = ScopedAdmissionReopen(
        attempt=target,
        continuation_id=ContinuationId(root="parked-wait"),
        park_authority=RequestId(root="park-authority"),
        resolved_cancelled_jobs=(),
    )
    decision_id = owner.admission_id
    scope = Scope(owner=state.run.run_id, generation=0)
    decision = codec.validate_decision(
        Operation(decision_id=decision_id, scope=scope, request=payload, deadline_at=100.0)
    )
    wire = codec.encode(payload)
    original = pending_intent(identity="reopen", phase=IntentPhase.COMPLETED)
    request = ExecuteRegisteredOperation(
        request_id=original.request_id,
        scope=scope,
        decision_id=decision_id,
        admission_id=decision_id,
        deadline_at=100.0,
        operation_id=OperationId(root=decision_id.root),
        operation=wire,
        retry_limit=0,
    )
    observation = observed(
        original,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
        admission_id=decision_id,
    )
    outcome = ScopedAdmissionReopenOutcome.model_validate(
        {
            "scope": Scope(owner=target.attempt_id, generation=target.generation),
            "admission": admission,
        }
    )
    original = Intent.model_validate(
        {
            **original.model_dump(),
            "request": request,
            "observation": observation,
            "outcome_schema": descriptor.outcome_schema,
            "outcome_json": codec.encode_outcome(wire.schema_ref, outcome),
        },
        context={"operation_registry": codec},
    )
    receipt = DecisionReceipt(
        decision_id=decision_id,
        decision=decision,
        payload_digest="persisted-reopen",
        feedback=Accepted(decision_id=decision_id, request_ids=(original.request_id,)),
        request_ids=(original.request_id,),
    )
    owner = owner.model_copy(
        update={
            "phase": AttemptPhase.ACQUIRING
            if admission == "unknown"
            else AttemptPhase.BLOCKED
            if admission == "closed"
            else AttemptPhase.ACTIVE
        }
    )
    slot = Slot(attempt=target, admission_id=decision_id, admitted_at=0.0)
    state = state.model_copy(
        update={
            "registry": (descriptor,),
            "run": state.run.model_copy(update={"receipts": (receipt,)}),
            "attempts": AttemptsState(attempts=(owner,)),
            "scheduling": SchedulingState(slots=(slot,)),
            "intents": state.intents.model_copy(
                update={"intents": (original, *state.intents.intents)}
            ),
        }
    )
    for epoch in (1, 2):
        result = step(reload(state), RecoveryStarted(epoch=epoch, now_at=11.0))
        check = next(
            check
            for check in result.state.intents.recovery.checks
            if check.target == original.request_id
        )
        assert check.resolution == ("pending" if admission == "unknown" else "terminal")
        assert result.state.attempts == state.attempts
        assert result.state.sessions == state.sessions
        assert result.state.scheduling == state.scheduling
        assert result.state.intents.intents[0] == original
        assert result.state.intents.intents[0].outcome == outcome
        assert all(isinstance(request, InspectRequest) for request in result.requests)
        assert result.events == ()
        assert result.state.run.status == RunStatus.PAUSED
        state = reload(result.state)


@given(
    case=st.sampled_from(
        ["exact", "foreign-request", "foreign-operation", "foreign-pool", "foreign-scope"]
    )
)
def test_registered_job_can_reattach_to_its_provisional_owner_without_inventing_ownership(
    case: str,
) -> None:
    """Positive root facts reconcile the existing owner while resource identity is pending."""
    original, descriptor = registered_intent(LifecycleClass.OWNED_JOB)
    assert isinstance(original.request, ExecuteRegisteredOperation)
    request = original.request
    observation = observed(
        original,
        accepted=True,
        resource_id=ResourceId(root="accepted-job"),
        status=ObservationStatus.PENDING,
        children_complete=True,
    )
    original = original.model_copy(update={"observation": observation})
    owner = RegisteredOwnedJob(
        operation_id=OperationId(root="foreign")
        if case == "foreign-operation"
        else request.operation_id,
        request_id=RequestId(root="foreign") if case == "foreign-request" else original.request_id,
        scope=request.scope.model_copy(update={"generation": 1})
        if case == "foreign-scope"
        else request.scope,
        resource_pool=PoolId(root="foreign") if case == "foreign-pool" else PoolId(root="jobs"),
    )
    state = recovering_state(original, pending_intent(identity="anchor"))
    state = state.model_copy(
        update={"registry": (descriptor,), "evaluation": EvaluationState(registered_jobs=(owner,))}
    )
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check
        for check in result.state.intents.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == ("reattached" if case == "exact" else "pending")
    assert result.state.evaluation == state.evaluation
    assert result.state.intents.intents[0] == original
    assert result.events == ()


@given(st.lists(st.integers(0, 100), min_size=1, max_size=30))
def test_generated_successor_deadlines_reconcile_original_root_without_recursive_requests(
    choices: list[int],
) -> None:
    """Fresh post-run timers reconcile the root without refreshing earlier successors."""
    original = pending_intent()
    state = recovering_state(original)
    state = state.model_copy(update={"run": state.run.model_copy(update={"deadline_at": 105.0})})
    emitted: set[RequestId] = set()
    for index, choice in enumerate(choices):
        records = state.intents.intents
        selected = records[choice % len(records)]
        event = ReconciliationDeadline(request_id=selected.request_id, now_at=float(110 + index))
        result = step(reload(state), event)
        assert result == step(state, event)
        assert result.state.intents.intents[0] == original
        assert result.state.intents.intents[: len(records)] == records
        assert result.state.intents.recovery.phase == RecoveryPhase.BLOCKED
        assert {check.target for check in result.state.intents.recovery.checks} == {
            original.request_id
        }
        for request in result.requests:
            assert isinstance(request, BlockIntent | InspectRequest)
            assert request.target == original.request_id
            assert request.request_id is not None
            assert request.request_id not in emitted
            assert request.deadline_at == (
                result.state.run.now_at + state.run.limits.reconciliation_bound
            )
            emitted.add(request.request_id)
        assert len(emitted) <= 2
        assert len(result.state.intents.intents) <= 3
        assert all(
            record.reconcile_deadline_at == record.request.deadline_at
            for record in result.state.intents.intents[1:]
        )
        assert result.events == ()
        state = reload(result.state)


def test_late_child_cleanup_survives_previous_parent_block_and_cancel_requests() -> None:
    """A reload with new child proof adds one cleanup, even when parent cleanup exists."""
    original = pending_intent()
    parent = ResourceId(root="parent")
    original = original.model_copy(
        update={"observation": observed(original, accepted=True, resource_id=parent)}
    )
    initial = step(
        recovering_state(original),
        ReconciliationDeadline(request_id=original.request_id, now_at=100.0),
    )
    parent_cancel = next(
        request for request in initial.requests if isinstance(request, CancelOwnedResource)
    )
    assert parent_cancel.request_id is not None
    child = ResourceId(root="late-child")
    lease = ChildLease(
        resource_id=child,
        scope=original.request.scope,
        source_requests=(original.request_id,),
        parent_resources=(parent,),
    )
    # Persisted late discovery is a public state input. Full observation delivery
    # remains blocked by the frozen intents A wrapper in this isolated slice.
    assert original.observation is not None
    latest = original.model_copy(
        update={
            "observation": original.observation.model_copy(
                update={"sequence": 2, "observed_at": 120.0, "children": (child,)}
            )
        }
    )
    state = initial.state.model_copy(
        update={
            "intents": initial.state.intents.model_copy(
                update={
                    "intents": (latest, *initial.state.intents.intents[1:]),
                    "children": (lease,),
                }
            )
        }
    )
    event = ReconciliationDeadline(request_id=parent_cancel.request_id, now_at=120.0)
    result = step(reload(state), event)
    assert len(result.requests) == 1
    cleanup = result.requests[0]
    assert isinstance(cleanup, CancelOwnedResource)
    assert cleanup.resource_id == child
    assert cleanup.target == original.request_id
    assert result.state.intents.children == (lease,)
    assert result.state.intents.intents[0] == latest
    repeated = step(reload(result.state), event)
    assert repeated.requests == ()
    assert repeated.state.intents == result.state.intents
    assert repeated.events == ()
