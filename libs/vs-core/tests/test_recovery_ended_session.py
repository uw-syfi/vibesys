"""A restart recognises the session an ensure created once that session has ended, accepted or not.

A run's implementer session was ensured, and every turn it ran was lost (the agent's login had
expired), so no turn ever accepted the session. The round's settlement closed it, the run ended
later, and every resume ended at once: "recovery blocked: outcome unknown for <the ensure>".
Recovery recognised an ensure's resource only through a session that was accepted, or busy
with its first turn; an ended session that was never accepted was neither. So the ensure's check
stayed pending, its inspection could only answer that the session was gone, and recovery
settled the check as blocked, on every restart.

Every test drives core through its public API (`step`).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_request_recovery import (
    fixture_digest,
    observed,
    pending_record,
    recovering_state,
    reload,
)

RESOURCE = core.ResourceId(root="session:run:0/session:implementer:000006")
OTHER = core.ResourceId(root="session:run:0/session:implementer:000005")


def _ensured(resource: core.ResourceId = RESOURCE) -> core.RequestRecord:
    """The completed ensure: accepted, and it reported the session's conversation."""
    record = pending_record("ensure", phase=core.RequestPhase.COMPLETED)
    observation = observed(
        record,
        status=core.ObservationStatus.SUCCEEDED,
        resource_id=resource,
        accepted=True,
        terminal=True,
        children_complete=True,
    )
    return record.model_copy(update={"observation": observation})


def _closed(ensure: core.RequestRecord) -> core.RequestRecord:
    """The settlement's close of the session, which released its conversation."""
    assert isinstance(ensure.request, core.EnsureSession)
    request = core.CloseSession(
        request_id=core.RequestId(root="close"),
        scope=ensure.request.scope,
        session_id=ensure.request.spec.session_id,
        resource_id=RESOURCE,
    )
    record = core.RequestRecord(
        request_id=request.request_id,
        request=request,
        payload_digest=fixture_digest(request),
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.RequestPhase.COMPLETED,
        until=core.Until(at=100.0),
        addressed_by=(),
    )
    observation = observed(
        record,
        status=core.ObservationStatus.SUCCEEDED,
        resource_id=RESOURCE,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )
    return record.model_copy(update={"observation": observation})


def _session(
    ensure: core.RequestRecord,
    *,
    phase: core.SessionPhase = core.SessionPhase.TERMINAL,
    accepted: bool = False,
    resource: core.ResourceId = RESOURCE,
) -> core.SessionView:
    assert isinstance(ensure.request, core.EnsureSession)
    return core.SessionView(
        spec=ensure.request.spec,
        scope=ensure.request.scope,
        generation=0,
        phase=phase,
        accepted=accepted,
        resource_id=resource,
    )


def _stopped(
    session: core.SessionView, *records: core.RequestRecord, barrier: core.RecoveryBarrier
) -> core.CoreState:
    state = recovering_state(*records)
    return state.model_copy(
        update={
            "sessions": core.SessionsState(sessions=(session,)),
            "records": state.records.model_copy(update={"recovery": barrier}),
        }
    )


def _check(result: core.Transition, target: core.RequestId) -> core.RecoveryCheck:
    return next(check for check in result.state.records.recovery.checks if check.target == target)


def test_a_restart_after_settlement_closed_a_session_no_turn_accepted_opens_recovery() -> None:
    ensure = _ensured()
    state = _stopped(_session(ensure), ensure, _closed(ensure), barrier=core.RecoveryBarrier())

    result = core.step(reload(state), core.RecoveryStarted(epoch=1, now_at=11.0))

    assert _check(result, ensure.request_id).resolution == "reattached"
    assert result.state.records.recovery.phase == core.RecoveryPhase.READY
    assert not any(
        isinstance(outbound.request, core.InspectRequest) for outbound in result.requests
    )


def test_a_run_whose_recovery_already_blocked_on_that_ensure_opens_on_the_next_restart() -> None:
    """The saved run: its last resume blocked on the ensure, and the next one must not."""
    ensure = _ensured()
    inspection = core.RequestId(root=f"recovery:2:{ensure.request_id.root}")
    blocked = core.RecoveryBarrier(
        epoch=2,
        phase=core.RecoveryPhase.BLOCKED,
        checks=(
            core.RecoveryCheck(
                target=ensure.request_id, inspection=inspection, resolution="blocked", wait=None
            ),
        ),
    )
    state = _stopped(_session(ensure), ensure, _closed(ensure), barrier=blocked)

    result = core.step(reload(state), core.RecoveryStarted(epoch=3, now_at=11.0))

    assert _check(result, ensure.request_id).resolution == "reattached"
    assert result.state.records.recovery.phase == core.RecoveryPhase.READY


@given(accepted=st.booleans(), closed=st.booleans())
def test_an_ended_session_owns_the_conversation_its_ensure_reported(
    *, accepted: bool, closed: bool
) -> None:
    """Acceptance and the close record are irrelevant once the session has ended."""
    ensure = _ensured()
    records = (ensure, _closed(ensure)) if closed else (ensure,)
    state = _stopped(_session(ensure, accepted=accepted), *records, barrier=core.RecoveryBarrier())

    result = core.step(reload(state), core.RecoveryStarted(epoch=1, now_at=11.0))

    assert _check(result, ensure.request_id).resolution == "reattached"
    assert result.state.sessions == state.sessions


@given(
    accepted=st.booleans(),
    phase=st.sampled_from([core.SessionPhase.TERMINAL, core.SessionPhase.IDLE]),
)
def test_a_session_holding_another_conversation_never_owns_the_ensures(
    *, accepted: bool, phase: core.SessionPhase
) -> None:
    """Only the very conversation the ensure reported is recognised, ended or not."""
    ensure = _ensured()
    state = _stopped(
        _session(ensure, phase=phase, accepted=accepted, resource=OTHER),
        ensure,
        barrier=core.RecoveryBarrier(),
    )

    result = core.step(reload(state), core.RecoveryStarted(epoch=1, now_at=11.0))

    assert _check(result, ensure.request_id).resolution == "pending"
    assert result.state.records.recovery.phase == core.RecoveryPhase.RECOVERING
