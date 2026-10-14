"""An open record ends at its limit, so an executor that went silent is blocked on time.

Core learns time only from ``ClockAdvanced``. Any tick at or after an open record's
``until`` blocks it (a ``BlockRequest`` request and a ``Failed(TIMED_OUT)`` event), earlier
ticks do not, the run's recovery barrier stays open for scheduling, and the projected next
wake names the limit so a shell sleeps toward it instead of stalling.
"""

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    Access,
    BlockRequest,
    ClockAdvanced,
    CoreState,
    EnsureSession,
    EventId,
    Failed,
    FailureReason,
    LifecycleClass,
    Observation,
    ObservationStatus,
    RecordsState,
    RecoveryBarrier,
    RecoveryPhase,
    RequestId,
    RequestPhase,
    RequestRecord,
    RoleId,
    Scope,
    SessionId,
    SessionSpec,
    Until,
    initial_state,
    project,
    step,
)

BOUND = 100.0


def _silent(phase: RequestPhase, status: ObservationStatus, *, terminal: bool) -> CoreState:
    state = initial_state()
    request_id = RequestId(root="setup")
    request = EnsureSession(
        request_id=request_id,
        scope=Scope(owner=state.run.run_id, generation=0),
        spec=SessionSpec(
            session_id=SessionId(root="setup"),
            role_id=RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
    )
    answered = Observation(
        event_id=EventId(root="setup:0"),
        request_id=request_id,
        scope=request.scope,
        sequence=0,
        observed_at=1.0,
        status=status,
        terminal=terminal,
    )
    record = RequestRecord(
        request_id=request_id,
        request=request,
        payload_digest="setup",
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=phase,
        sequence=0,
        observation=answered,
        until=Until(at=BOUND),
    )
    return state.model_copy(
        update={
            "records": RecordsState(
                hosts=(),
                records=(record,),
                recovery=RecoveryBarrier(epoch=1, phase=RecoveryPhase.READY),
            )
        }
    )


ANSWERS = st.sampled_from(
    [
        (RequestPhase.RECONCILING, ObservationStatus.UNKNOWN, False),
        (RequestPhase.DISPATCHED, ObservationStatus.FAILED, False),
    ]
)


@given(answer=ANSWERS, now=st.floats(min_value=1.0, max_value=1000.0))
def test_a_tick_blocks_a_silent_record_exactly_when_its_bound_has_passed(
    answer: tuple[RequestPhase, ObservationStatus, bool], now: float
) -> None:
    phase, status, terminal = answer
    state = _silent(phase, status, terminal=terminal)
    assert project(state).next_observe_at == BOUND

    after = step(state, ClockAdvanced(now_at=now))

    blocks = [r for r in after.requests if isinstance(r, BlockRequest)]
    assert (len(blocks) == 1) == (now >= BOUND)
    assert after.state.records.recovery.phase == RecoveryPhase.READY


def test_a_record_still_executing_ends_at_its_limit_like_any_open_record() -> None:
    """A limit bounds the request, not the executor's answers: no answer is no exception."""
    state = _silent(RequestPhase.DISPATCHED, ObservationStatus.PENDING, terminal=False)
    state = state.model_copy(
        update={
            "records": state.records.model_copy(
                update={
                    "records": (state.records.records[0].model_copy(update={"observation": None}),)
                }
            )
        }
    )
    assert project(state).next_observe_at == BOUND
    assert not step(state, ClockAdvanced(now_at=BOUND - 1.0)).requests
    after = step(state, ClockAdvanced(now_at=10_000.0))
    assert [type(r) for r in after.requests] == [BlockRequest]
    assert after.state.records.records[0].phase == RequestPhase.BLOCKED


@given(
    status=st.sampled_from(list(ObservationStatus)), now=st.floats(min_value=1.0, max_value=1000.0)
)
def test_every_open_record_ends_exactly_at_its_limit_whatever_the_executor_last_said(
    status: ObservationStatus, now: float
) -> None:
    """The limit passing ends the record with ``Failed(TIMED_OUT)`` naming that limit."""
    state = _silent(RequestPhase.DISPATCHED, status, terminal=False)
    assert project(state).next_observe_at == BOUND
    after = step(state, ClockAdvanced(now_at=now))
    blocks = [r for r in after.requests if isinstance(r, BlockRequest)]
    record = after.state.records.records[0]
    if now < BOUND:
        assert not blocks
        assert record.phase == RequestPhase.DISPATCHED
        assert record.failure is None
        assert after.events == ()
        return
    failed = Failed(
        reason=FailureReason.TIMED_OUT,
        decision=None,
        limit_at=BOUND,
    )
    assert len(blocks) == 1
    assert record.phase == RequestPhase.BLOCKED
    assert record.failure == failed
    assert after.events == (failed,)
