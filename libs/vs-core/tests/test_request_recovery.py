"""Recovery proofs and replay through the public immutable lifecycle API."""

import json
from hashlib import sha256
from itertools import product
from typing import ClassVar, Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

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
    BlockRequest,
    CancelOwnedResource,
    Capabilities,
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
    Failed,
    FailureReason,
    HostEpoch,
    HostFence,
    HostId,
    HostRenewed,
    InputDelivered,
    InputDropped,
    InputDropReason,
    InputId,
    InputRecord,
    InspectRequest,
    Invocation,
    InvocationId,
    InvocationInputTarget,
    InvocationRef,
    ItemId,
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
    OperationSchemaRef,
    OperationWire,
    PoolId,
    ReconciliationDeadline,
    RecordsState,
    RecoveryBarrier,
    RecoveryPhase,
    RecoveryStarted,
    RegisteredOwnedJob,
    ReissueProof,
    RequestId,
    RequestObserved,
    RequestPhase,
    RequestRecord,
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
    TargetObservation,
    Transition,
    TurnSpec,
    Until,
    Value,
    WaitOn,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    orphan_waits,
    project,
    step,
)
from vs_core.api import downtime as downtime_of
from vs_core.api.testing import sample_limits
from vs_sim.api.testing import examples

from .restart import restarts, unaddressed


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


class RegisteredWriteRequest(OperationRequest):
    """A registered idempotent write, the one kind whose resource core cancels itself."""

    kind: Literal["test.write"] = "test.write"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = RegisteredOutcome


WRITE_SCHEMA = OperationSchemaRef(
    kind="test.write",
    request_schema=SchemaRef(name="write", version=1),
    outcome_schema=SchemaRef(name="write-result", version=1),
    lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
)
WRITE_DESCRIPTOR = OperationDescriptor(**WRITE_SCHEMA.model_dump(), inspect=True, cancel=True)

REGISTERED_REQUESTS = {
    "test.write": RegisteredWriteRequest,
    "cluster.owned": RegisteredJobRequest,
    "agent.turn": RegisteredTurnRequest,
    "evaluation.scope.reopen": ScopedAdmissionReopen,
}


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


def pending_operation(
    identity: str = "original", phase: RequestPhase = RequestPhase.DISPATCHED
) -> RequestRecord:
    """A persisted registered operation, the one kind whose resource core cancels itself."""
    record = pending_record(identity, phase)
    request = ExecuteRegisteredOperation(
        request_id=record.request_id,
        scope=record.request.scope,
        operation_id=OperationId(root=identity),
        operation=OperationWire(schema_ref=WRITE_SCHEMA, payload_json="{}"),
        retry_limit=0,
    )
    return record.model_copy(update={"request": request, "payload_digest": fixture_digest(request)})


def recovering_state(*records: RequestRecord) -> CoreState:
    """Reloaded state starts behind the required barrier, retaining all ledgers.

    A record of ``pending_operation`` brings its kind's registration, so a reload decodes it.
    """
    state = initial_state()
    writes = any(
        isinstance(record.request, ExecuteRegisteredOperation)
        and record.request.operation.schema_ref == WRITE_SCHEMA
        for record in records
    )
    return state.model_copy(
        update={
            "registry": (WRITE_DESCRIPTOR,) if writes else state.registry,
            "records": RecordsState(hosts=(), records=records, recovery=RecoveryBarrier()),
            "run": state.run.model_copy(update={"status": RunStatus.PAUSED}),
        }
    )


def fixture_digest(value: Value) -> str:
    """Canonical bytes for scalar and ordered fixture contracts."""
    return sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def with_registered_origins(state: CoreState) -> CoreState:
    """Supply independent codec-bound accepted origins for eligible fixtures."""
    codec = OperationRegistry(
        tuple(
            OperationRegistration(
                descriptor=descriptor,
                request_model=REGISTERED_REQUESTS[descriptor.kind],
                outcome_model=REGISTERED_REQUESTS[descriptor.kind].outcome_model,
                normalize_turn=normalize_registered_turn
                if descriptor.lifecycle == LifecycleClass.SESSION_TURN
                else None,
            )
            for descriptor in state.registry
        )
    )
    receipts = list(state.run.receipts)
    for record in state.records.records:
        request = record.request
        if not isinstance(request, ExecuteRegisteredOperation):
            continue
        assert request.decision_id is not None
        decision = codec.validate_decision(
            Operation(
                decision_id=request.decision_id,
                scope=request.scope,
                request=codec.decode(request.operation),
            )
        )
        receipts.append(
            DecisionReceipt(
                decision_id=decision.decision_id,
                decision=decision,
                payload_digest=fixture_digest(decision),
                request_ids=(record.request_id,),
                feedback=Accepted(
                    decision_id=decision.decision_id, request_ids=(record.request_id,)
                ),
            )
        )
    return state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "receipts": tuple(receipts),
                    "capabilities": Capabilities(operations=state.registry),
                }
            )
        }
    )


def reload(state: CoreState) -> CoreState:
    """Reload an atomic envelope through the public registered persistence codec."""
    if not restarts():
        return state
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
        fence=HostFence(host_id=HostId(root="recovery-host"), epoch=state.records.recovery.epoch),
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


def observed(record: RequestRecord, **facts: object) -> Observation:
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
    "phase", [RequestPhase.DISPATCHED, RequestPhase.RECONCILING, RequestPhase.BLOCKED]
)
def test_recovery_inspects_unfinished_authority_without_replaying_payload(
    phase: RequestPhase,
) -> None:
    # Legacy L:569 recovery and O:488 unknown provider acceptance.
    original = pending_record(phase=phase)
    state = recovering_state(original)
    result = step(reload(state), RecoveryStarted(epoch=3, now_at=12.0))
    barrier = result.state.records.recovery
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
    # The check records what re-resolves it (#2428): its inspection, and the target while open.
    assert check.wait is not None
    assert set(check.wait.requests) == (
        {inspection.request_id}
        if phase == RequestPhase.BLOCKED
        else {inspection.request_id, original.request_id}
    )
    assert unaddressed(result.state.records.records[0]) == original
    assert result.state.run.status == RunStatus.PAUSED
    assert result.state.run.now_at == 12.0
    assert project(result.state).scheduling.charged == project(state).scheduling.charged
    assert result.events == ()
    assert reload(result.state) == result.state


@pytest.mark.regression
@example(events=[(1, 0), (2, 0)])
@example(events=[(0, 0), (1, 0)])
@given(st.lists(st.tuples(st.integers(0, 5), st.integers(0, 99)), min_size=1, max_size=30))
def test_startup_sequences_preserve_ownership_and_deduplicate_inspection(
    events: list[tuple[int, int]],
) -> None:
    """Duplicate, reordered and stale epochs retain one inspection per target/epoch."""
    original = pending_record()
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
        assert result.state.records.recovery.epoch == epoch
        assert result.state.run.now_at == now_at
        kept = unaddressed(result.state.records.records[0])
        assert kept.model_copy(update={"until": original.until}) == original
        assert kept.until.at >= original.until.at
        assert result.state.run.status == RunStatus.PAUSED
        assert result.events == ()
        for request in result.requests:
            assert isinstance(request, InspectRequest)
            assert request.target == original.request_id
            assert request.request_id is not None
            assert request.request_id not in inspections
            inspections.add(request.request_id)
        assert len(inspections) <= 6
        checks = result.state.records.recovery.checks
        assert checks[0].target == original.request_id
        assert checks[0].resolution == "pending"
        assert all(check.resolution == "safe-prepared" for check in checks[1:])
        assert len(checks) <= 7
        assert project(result.state).scheduling.charged == 0
        assert project(result.state).scheduling.refunded == 0
        state = reload(result.state)


def test_query_completion_without_target_proof_cannot_complete_recovery() -> None:
    original = pending_record(phase=RequestPhase.COMPLETED)
    original = original.model_copy(update={"observation": observed(original)})
    result = step(recovering_state(original), RecoveryStarted(epoch=1, now_at=20.0))
    assert result.state.records.recovery.phase == RecoveryPhase.RECOVERING
    assert result.state.records.recovery.checks[0].resolution == "pending"
    assert isinstance(result.requests[0], InspectRequest)
    assert unaddressed(result.state.records.records[0]) == original


def test_a_check_keeps_naming_its_inspection_while_that_is_in_flight() -> None:
    """A later observation re-records the check's wait (#2428); an inspection the shell is
    still answering stays named, though it is a reconciliation command, not a canonical
    record."""
    original = pending_record(phase=RequestPhase.RECONCILING)
    started = step(recovering_state(original), RecoveryStarted(epoch=1, now_at=20.0))
    (inspection,) = started.requests
    assert inspection.request_id is not None
    dispatched = step(started.state, DispatchAuthorized(request_id=inspection.request_id))
    later = step(
        reload(dispatched.state),
        RequestObserved(observation=observed(original, sequence=2)),
    )
    (check,) = later.state.records.recovery.checks
    assert check.resolution == "pending"
    assert check.wait is not None
    assert set(check.wait.requests) == {original.request_id, inspection.request_id}
    assert orphan_waits(later.state) == ()


def timed_out(record: RequestRecord) -> RequestRecord:
    """The record as core leaves it when its limit passed: BLOCKED with a typed failure."""
    return record.model_copy(
        update={
            "phase": RequestPhase.BLOCKED,
            "failure": Failed(
                reason=FailureReason.TIMED_OUT,
                decision=record.request.decision_id,
                limit_at=record.until.at,
            ),
        }
    )


def request_until(state: CoreState, request: object) -> float:
    """The absolute limit core stored on the record it registered for an emitted request."""
    request_id = getattr(request, "request_id", None)
    record = next(row for row in state.records.records if row.request_id == request_id)
    return record.until.at


def non_diagnostic(result: Transition) -> tuple[object, ...]:
    """Strategy events other than the Failed result a deadline publishes."""
    return tuple(event for event in result.events if not isinstance(event, Failed))


@given(st.integers(min_value=0, max_value=99))
def test_reconciliation_before_deadline_never_authorizes_cleanup(now_at: int) -> None:
    original = pending_record()
    started = step(recovering_state(original), RecoveryStarted(epoch=1, now_at=0.0))
    result = step(
        reload(started.state),
        ReconciliationDeadline(request_id=original.request_id, now_at=float(now_at)),
    )
    assert result.requests == ()
    assert result.events == ()
    assert result.state.records == started.state.records
    assert result.state.run.status == RunStatus.PAUSED


def test_an_observation_that_resolves_nothing_leaves_a_blocked_recovery_blocked() -> None:
    original = pending_record()
    started = step(recovering_state(original), RecoveryStarted(epoch=1, now_at=0.0))
    blocked = step(
        reload(started.state),
        ReconciliationDeadline(request_id=original.request_id, now_at=1000.0),
    )
    assert blocked.state.records.recovery.phase == RecoveryPhase.BLOCKED
    later = step(
        reload(blocked.state),
        RequestObserved(observation=observed(blocked.state.records.records[0], sequence=2)),
    )
    assert later.state.records.recovery.phase == RecoveryPhase.BLOCKED
    assert later.state.records.recovery.checks[0].resolution == "blocked"


def test_a_restart_after_recovery_blocked_on_a_record_stays_blocked_on_it() -> None:
    """A new host learns nothing by restarting, so the block survives it.

    Before, the restarted recovery rebuilt its checks from the records alone: the next answer
    that repeated "unknown" about the record core had ended counted as settled, and recovery
    opened on a record the dead host had judged it could not get past.
    """
    original = pending_record()
    started = step(recovering_state(original), RecoveryStarted(epoch=1, now_at=0.0))
    blocked = step(
        reload(started.state),
        ReconciliationDeadline(request_id=original.request_id, now_at=1000.0),
    )
    assert blocked.state.records.recovery.phase == RecoveryPhase.BLOCKED
    restarted = step(reload(blocked.state), RecoveryStarted(epoch=2, now_at=1001.0))
    assert restarted.state.records.recovery.phase == RecoveryPhase.BLOCKED
    record = next(
        row for row in restarted.state.records.records if row.request_id == original.request_id
    )
    later = step(
        reload(restarted.state),
        RequestObserved(observation=observed(record, sequence=2)),
    )
    assert later.state.records.recovery.phase == RecoveryPhase.BLOCKED


@pytest.mark.parametrize("now_at", [100.0, 101.0, 1000.0])
def test_deadline_blocks_ambiguity_without_fabricating_terminal_ledger_fact(now_at: float) -> None:
    original = pending_record()
    started = step(recovering_state(original), RecoveryStarted(epoch=1, now_at=0.0))
    event = ReconciliationDeadline(request_id=original.request_id, now_at=now_at)
    result = step(reload(started.state), event)
    assert result.state.records.recovery.phase == RecoveryPhase.BLOCKED
    assert result.state.records.recovery.checks[0].resolution == "blocked"
    expected_failure = timed_out(original).failure
    assert unaddressed(result.state.records.records[0]) == timed_out(original)
    assert result.state.run.status == RunStatus.PAUSED
    blocks = [request for request in result.requests if isinstance(request, BlockRequest)]
    inspections = [request for request in result.requests if isinstance(request, InspectRequest)]
    assert len(blocks) == 1
    assert blocks[0].target == original.request_id
    assert blocks[0].request_id is not None
    assert result.events == (expected_failure,)
    wire = result.events[0].model_dump_json()
    assert Failed.model_validate_json(wire) == result.events[0]
    assert len(inspections) == 1
    assert inspections[0].target == original.request_id
    assert inspections[0].request_id == result.state.records.recovery.checks[0].inspection
    repeated = step(reload(result.state), event)
    assert repeated.requests == ()
    assert repeated.state.records == result.state.records
    assert repeated.events == ()


def test_unknown_deadline_target_rejects_without_mutating_input() -> None:
    state = recovering_state(pending_record())
    before = state.model_dump_json()
    with pytest.raises(ContractError):
        step(state, ReconciliationDeadline(request_id=RequestId(root="foreign"), now_at=100.0))
    assert state.model_dump_json() == before


def test_cas_fence_reducer_side_requires_committed_recovery_before_dispatch() -> None:
    original = pending_record(phase=RequestPhase.PREPARED)
    state = recovering_state(original)
    with pytest.raises(ContractError, match="recovery"):
        step(reload(state), DispatchAuthorized(request_id=original.request_id))
    assert state.records.records == (original,)


def test_child_ownership_survives_parent_deadline_and_cleanup_requests() -> None:
    original = pending_operation()
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
        update={"records": state.records.model_copy(update={"children": (lease,)})}
    )
    started = step(reload(state), RecoveryStarted(epoch=1, now_at=0.0))
    result = step(
        reload(started.state),
        ReconciliationDeadline(request_id=original.request_id, now_at=100.0),
    )
    assert result.state.records.children == (lease,)
    cancelled = {
        request.resource_id
        for request in result.requests
        if isinstance(request, CancelOwnedResource)
    }
    assert cancelled == {root, child}
    assert unaddressed(result.state.records.records[0]) == timed_out(original)
    assert result.state.run.status == RunStatus.PAUSED
    assert reload(result.state) == result.state


@given(
    phase=st.sampled_from(list(RequestPhase)),
    accepted=st.booleans(),
    terminal=st.booleans(),
    released=st.booleans(),
    complete=st.booleans(),
)
def test_unknown_acceptance_flags_never_replace_positive_recovery_proof(
    phase: RequestPhase,
    *,
    accepted: bool,
    terminal: bool,
    released: bool,
    complete: bool,
) -> None:
    """F3/F4/F5: ambiguous acknowledgement never authorizes reuse or release."""
    original = pending_record(phase=phase)
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
    check = result.state.records.recovery.checks[0]
    assert check.resolution == "pending"
    assert result.state.records.recovery.phase == RecoveryPhase.RECOVERING
    assert len(result.requests) == 1
    assert isinstance(result.requests[0], InspectRequest)
    assert unaddressed(result.state.records.records[0]) == original
    assert result.state.records.children == state.records.children
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
    original = pending_record()
    state = recovering_state(original)
    seen: set[RequestId] = set()
    ended = False
    for kind, epoch, now_at in sequence:
        event = (
            RecoveryStarted(epoch=epoch, now_at=float(now_at))
            if kind == "startup"
            else ReconciliationDeadline(request_id=original.request_id, now_at=float(now_at))
        )
        limit = state.records.records[0].until
        result = step(state, event)
        assert result == step(reload(state), event)
        # Only a deadline event past the limit ends the record, and not while the executor is
        # still being asked about it (the inspection already out has neither answered nor run
        # out its bound); startup alone never does.
        asked = {
            check.inspection
            for check in state.records.recovery.checks
            if check.target == original.request_id and check.resolution == "pending"
        }
        inspection = next((row for row in state.records.records if row.request_id in asked), None)
        asking = (
            inspection is not None
            and inspection.observation is None
            and result.state.run.now_at < inspection.until.at
        )
        ended = ended or (kind == "deadline" and result.state.run.now_at >= limit.at and not asking)
        # A restart moves the limit later by its downtime, never earlier; nothing else changes.
        kept = unaddressed(result.state.records.records[0])
        assert kept.until.at >= limit.at
        moved = original.model_copy(update={"until": kept.until})
        assert kept == (timed_out(moved) if ended else moved)
        assert result.state.records.recovery.phase != RecoveryPhase.READY
        assert result.state.run.status == RunStatus.PAUSED
        assert not non_diagnostic(result)
        assert result.state.run.receipts == state.run.receipts
        assert result.state.scheduling == state.scheduling
        assert result.state.attempts == state.attempts
        assert result.state.sessions == state.sessions
        for request in result.requests:
            assert isinstance(request, InspectRequest | BlockRequest)
            assert request.target == original.request_id
            assert request.request_id is not None
            assert request.request_id not in seen
            seen.add(request.request_id)
            if isinstance(request, BlockRequest):
                assert result.state.run.now_at >= original.until.at
        assert len(seen) <= 15
        state = reload(result.state)


@given(st.text(alphabet="abc123", min_size=1, max_size=20))
def test_user_request_identity_prefix_never_hides_unresolved_authority(suffix: str) -> None:
    """Generated reconciliation IDs are not a reserved user identity namespace."""
    original = pending_record(identity=f"recovery:{suffix}")
    anchor = pending_record(identity="anchor")
    result = step(recovering_state(original, anchor), RecoveryStarted(epoch=1, now_at=0.0))
    checks = {check.target: check for check in result.state.records.recovery.checks}
    assert original.request_id in checks
    assert checks[original.request_id].resolution == "pending"
    assert {
        request.target for request in result.requests if isinstance(request, InspectRequest)
    } == {
        original.request_id,
        anchor.request_id,
    }
    assert tuple(map(unaddressed, result.state.records.records[:2])) == (original, anchor)


@pytest.mark.parametrize("released", [False, True])
def test_completed_but_live_session_with_incomplete_manifest_cannot_complete_recovery(
    *,
    released: bool,
) -> None:
    """A successful acquisition command is not complete lease correspondence."""
    original = pending_record(phase=RequestPhase.COMPLETED)
    observation = observed(original, accepted=True, terminal=True, released=released)
    observation = observation.model_copy(update={"status": ObservationStatus.SUCCEEDED})
    original = original.model_copy(update={"observation": observation})
    result = step(
        recovering_state(original, pending_record(identity="anchor")),
        RecoveryStarted(epoch=1, now_at=10.0),
    )
    check = next(
        check
        for check in result.state.records.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == "pending"
    assert any(
        isinstance(request, InspectRequest) and request.target == original.request_id
        for request in result.requests
    )
    assert unaddressed(result.state.records.records[0]) == original


def test_parent_release_does_not_hide_live_child_at_deadline() -> None:
    """Child ownership is independent even after a conclusive parent release."""
    original = pending_record(phase=RequestPhase.COMPLETED)
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
    state = recovering_state(original, pending_record(identity="anchor"))
    state = state.model_copy(
        update={"records": state.records.model_copy(update={"children": (lease,)})}
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
    assert result.state.records.children == (lease,)
    # The setup concluded and its owner heard; only its unreleased child is cleaned up.
    assert unaddressed(result.state.records.records[0]) == original
    assert result.state.run.status == RunStatus.PAUSED


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


@given(
    case=st.sampled_from(
        ["missing", "resource", "request", "scope", "invocation", "session", "exact"]
    )
)
def test_session_turn_recovery_requires_exact_typed_owner(case: str) -> None:
    """F3: known acceptance reattaches only the same invocation and physical lease."""
    original = turn_record()
    assert isinstance(original.request, DispatchTurn)
    request = original.request
    resource = ResourceId(root="session-resource")
    observation = observed(original, resource_id=resource, accepted=True, children_complete=True)
    observation = observation.model_copy(update={"status": ObservationStatus.PENDING})
    original = original.model_copy(update={"observation": observation})
    state = recovering_state(original, pending_record(identity="anchor"))
    owner_observation = observation.model_copy(
        update={
            "resource_id": ResourceId(root="foreign-resource") if case == "resource" else resource,
            "request_id": RequestId(root="foreign-request")
            if case == "request"
            else original.request_id,
        }
    )
    invocation = Invocation(
        until=Until(at=100.0),
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
        for check in result.state.records.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == ("reattached" if case == "exact" else "pending")
    assert result.state.sessions == state.sessions
    assert unaddressed(result.state.records.records[0]) == original
    assert result.state.records.recovery.phase == RecoveryPhase.RECOVERING
    assert not any(isinstance(request, CancelOwnedResource) for request in result.requests)


@given(
    case=st.sampled_from(
        ["exact", "missing", "resource", "generation", "unaccepted", "required", "provisional"]
    )
)
def test_session_lease_reattachment_preserves_conversation_identity(case: str) -> None:
    """F5 reopen: a fresh or mismatched lease cannot stand in for required_resource."""
    original = pending_record(phase=RequestPhase.COMPLETED)
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
        pending_records=(original.request_id,) if case == "provisional" else (),
        resource_id=None
        if case == "provisional"
        else ResourceId(root="other-resource")
        if case == "resource"
        else resource,
    )
    state = recovering_state(original, pending_record(identity="anchor"))
    state = state.model_copy(
        update={"sessions": SessionsState(sessions=() if case == "missing" else (owner,))}
    )
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check
        for check in result.state.records.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == ("reattached" if case in ("exact", "provisional") else "pending")
    assert result.state.sessions == state.sessions
    assert unaddressed(result.state.records.records[0]) == original


@given(
    status=st.sampled_from(
        [ObservationStatus.SUCCEEDED, ObservationStatus.FAILED, ObservationStatus.CANCELLED]
    )
)
def test_completed_query_resolves_itself_without_resolving_pending_target(
    status: ObservationStatus,
) -> None:
    """Recovery distinguishes query completion from the inspected target's facts."""
    query = pending_record(identity="query", phase=RequestPhase.COMPLETED)
    request = InspectRequest(
        request_id=query.request_id,
        scope=query.request.scope,
        target=RequestId(root="external-target"),
    )
    query = query.model_copy(
        update={
            "request": request,
            "lifecycle": LifecycleClass.QUERY,
            "observation": observed(query, status=status, terminal=True),
        }
    )
    target = pending_record(identity="pending-target")
    result = step(recovering_state(query, target), RecoveryStarted(epoch=1, now_at=11.0))
    checks = {check.target: check for check in result.state.records.recovery.checks}
    assert checks[query.request_id].resolution == "terminal"
    assert checks[target.request_id].resolution == "pending"
    assert result.state.records.recovery.phase == RecoveryPhase.RECOVERING
    assert [
        request.target for request in result.requests if isinstance(request, InspectRequest)
    ] == [target.request_id]


def test_accepted_owned_job_without_resource_identity_cannot_fabricate_terminal_proof() -> None:
    """An owned-job acknowledgement requires an external identity or nonacceptance."""
    original = pending_record(phase=RequestPhase.COMPLETED)
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
    state = recovering_state(original, pending_record(identity="anchor"))
    state = state.model_copy(update={"registry": (descriptor,)})
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=10.0))
    check = next(
        check
        for check in result.state.records.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == "pending"
    assert any(
        isinstance(request, InspectRequest) and request.target == original.request_id
        for request in result.requests
    )


def test_child_release_requires_its_exact_source_request() -> None:
    """A foreign root's terminal acknowledgement cannot clear this child lease."""
    original = pending_record()
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
        update={"records": state.records.model_copy(update={"children": (lease,)})}
    )
    started = step(reload(state), RecoveryStarted(epoch=1, now_at=10.0))
    assert started.state.records.recovery.checks[0].resolution == "pending"
    result = step(
        reload(started.state), ReconciliationDeadline(request_id=original.request_id, now_at=200.0)
    )
    assert {
        request.resource_id
        for request in result.requests
        if isinstance(request, CancelOwnedResource)
    } == {child}
    assert result.state.records.children == (lease,)


@given(st.lists(st.lists(st.integers(0, 5), max_size=6), min_size=1, max_size=4))
def test_recovery_reconstructs_shared_descendant_proofs_canonically(
    manifests: list[list[int]],
) -> None:
    """Persisted child manifests deduplicate discoveries without losing ancestry."""
    originals = []
    for index, manifest in enumerate(manifests):
        record = pending_record(identity=f"parent-request-{index}")
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
    children = result.state.records.children
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
    assert tuple(map(unaddressed, result.state.records.records[: len(originals)])) == tuple(
        record for record in originals
    )
    inspections = [request for request in result.requests if isinstance(request, InspectRequest)]
    assert len(inspections) == len(originals) + sum(len(set(manifest)) for manifest in manifests)
    assert len({request.request_id for request in inspections}) == len(inspections)
    assert all(check.resolution == "pending" for check in result.state.records.recovery.checks)
    replay = step(reload(result.state), event)
    assert replay.requests == ()
    assert replay.state.records == result.state.records
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
        pending_operation(identity=f"{first}:{middle}"),
        pending_operation(identity=first),
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
        event = ReconciliationDeadline(request_id=original.request_id, now_at=200.0)
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
    assert tuple(map(unaddressed, state.records.records[:2])) == tuple(
        timed_out(record) for record in records
    )


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
    historical = turn_record()
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
                until=Until(at=100.0),
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
                update={"limits": sample_limits(max_attempts=200, max_refunds=200)}
            ),
        }
    )


@settings(max_examples=examples(pr=4, nightly=100))
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
    original = pending_record()
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
        assert not non_diagnostic(result)
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
    first = pending_record(identity="first")
    second = pending_record(identity="second")
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
        update={"records": state.records.model_copy(update={"children": (lease,)})}
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
    original = pending_operation(phase=RequestPhase.COMPLETED)
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
    state = recovering_state(original, pending_record(identity="anchor"))
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
    assert unaddressed(result.state.records.records[0]) == original
    assert result.state.sessions == state.sessions
    assert result.state.attempts == state.attempts


@given(st.integers(min_value=101, max_value=1500))
def test_reordered_time_cannot_expire_new_inspection_and_cancellation_bounds(
    logical_now: int,
) -> None:
    """Restart preserves the overdue original deadline and uses current supplied time."""
    original = pending_operation()
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
        request_until(result.state, request) == logical_now + state.run.limits.reconciliation_bound
        for request in result.requests
    )
    assert result.state.records.records[0].until.at == original.until.at
    # The original limit is long past, but the executor is still being asked: the deadline
    # waits for the inspection's bound, and a reordered, older time does not end that wait.
    early = step(
        reload(result.state), ReconciliationDeadline(request_id=original.request_id, now_at=100.0)
    )
    assert unaddressed(early.state.records.records[0]) == original
    assert early.state.records.recovery.phase == RecoveryPhase.RECOVERING
    waited = float(logical_now) + state.run.limits.reconciliation_bound
    blocked = step(
        reload(result.state),
        ReconciliationDeadline(request_id=original.request_id, now_at=waited),
    )
    assert blocked.state.records.recovery.phase == RecoveryPhase.BLOCKED
    for request in blocked.requests:
        bound = (
            state.run.limits.cancellation_bound
            if isinstance(request, CancelOwnedResource)
            else state.run.limits.reconciliation_bound
        )
        assert request_until(blocked.state, request) == waited + bound
    assert any(isinstance(request, CancelOwnedResource) for request in blocked.requests)
    assert unaddressed(blocked.state.records.records[0]) == timed_out(original)


def registered_record(
    lifecycle: LifecycleClass, identity: str = "registered"
) -> tuple[RequestRecord, OperationDescriptor]:
    """Canonical registry encoding for job and agent lifecycle payloads."""
    match lifecycle:
        case LifecycleClass.OWNED_JOB:
            payload = RegisteredJobRequest()
        case LifecycleClass.SESSION_TURN:
            original_turn = turn_record()
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
    original = pending_record(identity=identity)
    request = ExecuteRegisteredOperation(
        request_id=original.request_id,
        scope=original.request.scope,
        decision_id=DecisionId(root=identity),
        operation_id=OperationId(root=f"operation:{identity}"),
        operation=codec.encode(payload),
        retry_limit=0,
    )
    return original.model_copy(
        update={
            "request": request,
            "lifecycle": lifecycle,
            "payload_digest": fixture_digest(request),
        }
    ), descriptor


@pytest.mark.parametrize("lifecycle", list(LifecycleClass))
@pytest.mark.parametrize("phase", list(RequestPhase))
@pytest.mark.parametrize("flags", list(product((False, True), repeat=4)))
def test_ambiguous_acceptance_never_replays_any_lifecycle_class(
    lifecycle: LifecycleClass, phase: RequestPhase, flags: tuple[bool, bool, bool, bool]
) -> None:
    """Exhaust every class, phase and flag set; Unknown never proves external completion.

    A blocked query's recovery check is terminal because core already ended the query,
    not because its ambiguous observation proves acceptance, termination or release.
    Every other ambiguous record still needs inspection. Neither case replays work or
    changes the recorded observation or its owners.
    """
    _assert_ambiguous_recovery(lifecycle, phase, flags)


@pytest.mark.regression
@given(
    lifecycle=st.just(LifecycleClass.QUERY),
    phase=st.just(RequestPhase.BLOCKED),
    flags=st.tuples(st.booleans(), st.booleans(), st.booleans(), st.booleans()),
)
@example(
    lifecycle=LifecycleClass.QUERY,
    phase=RequestPhase.BLOCKED,
    flags=(False, False, False, False),
)
def test_ambiguous_blocked_query_settles_only_its_recovery_check(
    lifecycle: LifecycleClass, phase: RequestPhase, flags: tuple[bool, bool, bool, bool]
) -> None:
    """Pin the input that contradicted the old, overbroad recovery property."""
    _assert_ambiguous_recovery(lifecycle, phase, flags)


def _assert_ambiguous_recovery(
    lifecycle: LifecycleClass, phase: RequestPhase, flags: tuple[bool, bool, bool, bool]
) -> None:
    terminal, released, accepted, complete = flags
    if lifecycle == LifecycleClass.SESSION_TURN:
        original = turn_record()
        registry = ()
    elif lifecycle == LifecycleClass.OWNED_JOB:
        original, descriptor = registered_record(lifecycle)
        registry = (descriptor,)
    elif lifecycle == LifecycleClass.QUERY:
        original = pending_record()
        request = InspectRequest(
            request_id=original.request_id,
            scope=original.request.scope,
            target=RequestId(root="external"),
        )
        original = original.model_copy(update={"request": request, "lifecycle": lifecycle})
        registry = ()
    else:
        original = pending_record()
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
    state = recovering_state(original, pending_record(identity="anchor"))
    state = with_sibling_history(state.model_copy(update={"registry": registry}), (3, 1))
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check
        for check in result.state.records.recovery.checks
        if check.target == original.request_id
    )
    ended_query = lifecycle == LifecycleClass.QUERY and phase == RequestPhase.BLOCKED
    assert check.resolution == ("terminal" if ended_query else "pending")
    inspections = [
        request
        for request in result.requests
        if isinstance(request, InspectRequest) and request.target == original.request_id
    ]
    assert bool(inspections) == (not ended_query)
    assert all(isinstance(request, InspectRequest) for request in result.requests)
    assert unaddressed(result.state.records.records[0]) == original
    assert result.state.sessions == state.sessions
    assert result.state.attempts == state.attempts
    assert result.events == ()


@pytest.mark.parametrize("cleanup", ["discard", "close"])
def test_stale_retirement_recovery_cannot_release_newer_generation_or_episode(cleanup: str) -> None:
    """Translate legacy test_stale_withdrawal_recovery_cannot_release_a_newer_generation."""
    original = pending_record(identity="old-retirement")
    old_attempt = AttemptRef(attempt_id=AttemptId(root="current-owner"), generation=1)
    scope = Scope(owner=old_attempt.attempt_id, generation=old_attempt.generation)
    request_model = DiscardWorkspace if cleanup == "discard" else CloseAttemptScope
    request = request_model(
        request_id=original.request_id,
        scope=scope,
        attempt=old_attempt,
        admission_id=DecisionId(root="old-admission"),
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
        ReconciliationDeadline(request_id=original.request_id, now_at=200.0),
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
        isinstance(request, BlockRequest | InspectRequest | CancelOwnedResource)
        for request in blocked.requests
    )
    assert not non_diagnostic(blocked)
    assert unaddressed(blocked.state.records.records[0]) == timed_out(original)


@given(
    case=st.sampled_from(
        [
            "exact",
            "foreign-operation",
            "missing-normalization",
            "foreign-turn",
            "foreign-session",
            "foreign-invocation",
        ]
    )
)
def test_registered_session_turn_requires_its_normalized_owner_identity(case: str) -> None:
    """Registered agent work reattaches through its declared session owner."""
    original, descriptor = registered_record(LifecycleClass.SESSION_TURN)
    assert isinstance(original.request, ExecuteRegisteredOperation)
    operation_id = original.request.operation_id
    decision_id = original.request.decision_id
    assert decision_id is not None
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
    turn = turn_record()
    assert isinstance(turn.request, DispatchTurn)
    owner = Invocation(
        until=Until(at=100.0),
        invocation=InvocationRef(
            session_id=SessionId(root="foreign-session")
            if case == "foreign-session"
            else turn.request.turn.session.session_id,
            invocation_id=InvocationId(root="foreign-invocation")
            if case == "foreign-invocation"
            else turn.request.turn.invocation_id,
            generation=0,
        ),
        scope=original.request.scope,
        turn=turn.request.turn.model_copy(update={"max_turns": turn.request.turn.max_turns + 1})
        if case == "foreign-turn"
        else turn.request.turn,
        phase=SessionPhase.EXECUTING,
        registered_operation=OperationId(root="foreign")
        if case == "foreign-operation"
        else operation_id,
        observation=observation,
    )
    state = recovering_state(original, pending_record(identity="anchor"))
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
        )
    )
    receipt = DecisionReceipt(
        decision_id=decision_id,
        decision=decision,
        payload_digest=fixture_digest(decision),
        feedback=Accepted(decision_id=decision_id, request_ids=(original.request_id,)),
        request_ids=(original.request_id,),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "receipts": () if case == "missing-normalization" else (receipt,),
                    "capabilities": Capabilities(operations=(descriptor,)),
                }
            )
        }
    )
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check
        for check in result.state.records.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == ("reattached" if case == "exact" else "pending")
    assert result.state.sessions == state.sessions


def assert_handover_resolves(
    state: CoreState, owner: RegisteredOwnedJob, parent: RequestRecord
) -> None:
    """Chaos seeds 268 and 479: the ledger later proves it owns the job.

    The next observation hands the child over, and the check that waited on the child
    re-resolves with it, instead of waiting on a lease no inspection will ever settle.
    """
    assert owner.observation is not None
    learned = owner.model_copy(
        update={
            "observation": owner.observation.model_copy(
                update={"status": ObservationStatus.PENDING, "accepted": True}
            )
        }
    )
    state = state.model_copy(update={"evaluation": EvaluationState(registered_jobs=(learned,))})
    anchor = state.records.records[2]
    result = step(reload(state), RequestObserved(observation=observed(anchor)))
    check = next(
        check for check in result.state.records.recovery.checks if check.target == parent.request_id
    )
    assert result.state.records.children == ()
    assert check.resolution == "terminal"


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
    state = with_sibling_history(recovering_state(pending_record(identity="anchor")), (2, 1))
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
        Operation(
            decision_id=decision_id,
            scope=scope,
            request=payload,
        )
    )
    wire = codec.encode(payload)
    original = pending_record(identity="reopen", phase=RequestPhase.COMPLETED)
    request = ExecuteRegisteredOperation(
        request_id=original.request_id,
        scope=scope,
        decision_id=decision_id,
        admission_id=decision_id,
        operation_id=OperationId(root=f"operation:{decision_id.root}"),
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
    original = RequestRecord.model_validate(
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
        payload_digest=fixture_digest(decision),
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
            "run": state.run.model_copy(
                update={
                    "receipts": (receipt,),
                    "capabilities": Capabilities(operations=(descriptor,)),
                }
            ),
            "attempts": AttemptsState(attempts=(owner,)),
            "scheduling": SchedulingState(slots=(slot,)),
            "records": state.records.model_copy(
                update={"records": (original, *state.records.records)}
            ),
        }
    )
    for epoch in (1, 2):
        result = step(reload(state), RecoveryStarted(epoch=epoch, now_at=11.0))
        check = next(
            check
            for check in result.state.records.recovery.checks
            if check.target == original.request_id
        )
        assert check.resolution == ("pending" if admission == "unknown" else "terminal")
        assert result.state.attempts == state.attempts
        assert result.state.sessions == state.sessions
        assert result.state.scheduling == state.scheduling
        assert unaddressed(result.state.records.records[0]) == original
        assert result.state.records.records[0].outcome == outcome
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
    original, descriptor = registered_record(LifecycleClass.OWNED_JOB)
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
        wait=WaitOn(requests=(original.request_id,)),
        operation_id=OperationId(root="foreign")
        if case == "foreign-operation"
        else request.operation_id,
        request_id=RequestId(root="foreign") if case == "foreign-request" else original.request_id,
        scope=request.scope.model_copy(update={"generation": 1})
        if case == "foreign-scope"
        else request.scope,
        resource_pool=PoolId(root="foreign") if case == "foreign-pool" else PoolId(root="jobs"),
    )
    state = recovering_state(original, pending_record(identity="anchor"))
    state = state.model_copy(
        update={"registry": (descriptor,), "evaluation": EvaluationState(registered_jobs=(owner,))}
    )
    state = with_registered_origins(state)
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    check = next(
        check
        for check in result.state.records.recovery.checks
        if check.target == original.request_id
    )
    assert check.resolution == ("reattached" if case == "exact" else "pending")
    assert result.state.evaluation == state.evaluation
    assert unaddressed(result.state.records.records[0]) == original
    assert result.events == ()


@given(st.lists(st.integers(0, 100), min_size=1, max_size=30))
def test_generated_successor_deadlines_reconcile_original_root_without_recursive_requests(
    choices: list[int],
) -> None:
    """Fresh post-run timers reconcile the root without refreshing earlier successors."""
    original = pending_record()
    state = recovering_state(original)
    state = state.model_copy(update={"run": state.run.model_copy(update={"deadline_at": 105.0})})
    emitted: set[RequestId] = set()
    for index, choice in enumerate(choices):
        records = state.records.records
        selected = records[choice % len(records)]
        event = ReconciliationDeadline(request_id=selected.request_id, now_at=float(110 + index))
        result = step(reload(state), event)
        assert result == step(state, event)
        assert unaddressed(result.state.records.records[0]) == timed_out(original)
        assert result.state.records.records[1 : len(records)] == records[1:]
        assert result.state.records.recovery.phase == RecoveryPhase.BLOCKED
        assert {check.target for check in result.state.records.recovery.checks} == {
            original.request_id
        }
        for request in result.requests:
            assert isinstance(request, BlockRequest | InspectRequest)
            assert request.target == original.request_id
            assert request.request_id is not None
            assert request.request_id not in emitted
            assert request_until(result.state, request) == (
                result.state.run.now_at + state.run.limits.reconciliation_bound
            )
            emitted.add(request.request_id)
        assert len(emitted) <= 2
        assert len(result.state.records.records) <= 3
        assert not non_diagnostic(result)
        state = reload(result.state)


def test_late_child_cleanup_survives_previous_parent_block_and_cancel_requests() -> None:
    """A reload with new child proof adds one cleanup, even when parent cleanup exists."""
    original = pending_operation()
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
    # remains blocked by the frozen records A wrapper in this isolated slice.
    assert original.observation is not None
    latest = initial.state.records.records[0].model_copy(
        update={
            "observation": original.observation.model_copy(
                update={"sequence": 2, "observed_at": 120.0, "children": (child,)}
            )
        }
    )
    state = initial.state.model_copy(
        update={
            "records": initial.state.records.model_copy(
                update={
                    "records": (latest, *initial.state.records.records[1:]),
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
    assert result.state.records.children == (lease,)
    assert unaddressed(result.state.records.records[0]) == unaddressed(latest)
    repeated = step(reload(result.state), event)
    assert repeated.requests == ()
    assert repeated.state.records == result.state.records
    assert repeated.events == ()


@pytest.mark.parametrize("request_model", [DiscardWorkspace, CloseAttemptScope])
@pytest.mark.parametrize("names_resource", [False, True])
def test_a_request_that_completed_before_the_restart_resolves_its_own_recovery_check(
    request_model: type[DiscardWorkspace] | type[CloseAttemptScope], *, names_resource: bool
) -> None:
    """A terminal observation answers its own check, whether or not it names a resource.

    Such a request reports the workspace another request owns, so the report says nothing
    about that workspace's release and recovery has nothing left to wait on.
    """
    original = pending_record(identity="finished", phase=RequestPhase.COMPLETED)
    attempt = AttemptRef(attempt_id=AttemptId(root="owner"), generation=0)
    request = request_model(
        request_id=original.request_id,
        scope=Scope(owner=attempt.attempt_id, generation=attempt.generation),
        attempt=attempt,
        admission_id=DecisionId(root="admission"),
    )
    swapped = original.model_copy(
        update={"request": request, "lifecycle": LifecycleClass.IDEMPOTENT_WRITE}
    )
    finished = swapped.model_copy(
        update={
            "observation": observed(
                swapped,
                admission_id=request.admission_id,
                resource_id=ResourceId(root="workspace") if names_resource else None,
                status=ObservationStatus.SUCCEEDED,
                accepted=True,
                terminal=True,
                released=True,
                children_complete=True,
            ),
        }
    )
    result = step(reload(recovering_state(finished)), RecoveryStarted(epoch=1, now_at=11.0))
    assert [check.resolution for check in result.state.records.recovery.checks] != ["pending"]
    assert result.state.records.recovery.phase != RecoveryPhase.RECOVERING


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize(
    "lifecycle",
    [LifecycleClass.IDEMPOTENT_WRITE, LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN],
)
def test_a_refusal_that_accepted_nothing_resolves_its_recovery_check(
    lifecycle: LifecycleClass, *, accepted: bool
) -> None:
    """A request the executor never began is refused, and a refusal has nothing to recover.

    A refusal that claims acceptance is contradictory and proves nothing.
    """
    original = pending_record(identity="never-began", phase=RequestPhase.COMPLETED)
    refused = original.model_copy(
        update={
            "lifecycle": lifecycle,
            "observation": observed(
                original,
                status=ObservationStatus.REJECTED,
                terminal=True,
                accepted=accepted,
                released=True,
            ),
        }
    )
    result = step(reload(recovering_state(refused)), RecoveryStarted(epoch=1, now_at=11.0))
    resolved = result.state.records.recovery.checks[0].resolution != "pending"
    assert resolved is not accepted


def test_a_reissue_proof_must_fit_the_observation_it_accompanies() -> None:
    original = pending_record()
    accepting = observed(original, status=ObservationStatus.SUCCEEDED, accepted=True, terminal=True)
    with pytest.raises(ValueError, match="reissue"):
        TargetObservation(observation=accepting, reissue=ReissueProof.NEVER_BEGAN)


def test_a_second_inspection_of_a_request_already_reissued_changes_nothing() -> None:
    """Two epochs inspected the same never-begun request: the second answer is a replay."""
    original = pending_record(phase=RequestPhase.DISPATCHED)
    started = step(reload(recovering_state(original)), RecoveryStarted(epoch=1, now_at=11.0))
    inspection = started.requests[0]
    assert isinstance(inspection, InspectRequest)
    assert inspection.request_id is not None
    dispatched = step(started.state, DispatchAuthorized(request_id=inspection.request_id))
    refusal = observed(
        original, status=ObservationStatus.REJECTED, terminal=True, accepted=False, released=True
    )
    answer = Observation.model_validate(
        {
            "event_id": EventId(root="inspection-answer"),
            "request_id": inspection.request_id,
            "scope": inspection.scope,
            "sequence": 1,
            "observed_at": 12.0,
            "status": ObservationStatus.SUCCEEDED,
            "accepted": True,
            "terminal": True,
            "released": True,
            "children_complete": True,
        }
    )
    event = RequestObserved(
        observation=answer,
        target=TargetObservation(observation=refusal, reissue=ReissueProof.NEVER_BEGAN),
    )
    once = step(dispatched.state, event)
    again = step(once.state, event)
    assert again.state.records.records == once.state.records.records


def hosted(state: CoreState, *hosts: HostEpoch) -> CoreState:
    """``state`` as the epochs in ``hosts`` left it: the last host still holds the lease."""
    records = state.records.model_copy(
        update={
            "hosts": hosts,
            "recovery": RecoveryBarrier(epoch=hosts[-1].epoch, phase=RecoveryPhase.READY),
        }
    )
    return state.model_copy(update={"records": records})


@settings(max_examples=examples(pr=10, nightly=300))
@given(
    died_at=st.integers(min_value=0, max_value=99),
    downtime=st.integers(min_value=1, max_value=899),
    phase=st.sampled_from([RequestPhase.DISPATCHED, RequestPhase.RECONCILING]),
)
def test_a_restart_gives_an_open_record_back_the_time_no_host_was_alive(
    died_at: int, downtime: int, phase: RequestPhase
) -> None:
    """A limit counts only time a host was alive to wait: the restart moves it by the downtime.

    The host dies with the request's limit not yet passed and restarts after it. The
    executor's answer to the restart's inspection says nothing new (unknown), and a deadline
    arriving before the moved limit ends nothing: the request still has the time it had when
    the host died. (#2381, the crash walk that found it: a session turn begun and never
    delivered, whose limit passed during four short-lived hosts, was ended before Sessions
    learned the turn never started, so the attempt was lost.)
    """
    original = pending_record(phase=phase)
    state = hosted(
        recovering_state(original), HostEpoch(epoch=1, started_at=0.0, alive_at=float(died_at))
    )
    restart_at = float(died_at + downtime)
    started = step(reload(state), RecoveryStarted(epoch=2, now_at=restart_at))
    moved = started.state.records.records[0]
    assert moved.until.at == min(original.until.at + downtime, state.run.deadline_at)
    assert moved.until.at > restart_at
    inspection = next(r for r in started.requests if isinstance(r, InspectRequest))
    assert inspection.request_id is not None
    asked = step(started.state, DispatchAuthorized(request_id=inspection.request_id)).state
    answer = Observation.model_validate(
        {
            "event_id": EventId(root="restart-answer"),
            "request_id": inspection.request_id,
            "scope": inspection.scope,
            "sequence": 1,
            "observed_at": restart_at,
            "status": ObservationStatus.SUCCEEDED,
            "accepted": True,
            "terminal": True,
            "released": True,
            "children_complete": True,
        }
    )
    unknown = observed(original, status=ObservationStatus.UNKNOWN, observed_at=restart_at)
    answered = step(
        reload(asked),
        RequestObserved(observation=answer, target=TargetObservation(observation=unknown)),
    ).state
    before_limit = ReconciliationDeadline(request_id=original.request_id, now_at=restart_at)

    result = step(reload(answered), before_limit)

    record = result.state.records.records[0]
    assert record.phase in (RequestPhase.DISPATCHED, RequestPhase.RECONCILING)
    assert record.failure is None
    assert result.events == ()


RENEWAL = 10
"""The longest a live host goes between proofs that it is alive (lease renewals or commits)."""


@st.composite
def host_lives(draw: st.DrawFn) -> tuple[list[float], float, float]:
    """One host's life from its start: the renewals core records, its last commit, its death.

    A live host proves itself alive (a renewal or a commit) at least every ``RENEWAL``
    seconds, and core records its latest proof one way or the other. Other renewals reach
    core late, out of order and more than once (a heartbeat renewal recorded by a later
    iteration), each carrying the time it happened. The host dies at most one renewal
    interval after its last proof.
    """
    gaps = draw(st.lists(st.integers(min_value=0, max_value=RENEWAL), max_size=4))
    proofs = [0.0, *(float(sum(gaps[: i + 1])) for i in range(len(gaps)))]
    last = proofs[-1]
    renewals = draw(st.lists(st.sampled_from(proofs), max_size=6))
    if draw(st.booleans()):
        committed = last
    else:
        committed = draw(st.sampled_from(proofs))
        renewals.insert(draw(st.integers(min_value=0, max_value=len(renewals))), last)
    died = last + draw(st.integers(min_value=0, max_value=RENEWAL))
    return renewals, committed, died


@settings(max_examples=examples(pr=10, nightly=300))
@given(
    lives=st.lists(
        st.tuples(host_lives(), st.integers(min_value=0, max_value=200)), min_size=1, max_size=6
    ),
    past_deadline=st.booleans(),
)
def test_a_crash_loop_credits_a_limit_the_time_no_host_was_alive(
    lives: list[tuple[tuple[list[float], float, float], int]], *, past_deadline: bool
) -> None:
    """Across any history of host deaths, an open limit moves by the real downtime, plus at
    most one renewal interval per death, and never back.

    Each host starts, renews its lease and commits, then dies; the next starts some time
    later (waiting out the dead host's lease included). The credit is measured from the dead
    host's last recorded renewal or commit, so it never falls short of the real downtime, and
    exceeds it by at most the time between that proof and the death. (#2417: crediting from
    the last commit alone also credited a live idle host's time, so a crash loop pushed a
    limit out without bound. #2425: crediting only after the dead host's lease ended timed
    out a request no host could wait on.) A limit past the run deadline (core's own cleanup
    budget) is never capped, so there both bounds hold exactly.
    """
    original = pending_record(phase=RequestPhase.DISPATCHED)
    state = recovering_state(original)
    if past_deadline:
        state = state.model_copy(
            update={"run": state.run.model_copy(update={"deadline_at": original.until.at - 1.0})}
        )
    start = state.run.now_at
    downtime = 0.0
    for epoch, ((renewals, committed, died), down) in enumerate(lives, start=1):
        state = step(reload(state), RecoveryStarted(epoch=epoch, now_at=start)).state
        for renewal in renewals:
            state = step(state, HostRenewed(epoch=epoch, now_at=start + renewal)).state
        run = state.run.model_copy(update={"now_at": max(state.run.now_at, start + committed)})
        state = state.model_copy(update={"run": run})
        if epoch < len(lives):
            downtime += down
        start += died + down

    moved = state.records.records[0].until.at - original.until.at
    deaths = len(lives) - 1
    assert 0 <= moved <= downtime + RENEWAL * deaths
    if past_deadline:
        assert moved >= downtime


def test_a_saved_state_without_a_host_log_is_refused() -> None:
    """A state saved before the host log existed fails to load, naming the missing log.

    With a default, such a state would load with no log and credit no downtime for the
    restart that follows, which brings back the limit that expires while no host was alive.
    """
    saved = json.loads(RecordsState(hosts=()).model_dump_json())
    del saved["hosts"]
    with pytest.raises(ValidationError, match="hosts"):
        RecordsState.model_validate(saved)


def test_the_host_log_keeps_only_what_the_last_downtime_needs() -> None:
    """However many hosts die, core keeps the current epoch and the one before it."""
    state = recovering_state(pending_record(phase=RequestPhase.DISPATCHED))
    for epoch in range(1, 6):
        state = step(reload(state), RecoveryStarted(epoch=epoch, now_at=10.0 * epoch)).state
        state = step(state, HostRenewed(epoch=epoch, now_at=10.0 * epoch + 3)).state

    assert state.records.hosts == (
        HostEpoch(epoch=4, started_at=40.0, alive_at=43.0),
        HostEpoch(epoch=5, started_at=50.0, alive_at=53.0),
    )
    assert downtime_of(state.records.hosts) == 7.0


def test_a_renewal_from_an_epoch_that_is_not_current_is_refused() -> None:
    """Only the host holding the lease renews it, so a renewal naming another epoch is a defect."""
    state = step(
        recovering_state(pending_record(phase=RequestPhase.DISPATCHED)),
        RecoveryStarted(epoch=2, now_at=10.0),
    ).state

    with pytest.raises(ContractError, match="not current"):
        step(state, HostRenewed(epoch=1, now_at=11.0))


def test_a_limit_that_passed_while_the_host_was_down_waits_for_the_executors_report() -> None:
    """The far side may have finished in time: the restarted host asks before it times out.

    The host restarts at the run deadline, so the limit has passed when the first deadline
    event arrives even though the restart moved it by the downtime (no further than the run
    deadline). While the inspection of this epoch is out, that event ends nothing; the
    executor's report that the request finished completes it without a failure.
    """
    original = pending_record(phase=RequestPhase.DISPATCHED)
    late = original.until.at + 900.0
    started = step(reload(recovering_state(original)), RecoveryStarted(epoch=1, now_at=late))
    inspection = started.requests[0]
    assert isinstance(inspection, InspectRequest)
    assert inspection.request_id is not None
    deadline = ReconciliationDeadline(request_id=original.request_id, now_at=late)

    waiting = step(reload(started.state), deadline)

    assert unaddressed(waiting.state.records.records[0]) == original
    assert not waiting.events
    assert not [r for r in waiting.requests if isinstance(r, BlockRequest)]
    dispatched = step(waiting.state, DispatchAuthorized(request_id=inspection.request_id))
    finished = observed(
        original,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )
    answer = Observation.model_validate(
        {
            "event_id": EventId(root="inspection-answer"),
            "request_id": inspection.request_id,
            "scope": inspection.scope,
            "sequence": 1,
            "observed_at": late,
            "status": ObservationStatus.SUCCEEDED,
            "accepted": True,
            "terminal": True,
            "released": True,
            "children_complete": True,
        }
    )
    reported = step(
        dispatched.state,
        RequestObserved(observation=answer, target=TargetObservation(observation=finished)),
    )
    after = step(reload(reported.state), deadline)

    record = after.state.records.records[0]
    assert record.phase == RequestPhase.COMPLETED
    assert record.failure is None
    assert not after.events


@settings(max_examples=examples(pr=6, nightly=60))
@given(answers=st.integers(min_value=1, max_value=4), restart_between=st.booleans())
def test_a_blocked_check_never_returns_to_pending(*, answers: int, restart_between: bool) -> None:
    """The limit blocks the check and asks once more; however often the far side answers unknown.

    An unknown answer cannot undo the block: a pending check of an ended record has no request
    left in flight, so nothing would ever resolve it and the run would halt on an orphan wait.
    """
    original = pending_record(phase=RequestPhase.DISPATCHED)
    started = step(reload(recovering_state(original)), RecoveryStarted(epoch=1, now_at=11.0))
    deadline = ReconciliationDeadline(
        request_id=original.request_id, now_at=original.until.at + 1.0
    )
    blocked = step(reload(started.state), deadline)
    assert [check.resolution for check in blocked.state.records.recovery.checks] == ["blocked"]
    inspection = next(r for r in blocked.requests if isinstance(r, InspectRequest))
    assert inspection.request_id is not None
    state = step(blocked.state, DispatchAuthorized(request_id=inspection.request_id)).state
    for sequence in range(1, answers + 1):
        unknown = observed(original, status=ObservationStatus.UNKNOWN)
        answer = Observation.model_validate(
            {
                "event_id": EventId(root=f"deadline-answer-{sequence}"),
                "request_id": inspection.request_id,
                "scope": inspection.scope,
                "sequence": sequence,
                "observed_at": 200.0 + sequence,
                "status": ObservationStatus.SUCCEEDED,
                "accepted": True,
                "terminal": True,
                "released": True,
                "children_complete": True,
            }
        )
        state = step(
            reload(state) if restart_between else state,
            RequestObserved(observation=answer, target=TargetObservation(observation=unknown)),
        ).state
        barrier = state.records.recovery
        assert [check.resolution for check in barrier.checks] == ["blocked"]
        assert barrier.phase == RecoveryPhase.BLOCKED
