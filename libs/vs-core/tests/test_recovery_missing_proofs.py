"""Recovery never promotes absent resource or registry proof through public step."""

from hypothesis import example, given
from hypothesis import strategies as st

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    Accepted,
    Access,
    ArtifactId,
    ArtifactRef,
    AttemptId,
    AttemptRef,
    CloseSession,
    ContinuationId,
    CoreState,
    DecisionId,
    DecisionReceipt,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    EventCursor,
    EventId,
    ExecuteRegisteredOperation,
    HostFence,
    HostId,
    InspectRequest,
    Intent,
    IntentPhase,
    IntentsState,
    InvocationId,
    LifecycleClass,
    Observation,
    ObservationStatus,
    Operation,
    OperationDescriptor,
    OperationId,
    OperationNormalizationKind,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    RecoveryStarted,
    RequestId,
    ResourceId,
    ResumeSessionTurn,
    RevisionId,
    RevisionRef,
    RoleId,
    RunEnvelope,
    RunStatus,
    SchemaRef,
    Scope,
    ScopedAdmissionReopen,
    ScopedAdmissionReopenOutcome,
    ScopeReopenNormalization,
    SessionId,
    SessionPhase,
    SessionSpec,
    SessionsState,
    SessionView,
    StrategyState,
    TurnSpec,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    step,
)


def _state(record: Intent) -> CoreState:
    state = initial_state()
    anchor_request = EnsureSession(
        request_id=RequestId(root="anchor"),
        scope=record.request.scope,
        deadline_at=100.0,
        spec=SessionSpec(
            session_id=SessionId(root="anchor"),
            role_id=RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
    )
    assert anchor_request.request_id is not None
    anchor = Intent(
        request_id=anchor_request.request_id,
        request=anchor_request,
        payload_digest="anchor",
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=IntentPhase.DISPATCHED,
        reconcile_deadline_at=100.0,
    )
    return state.model_copy(
        update={
            "run": state.run.model_copy(update={"status": RunStatus.PAUSED}),
            "intents": IntentsState(intents=(record, anchor)),
        }
    )


@given(
    kind=st.sampled_from(["setup", "workspace", "close", "turn", "resume"]),
    identity=st.sampled_from(["exact", "missing", "wrong"]),
    status=st.sampled_from(
        [ObservationStatus.SUCCEEDED, ObservationStatus.FAILED, ObservationStatus.CANCELLED]
    ),
    released=st.booleans(),
    complete=st.booleans(),
)
@example(
    kind="setup",
    identity="missing",
    status=ObservationStatus.SUCCEEDED,
    released=False,
    complete=True,
)
@example(
    kind="turn",
    identity="missing",
    status=ObservationStatus.SUCCEEDED,
    released=True,
    complete=True,
)
def test_accepted_resource_bearing_requests_need_identified_release(
    kind: str, identity: str, status: ObservationStatus, *, released: bool, complete: bool
) -> None:
    """Setup and every turn require a named lease; required leases cannot be substituted."""
    scope = Scope(owner=initial_state().run.run_id, generation=0)
    request_id = RequestId(root="resource-operation")
    resource = ResourceId(root="exact-lease")
    spec = SessionSpec(
        session_id=SessionId(root="session"),
        role_id=RoleId(root="worker"),
        policy="reuse",
        lifetime="owner",
        access=Access.WRITE_ARTIFACTS,
    )
    setup = EnsureSession(
        request_id=request_id,
        scope=scope,
        deadline_at=100.0,
        spec=spec,
        required_resource=resource,
    )
    turn = TurnSpec(
        session=spec,
        invocation_id=InvocationId(root="invocation"),
        workspace=scope,
        prompts=(ArtifactRef(artifact_id=ArtifactId(root="prompt"), digest="digest"),),
        output_schema=SchemaRef(name="output", version=1),
        deadline_at=100.0,
        charge_class="free",
    )
    request = (
        setup
        if kind == "setup"
        else DispatchTurn(
            request_id=request_id,
            scope=scope,
            deadline_at=100.0,
            turn=turn,
        )
        if kind == "turn"
        else ResumeSessionTurn(
            request_id=request_id,
            scope=scope,
            deadline_at=100.0,
            turn=turn,
            continuation_id=ContinuationId(root="continuation"),
        )
    )
    if kind == "workspace":
        request = EnsureWorkspace(
            request_id=request_id,
            scope=scope,
            deadline_at=100.0,
            attempt=AttemptRef(attempt_id=AttemptId(root="attempt"), generation=0),
            plan=WorkspacePlan(
                mode=WorkspaceMode.ISOLATED_CHILD,
                base=RevisionRef(revision_id=RevisionId(root="base"), digest="base-digest"),
            ),
        )
    elif kind == "close":
        request = CloseSession(
            request_id=request_id,
            scope=scope,
            deadline_at=100.0,
            session_id=spec.session_id,
        )
    observation = Observation(
        event_id=EventId(root="proof"),
        request_id=request_id,
        scope=scope,
        sequence=1,
        observed_at=10.0,
        status=status,
        accepted=True,
        terminal=True,
        released=released,
        children_complete=complete,
        resource_id=resource
        if identity == "exact"
        else None
        if identity == "missing"
        else ResourceId(root="different-lease"),
    )
    record = Intent(
        request_id=request_id,
        request=request,
        payload_digest="resource-operation",
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE
        if kind in ("setup", "workspace", "close")
        else LifecycleClass.SESSION_TURN,
        phase=IntentPhase.COMPLETED,
        reconcile_deadline_at=100.0,
        observation=observation,
    )
    state = _state(record)
    result = step(state, RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check for check in result.state.intents.recovery.checks if check.target == request_id
    )
    identified = identity != "missing" and (kind != "setup" or identity == "exact")
    assert check.resolution == ("terminal" if identified and released and complete else "pending")
    assert any(
        isinstance(request, InspectRequest) and request.target == request_id
        for request in result.requests
    ) == (check.resolution == "pending")
    assert result.state.intents.intents[0] == record
    assert result.state.sessions == state.sessions


@given(
    status=st.sampled_from(
        [
            ObservationStatus.REJECTED,
            ObservationStatus.FAILED,
            ObservationStatus.CANCELLED,
        ]
    ),
    query=st.booleans(),
)
def test_resource_free_query_and_proven_nonacceptance_remain_terminal(
    status: ObservationStatus, *, query: bool
) -> None:
    """Absence is allowed for queries and separately proven never accepted setup."""
    scope = Scope(owner=initial_state().run.run_id, generation=0)
    request_id = RequestId(root="negative-operation")
    request = (
        InspectRequest(
            request_id=request_id,
            scope=scope,
            deadline_at=100.0,
            target=RequestId(root="target"),
        )
        if query
        else EnsureSession(
            request_id=request_id,
            scope=scope,
            deadline_at=100.0,
            required_resource=ResourceId(root="existing-lease"),
            spec=SessionSpec(
                session_id=SessionId(root="session"),
                role_id=RoleId(root="worker"),
                policy="reuse",
                lifetime="owner",
                access=Access.WRITE_ARTIFACTS,
            ),
        )
    )
    record = Intent(
        request_id=request_id,
        request=request,
        payload_digest="negative-operation",
        lifecycle=LifecycleClass.QUERY if query else LifecycleClass.IDEMPOTENT_WRITE,
        phase=IntentPhase.COMPLETED,
        reconcile_deadline_at=100.0,
        observation=Observation(
            event_id=EventId(root="negative-proof"),
            request_id=request_id,
            scope=scope,
            sequence=1,
            observed_at=10.0,
            status=status,
            accepted=False,
            terminal=True,
            released=False,
            children_complete=True,
        ),
    )
    result = step(_state(record), RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check for check in result.state.intents.recovery.checks if check.target == request_id
    )
    assert check.resolution == "terminal"


def _reopen_normalization(request: OperationRequest) -> ScopeReopenNormalization:
    assert isinstance(request, ScopedAdmissionReopen)
    return ScopeReopenNormalization(
        attempt=request.attempt,
        continuation_id=request.continuation_id,
        park_authority=request.park_authority,
        resolved_cancelled_jobs=request.resolved_cancelled_jobs,
    )


@given(
    admission=st.sampled_from(["unknown", "closed", "reopened"]),
    metadata=st.sampled_from(["exact", "missing", "request_schema", "outcome_schema", "lifecycle"]),
)
@example(admission="unknown", metadata="missing")
def test_absent_scope_reopen_descriptor_never_proves_admission(
    admission: str, metadata: str
) -> None:
    """Registered codec knowledge cannot substitute for absent durable registry metadata."""
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
                normalize_scope_reopen=_reopen_normalization,
            ),
        )
    )
    scope = Scope(owner=initial_state().run.run_id, generation=0)
    payload = ScopedAdmissionReopen(
        attempt=AttemptRef(attempt_id=AttemptId(root="parked"), generation=3),
        continuation_id=ContinuationId(root="continuation"),
        park_authority=RequestId(root="park"),
        resolved_cancelled_jobs=(),
    )
    decision_id = DecisionId(root="reopen-decision")
    decision = codec.validate_decision(
        Operation(
            decision_id=decision_id,
            scope=scope,
            request=payload,
            deadline_at=100.0,
        )
    )
    wire = codec.encode(payload)
    request_id = RequestId(root="reopen-request")
    request = ExecuteRegisteredOperation(
        request_id=request_id,
        scope=scope,
        deadline_at=100.0,
        decision_id=decision_id,
        operation_id=OperationId(root="reopen-operation"),
        operation=wire,
        retry_limit=0,
    )
    outcome = ScopedAdmissionReopenOutcome.model_validate(
        {
            "scope": Scope(owner=payload.attempt.attempt_id, generation=3),
            "admission": admission,
        }
    )
    record = Intent.model_validate(
        {
            "request_id": request_id,
            "request": request,
            "payload_digest": "reopen",
            "lifecycle": LifecycleClass.IDEMPOTENT_WRITE,
            "phase": IntentPhase.COMPLETED,
            "reconcile_deadline_at": 100.0,
            "outcome_schema": descriptor.outcome_schema,
            "outcome_json": codec.encode_outcome(wire.schema_ref, outcome),
            "observation": Observation(
                event_id=EventId(root="reopen-proof"),
                request_id=request_id,
                scope=scope,
                sequence=1,
                observed_at=10.0,
                status=ObservationStatus.SUCCEEDED,
                accepted=True,
                terminal=True,
                released=True,
                children_complete=True,
            ),
        },
        context={"operation_registry": codec},
    )
    retained_descriptor = descriptor
    if metadata in ("request_schema", "outcome_schema"):
        retained_descriptor = descriptor.model_copy(
            update={
                metadata: SchemaRef(name="incompatible-schema", version=2),
            }
        )
    elif metadata == "lifecycle":
        retained_descriptor = OperationDescriptor(
            kind=descriptor.kind,
            request_schema=descriptor.request_schema,
            outcome_schema=descriptor.outcome_schema,
            lifecycle=LifecycleClass.QUERY,
            inspect=True,
        )
    state = _state(record)
    state = state.model_copy(
        update={
            "registry": () if metadata == "missing" else (retained_descriptor,),
            "run": state.run.model_copy(
                update={
                    "receipts": (
                        DecisionReceipt(
                            decision_id=decision_id,
                            decision=decision,
                            payload_digest="reopen",
                            feedback=Accepted(decision_id=decision_id, request_ids=(request_id,)),
                            request_ids=(request_id,),
                        ),
                    )
                }
            ),
        }
    )
    reloaded = state
    if metadata in ("exact", "missing"):
        envelope = RunEnvelope[StrategyState](
            schema_version=ENVELOPE_SCHEMA_VERSION,
            core=state,
            fence=HostFence(host_id=HostId(root="host"), epoch=0),
            event_cursor=EventCursor(sequence=0),
            strategy_id=state.run.declaration.strategy_id,
            state_schema=state.run.declaration.state_schema,
            strategy=StrategyState(schema_version=state.run.declaration.state_schema.version),
        )
        reloaded = codec.decode_envelope(
            RunEnvelope[StrategyState], codec.encode_envelope(envelope)
        ).core
        assert reloaded == state
    result = step(reloaded, RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check for check in result.state.intents.recovery.checks if check.target == request_id
    )
    assert check.resolution == (
        "terminal" if metadata == "exact" and admission != "unknown" else "pending"
    )
    assert any(
        isinstance(request, InspectRequest) and request.target == request_id
        for request in result.requests
    ) == (check.resolution == "pending")
    assert result.state.intents.intents[0] == record


@given(
    kind=st.sampled_from(["setup", "turn", "resume", "close"]),
    identity=st.sampled_from(["exact", "wrong"]),
    correlated=st.booleans(),
)
def test_terminal_session_resource_matches_request_correlated_lease(
    kind: str, identity: str, *, correlated: bool
) -> None:
    """Only a session explicitly carrying this request supplies physical correspondence."""
    state = initial_state()
    scope = Scope(owner=state.run.run_id, generation=0)
    request_id = RequestId(root="session-release")
    resource = ResourceId(root="expected-lease")
    spec = SessionSpec(
        session_id=SessionId(root="session"),
        role_id=RoleId(root="worker"),
        policy="reuse",
        lifetime="owner",
        access=Access.WRITE_ARTIFACTS,
    )
    turn = TurnSpec(
        session=spec,
        invocation_id=InvocationId(root="turn"),
        workspace=scope,
        prompts=(),
        output_schema=SchemaRef(name="output", version=1),
        deadline_at=100.0,
        charge_class="free",
    )
    requests = {
        "setup": EnsureSession(request_id=request_id, scope=scope, deadline_at=100.0, spec=spec),
        "close": CloseSession(
            request_id=request_id, scope=scope, deadline_at=100.0, session_id=spec.session_id
        ),
        "turn": DispatchTurn(request_id=request_id, scope=scope, deadline_at=100.0, turn=turn),
        "resume": ResumeSessionTurn(
            request_id=request_id,
            scope=scope,
            deadline_at=100.0,
            turn=turn,
            continuation_id=ContinuationId(root="continuation"),
        ),
    }
    record = Intent(
        request_id=request_id,
        request=requests[kind],
        payload_digest="session-release",
        lifecycle=LifecycleClass.SESSION_TURN
        if kind in ("turn", "resume")
        else LifecycleClass.IDEMPOTENT_WRITE,
        phase=IntentPhase.COMPLETED,
        reconcile_deadline_at=100.0,
        observation=Observation(
            event_id=EventId(root="release"),
            request_id=request_id,
            scope=scope,
            resource_id=resource if identity == "exact" else ResourceId(root="different-lease"),
            sequence=1,
            observed_at=10.0,
            status=ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=True,
            children_complete=True,
        ),
    )
    state = _state(record).model_copy(
        update={
            "sessions": SessionsState(
                sessions=(
                    SessionView(
                        spec=spec,
                        scope=scope,
                        generation=0,
                        phase=SessionPhase.IDLE,
                        accepted=True,
                        resource_id=resource,
                        pending_intents=(request_id,) if correlated else (),
                    ),
                )
            )
        }
    )
    result = step(state, RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check for check in result.state.intents.recovery.checks if check.target == request_id
    )
    assert check.resolution == ("pending" if correlated and identity == "wrong" else "terminal")
    assert result.state.sessions == state.sessions
