"""An open record ends at its limit, so an executor that went silent is ended on time.

``decide`` is the one rule that ends a step at its limit, whatever its executor last said: at or
after an open record's ``until`` it settles the step ``Cancelled(DEADLINE)`` and the loop records
that (``reach``), and the fold names the limit in the record's ``Failed(TIMED_OUT)``. Earlier
times end nothing, and `next_end` names the limit so the loop sleeps toward it instead of stalling.
"""

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    Access,
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
)
from vs_core.api.testing import next_end, reach

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
        addressed_by=(),
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


def _ended_at_its_limit(state: CoreState, now: float) -> bool:
    """Whether the loop, with the run clock at ``now``, ended the one record, and how it ended it."""
    after = reach(state, now)
    record = after.state.records.records[0]
    if now < BOUND:
        assert record.phase in (RequestPhase.DISPATCHED, RequestPhase.RECONCILING)
        assert record.failure is None
        assert after.events == ()
        return False
    assert record.phase == RequestPhase.COMPLETED
    assert record.failure == Failed(reason=FailureReason.TIMED_OUT, decision=None, limit_at=BOUND)
    assert reach(after.state, now + 1_000.0).events == (), "the end is recorded once"
    return True


@given(answer=ANSWERS, now=st.floats(min_value=1.0, max_value=1000.0))
def test_a_silent_record_ends_exactly_when_its_limit_has_passed(
    answer: tuple[RequestPhase, ObservationStatus, bool], now: float
) -> None:
    phase, status, terminal = answer
    state = _silent(phase, status, terminal=terminal)
    assert next_end(state) == BOUND

    assert _ended_at_its_limit(state, now) is (now >= BOUND)


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
    assert next_end(state) == BOUND
    assert not _ended_at_its_limit(state, BOUND - 1.0)
    assert _ended_at_its_limit(state, 10_000.0)


@given(
    status=st.sampled_from(list(ObservationStatus)), now=st.floats(min_value=1.0, max_value=1000.0)
)
def test_every_open_record_ends_exactly_at_its_limit_whatever_the_executor_last_said(
    status: ObservationStatus, now: float
) -> None:
    """The limit passing ends the record with ``Failed(TIMED_OUT)`` naming that limit."""
    state = _silent(RequestPhase.DISPATCHED, status, terminal=False)
    assert next_end(state) == BOUND

    assert _ended_at_its_limit(state, now) is (now >= BOUND)
