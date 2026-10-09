"""An inspection that never reported before a crash is issued again by the next recovery."""

import json
from hashlib import sha256

import pytest

from vs_core.api import (
    Access,
    EnsureSession,
    InspectRequest,
    Intent,
    IntentPhase,
    IntentsState,
    LifecycleClass,
    RecoveryBarrier,
    RecoveryStarted,
    RequestId,
    RoleId,
    RunStatus,
    Scope,
    SessionId,
    SessionSpec,
    Value,
    initial_state,
    step,
)


def _digest(value: Value) -> str:
    return sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _session_intent() -> Intent:
    state = initial_state()
    request_id = RequestId(root="target")
    request = EnsureSession(
        request_id=request_id,
        scope=Scope(owner=state.run.run_id, generation=0),
        deadline_at=100.0,
        spec=SessionSpec(
            session_id=SessionId(root="target"),
            role_id=RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
    )
    return Intent(
        request_id=request_id,
        request=request,
        payload_digest=_digest(request),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=IntentPhase.DISPATCHED,
        reconcile_deadline_at=100.0,
    )


def _lost_inspection(target: Intent, phase: IntentPhase) -> Intent:
    request = InspectRequest(
        request_id=RequestId(root="inspection"),
        scope=target.request.scope,
        deadline_at=100.0,
        target=target.request_id,
    )
    return Intent(
        request_id=request.request_id,
        request=request,
        payload_digest=_digest(request),
        lifecycle=LifecycleClass.QUERY,
        phase=phase,
        reconcile_deadline_at=100.0,
    )


@pytest.mark.parametrize("phase", [IntentPhase.DISPATCHED, IntentPhase.RECONCILING])
def test_an_inspection_that_never_reported_is_prepared_again(phase: IntentPhase) -> None:
    target = _session_intent()
    lost = _lost_inspection(target, phase)
    state = initial_state()
    state = state.model_copy(
        update={
            "intents": IntentsState(intents=(target, lost), recovery=RecoveryBarrier()),
            "run": state.run.model_copy(update={"status": RunStatus.PAUSED}),
        }
    )
    result = step(state, RecoveryStarted(epoch=1, now_at=11.0))
    phases = {row.request_id: row.phase for row in result.state.intents.intents}
    assert phases[lost.request_id] == IntentPhase.PREPARED
    assert phases[target.request_id] == target.phase
