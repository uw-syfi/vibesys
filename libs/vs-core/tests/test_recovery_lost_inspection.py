"""An inspection that never reported before a crash is issued again by the next recovery."""

import json
from hashlib import sha256

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    Access,
    EnsureSession,
    InspectRequest,
    LifecycleClass,
    RecordsState,
    RecoveryBarrier,
    RecoveryStarted,
    RequestId,
    RequestPhase,
    RequestRecord,
    RoleId,
    RunStatus,
    Scope,
    SessionId,
    SessionSpec,
    Until,
    Value,
    initial_state,
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
