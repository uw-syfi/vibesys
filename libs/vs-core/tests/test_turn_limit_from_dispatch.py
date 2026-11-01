"""A turn's limit counts from the instant the shell dispatches its requests, not from the ask.

A request waits for a free slot of the shell before it runs. The time it waits is not the
turn's: a turn asked at ``t`` with limit ``L`` and dispatched at ``d >= t`` may run until
``d + L``. Until it is dispatched only the run deadline ends it. The shell's dispatch time
reaches core on ``DispatchAuthorized`` (``now_at``), so a replay stamps the same instant.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.testing.limits import reach

from .test_session_turns import invocation, scope, turn, waiting_turn_state
from .test_silent_executor_deadline import _silent
from .wait_support import OPEN

_LIMIT = st.integers(min_value=1, max_value=1000).map(float)
_QUEUED = st.integers(min_value=0, max_value=5000).map(float)
_RUN_DEADLINE = 1_000_000.0


def _ended(state: core.CoreState, request_id: core.RequestId) -> bool:
    record = next(row for row in state.records.records if row.request_id == request_id)
    return record.phase not in OPEN


def _prepared(state: core.CoreState, kind: type[core.Request]) -> core.RequestRecord:
    return next(
        row
        for row in state.records.records
        if isinstance(row.request, kind) and row.phase == core.RequestPhase.PREPARED
    )


def _with_deadline(state: core.CoreState) -> core.CoreState:
    return state.model_copy(
        update={"run": state.run.model_copy(update={"deadline_at": _RUN_DEADLINE})}
    )


def _asked(limit: float) -> core.CoreState:
    """A turn asked at time zero with ``limit``; its session acquisition is queued."""
    state = _with_deadline(core.initial_state())
    event = core.TurnRequested(until=core.Until(at=limit), scope=scope(), turn=turn())
    return core.step(state, event).state


def _reserved(limit: float) -> core.CoreState:
    """A turn asked at time zero with ``limit`` whose dispatch is queued."""
    state = _with_deadline(waiting_turn_state(turn()))
    [row] = state.sessions.invocations
    state = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={
                    "invocations": (
                        row.model_copy(update={"until": core.Until(at=limit), "limit": limit}),
                    )
                }
            )
        }
    )
    event = core.TurnInputsReserved(invocation=invocation(turn()), input_ids=())
    return core.step(state, event).state


def _blocked_after(state: core.CoreState, request_id: core.RequestId, at: float) -> bool:
    return _ended(reach(state, at).state, request_id)


@given(limit=_LIMIT, queued=_QUEUED)
def test_a_queued_session_acquisition_spends_none_of_the_turns_limit(
    limit: float, queued: float
) -> None:
    state = _asked(limit)
    record = _prepared(state, core.EnsureSession)
    request_id = record.request_id

    # Waiting for a slot, however long, ends nothing: only the run deadline bounds it.
    state = reach(state, queued).state
    assert not _ended(state, request_id)

    state = core.step(state, core.DispatchAuthorized(request_id=request_id, now_at=queued)).state
    assert not _blocked_after(state, request_id, queued + limit - 1.0)
    assert _blocked_after(state, request_id, queued + limit)


@given(limit=_LIMIT)
def test_a_turns_dispatch_is_queued_with_its_limit_and_no_earlier_end(limit: float) -> None:
    state = _reserved(limit)
    record = _prepared(state, core.DispatchTurn)

    assert record.limit_from_dispatch == limit
    assert record.until.at == _RUN_DEADLINE


def _queued_record(limit: float, *, now_at: float) -> core.CoreState:
    """A prepared request asked at ``now_at`` whose limit counts from its dispatch."""
    state = _silent(core.RequestPhase.PREPARED, core.ObservationStatus.UNKNOWN, terminal=False)
    [record] = state.records.records
    queued = record.model_copy(
        update={
            "observation": None,
            "until": core.Until(at=_RUN_DEADLINE),
            "limit_from_dispatch": limit,
        }
    )
    return _with_deadline(state).model_copy(
        update={
            "records": state.records.model_copy(update={"records": (queued,)}),
            "run": state.run.model_copy(update={"deadline_at": _RUN_DEADLINE, "now_at": now_at}),
        }
    )


@given(limit=_LIMIT, queued=_QUEUED)
def test_the_dispatch_time_starts_the_limit_and_is_core_time(limit: float, queued: float) -> None:
    state = _queued_record(limit, now_at=0.0)
    request_id = state.records.records[0].request_id

    state = core.step(state, core.DispatchAuthorized(request_id=request_id, now_at=queued)).state

    record = state.records.records[0]
    assert state.run.now_at == queued
    assert record.until.at == queued + limit
    assert record.dispatched_at == queued


@given(limit=_LIMIT, queued=_QUEUED)
def test_the_limit_never_reaches_past_the_run_deadline(limit: float, queued: float) -> None:
    nearly = _RUN_DEADLINE - queued % 10.0 - 1.0
    state = _queued_record(limit, now_at=0.0)
    request_id = state.records.records[0].request_id

    state = core.step(state, core.DispatchAuthorized(request_id=request_id, now_at=nearly)).state

    assert state.records.records[0].until.at == min(nearly + limit, _RUN_DEADLINE)


@given(limit=_LIMIT, queued=_QUEUED)
def test_a_retried_dispatch_keeps_the_limit_it_started_with(limit: float, queued: float) -> None:
    state = _queued_record(limit, now_at=0.0)
    request_id = state.records.records[0].request_id
    state = core.step(state, core.DispatchAuthorized(request_id=request_id, now_at=queued)).state
    first = state.records.records[0].until

    again = core.step(state, core.DispatchAuthorized(request_id=request_id, now_at=queued + 7.0))

    assert again.state.records.records[0].until == first
