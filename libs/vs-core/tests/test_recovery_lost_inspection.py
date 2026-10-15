"""An inspection that never reported before a crash is issued again by the next recovery."""

import json
from hashlib import sha256

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    Access,
    BlockRequest,
    CancelOwnedResource,
    ClockAdvanced,
    DispatchAuthorized,
    EnsureSession,
    EventId,
    FailureReason,
    InspectRequest,
    LifecycleClass,
    Observation,
    ObservationStatus,
    RecordsState,
    RecoveryBarrier,
    RecoveryPhase,
    RecoveryStarted,
    ReissueProof,
    RequestId,
    RequestObserved,
    RequestPhase,
    RequestRecord,
    ResourceId,
    RoleId,
    RunStatus,
    Scope,
    SessionId,
    SessionSpec,
    TargetObservation,
    Transition,
    Until,
    Value,
    WaitKind,
    initial_state,
    orphan_waits,
    step,
)


def _digest(value: Value) -> str:
    return sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _session_record() -> RequestRecord:
    state = initial_state()
    request_id = RequestId(root="target")
    request = EnsureSession(
        request_id=request_id,
        scope=Scope(owner=state.run.run_id, generation=0),
        spec=SessionSpec(
            session_id=SessionId(root="target"),
            role_id=RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
    )
    return RequestRecord(
        request_id=request_id,
        request=request,
        payload_digest=_digest(request),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=RequestPhase.DISPATCHED,
        until=Until(at=100.0),
        addressed_by=(),
    )


def _lost_inspection(target: RequestRecord, phase: RequestPhase) -> RequestRecord:
    request_id = RequestId(root="inspection")
    request = InspectRequest(
        request_id=request_id,
        scope=target.request.scope,
        target=target.request_id,
    )
    return RequestRecord(
        request_id=request_id,
        request=request,
        payload_digest=_digest(request),
        lifecycle=LifecycleClass.QUERY,
        phase=phase,
        until=Until(at=100.0),
        addressed_by=(),
    )


@given(
    phase=st.sampled_from(
        [RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING]
    ),
    restart_at=st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False),
)
def test_an_inspection_that_never_reported_goes_out_again_with_a_bound_from_the_restart(
    phase: RequestPhase, restart_at: float
) -> None:
    """Whether it was in flight or only prepared, and however long the host was down.

    Nothing ends an inspection at its bound, so one sent out with a bound that passed while
    the host was down would be dropped unanswered and hold the run open for good.
    """
    target = _session_record()
    lost = _lost_inspection(target, phase)
    state = initial_state()
    state = state.model_copy(
        update={
            "records": RecordsState(hosts=(), records=(target, lost), recovery=RecoveryBarrier()),
            "run": state.run.model_copy(update={"status": RunStatus.PAUSED}),
        }
    )
    result = step(state, RecoveryStarted(epoch=1, now_at=restart_at))
    rows = {row.request_id: row for row in result.state.records.records}
    now_at = max(result.state.run.now_at, restart_at)
    assert rows[lost.request_id].phase == RequestPhase.PREPARED
    assert rows[lost.request_id].until.at >= now_at + state.run.limits.reconciliation_bound
    assert rows[target.request_id].phase == target.phase


def _lost_command(target: RequestRecord, kind: str, phase: RequestPhase) -> RequestRecord:
    request_id = RequestId(root=f"lost-{kind}")
    scope = target.request.scope
    request: CancelOwnedResource | BlockRequest = (
        CancelOwnedResource(
            request_id=request_id,
            scope=scope,
            target=target.request_id,
            resource_id=ResourceId(root="target-resource"),
        )
        if kind == "cancel"
        else BlockRequest(
            request_id=request_id,
            scope=scope,
            target=target.request_id,
            reason=FailureReason.TIMED_OUT,
        )
    )
    return RequestRecord(
        request_id=request_id,
        request=request,
        payload_digest=_digest(request),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=phase,
        until=Until(at=100.0),
        addressed_by=(),
        dispatched_in=None if phase == RequestPhase.PREPARED else 0,
    )


def _restarted(
    kind: str, phase: RequestPhase, restart_at: float
) -> tuple[Transition, RequestRecord]:
    target = _session_record()
    lost = _lost_command(target, kind, phase)
    state = initial_state()
    state = state.model_copy(
        update={
            "records": RecordsState(hosts=(), records=(target, lost), recovery=RecoveryBarrier()),
            "run": state.run.model_copy(update={"status": RunStatus.PAUSED}),
        }
    )
    return step(state, RecoveryStarted(epoch=1, now_at=restart_at)), lost


@given(
    kind=st.sampled_from(["cancel", "block"]),
    phase=st.sampled_from([RequestPhase.DISPATCHED, RequestPhase.RECONCILING]),
    restart_at=st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False),
)
def test_a_dead_hosts_cancel_or_block_closes_only_by_an_inspection_of_it(
    kind: str, phase: RequestPhase, restart_at: float
) -> None:
    """A cancel or block a dead host sent is inspected by the next epoch, never sent blind.

    Before, recovery skipped it: it stayed in flight for good and held its waiters (#2529).
    A blind re-send is re-execution, and one answered "unknown" stalls just the same
    (REVIEW-8 P2-2). The inspection's answer replays what the dead host's attempt did, or
    proves it never began, which is what licenses the ledger to send it again.
    """
    result, lost = _restarted(kind, phase, restart_at)
    rows = {row.request_id: row for row in result.state.records.records}
    assert rows[lost.request_id].phase == phase
    inspections = [
        request
        for request in result.requests
        if isinstance(request, InspectRequest) and request.target == lost.request_id
    ]
    assert len(inspections) == 1
    assert inspections[0].request_id in rows[lost.request_id].addressed_by
    assert not any(request.request_id == lost.request_id for request in result.requests)


@given(
    kind=st.sampled_from(["cancel", "block"]),
    restart_at=st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False),
)
def test_a_cancel_or_block_the_dead_host_never_sent_goes_out_with_a_bound_from_the_restart(
    kind: str, restart_at: float
) -> None:
    """One still PREPARED never left the dead host: sending it is its first send."""
    result, lost = _restarted(kind, RequestPhase.PREPARED, restart_at)
    rows = {row.request_id: row for row in result.state.records.records}
    now_at = max(result.state.run.now_at, restart_at)
    assert rows[lost.request_id].phase == RequestPhase.PREPARED
    assert rows[lost.request_id].until.at >= now_at + result.state.run.limits.reconciliation_bound


@given(silent_for=st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False))
def test_a_recovery_check_whose_inspection_is_never_answered_blocks_and_is_no_orphan(
    silent_for: float,
) -> None:
    """The check's inspection ends at its limit, and then the check ends blocked.

    ``RESOLUTIONS`` says so: a recovery check is never a silent orphan, and recovery blocks
    with the target named once its inspection could not settle it.
    """
    target = _session_record()
    state = initial_state()
    state = state.model_copy(
        update={
            "records": RecordsState(hosts=(), records=(target,), recovery=RecoveryBarrier()),
            "run": state.run.model_copy(update={"status": RunStatus.PAUSED}),
        }
    )
    state = step(state, RecoveryStarted(epoch=1, now_at=0.0)).state
    assert state.records.recovery.phase == RecoveryPhase.RECOVERING
    bound = state.run.limits.reconciliation_bound
    for now_at in (bound + 1.0 + silent_for, 2 * bound + 2.0 + silent_for):
        state = step(state, ClockAdvanced(now_at=now_at)).state

    assert state.records.recovery.phase == RecoveryPhase.BLOCKED
    assert [check.resolution for check in state.records.recovery.checks] == ["blocked"]
    assert not [wait for wait in orphan_waits(state) if wait.waiter == WaitKind.RECOVERY_CHECK]


def _answered_never_began(
    inspection: InspectRequest, lost: RequestRecord, at: float
) -> RequestObserved:
    """The executor's answer to ``inspection``: the dead host's ``lost`` command never began."""
    refusal = Observation.model_validate(
        {
            "event_id": EventId(root=f"refusal-{lost.request_id.root}"),
            "request_id": lost.request_id,
            "scope": lost.request.scope,
            "sequence": 1,
            "observed_at": at,
            "status": ObservationStatus.REJECTED,
            "accepted": False,
            "terminal": True,
            "released": True,
        }
    )
    answer = Observation.model_validate(
        {
            "event_id": EventId(root="inspection-answer"),
            "request_id": inspection.request_id,
            "scope": inspection.scope,
            "sequence": 1,
            "observed_at": at,
            "status": ObservationStatus.SUCCEEDED,
            "accepted": True,
            "terminal": True,
            "released": True,
            "children_complete": True,
        }
    )
    return RequestObserved(
        observation=answer,
        target=TargetObservation(observation=refusal, reissue=ReissueProof.NEVER_BEGAN),
    )


@given(
    kind=st.sampled_from(["cancel", "block"]),
    phase=st.sampled_from([RequestPhase.DISPATCHED, RequestPhase.RECONCILING]),
    down_past_bound=st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False),
)
def test_a_dead_hosts_command_restarted_after_its_limit_is_sent_again_on_a_never_began_proof(
    kind: str, phase: RequestPhase, down_past_bound: float
) -> None:
    """However long the host was down, the command lives until its inspection answers.

    Before, a dispatched cancel or block kept the limit the dead host gave it (REVIEW-10
    P2-3). Restarted after that limit, it lapsed BLOCKED on the first tick, the inspection's
    "never began" proof was dropped, and the cancel was never sent; a re-send would have kept
    the stale limit and lapsed on the next tick.
    """
    restart_at = 100.0 + down_past_bound
    started, lost = _restarted(kind, phase, restart_at)
    (inspection,) = (
        request
        for request in started.requests
        if isinstance(request, InspectRequest) and request.target == lost.request_id
    )
    assert inspection.request_id is not None
    bound = started.state.run.limits.reconciliation_bound

    ticked = step(started.state, ClockAdvanced(now_at=restart_at + 1.0))
    rows = {row.request_id: row for row in ticked.state.records.records}
    assert rows[lost.request_id].phase == phase

    asked = step(ticked.state, DispatchAuthorized(request_id=inspection.request_id))
    answered_at = restart_at + 2.0
    result = step(
        asked.state.model_copy(
            update={"run": asked.state.run.model_copy(update={"now_at": answered_at})}
        ),
        _answered_never_began(inspection, lost, answered_at),
    )
    rows = {row.request_id: row for row in result.state.records.records}
    assert rows[lost.request_id].phase == RequestPhase.PREPARED
    assert rows[lost.request_id].until.at >= answered_at + bound

    later = step(result.state, ClockAdvanced(now_at=answered_at + 1.0))
    rows = {row.request_id: row for row in later.state.records.records}
    assert rows[lost.request_id].phase == RequestPhase.PREPARED
