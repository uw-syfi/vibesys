"""Recovery proofs and replay through the public immutable lifecycle API."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_core.api import (
    Access,
    BlockIntent,
    CancelOwnedResource,
    ChildLease,
    ContractError,
    CoreState,
    DispatchAuthorized,
    EnsureSession,
    EventId,
    InspectRequest,
    Intent,
    IntentPhase,
    IntentsState,
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
    Scope,
    SessionId,
    SessionSpec,
    initial_state,
    project,
    step,
)


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
    """Use the public strict state codec at every durable boundary."""
    return CoreState.model_validate_json(state.model_dump_json())


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
    assert len(result.requests) == 1
    assert isinstance(result.requests[0], BlockIntent)
    assert result.requests[0].target == original.request_id
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
        assert len(seen) <= 10
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
