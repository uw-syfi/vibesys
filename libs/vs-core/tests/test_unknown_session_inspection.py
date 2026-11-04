"""An unknown session whose last request ends without settling its turn is still carried."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core

from .test_session_acceptance_reconciliation import (
    reload_step,
    scope,
    turn,
    turn_observation,
    unacknowledged_success,
)


def _unknown_turn() -> core.Transition:
    """A dispatched turn whose executor answered that it does not know what happened."""
    dispatched, event = unacknowledged_success()
    unknown = event.model_copy(
        update={
            "output_json": None,
            "output_schema": None,
            "observation": event.observation.model_copy(
                update={"status": core.ObservationStatus.UNKNOWN, "terminal": False}
            ),
        }
    )
    return reload_step(dispatched.state, unknown)


def _ended(
    state: core.CoreState, phases: dict[core.RequestId, core.RequestPhase]
) -> core.CoreState:
    records = tuple(
        record.model_copy(update={"phase": phases[record.request_id]})
        if record.request_id in phases
        else record
        for record in state.records.records
    )
    return state.model_copy(
        update={"records": state.records.model_copy(update={"records": records})}
    )


@pytest.mark.parametrize(
    "inspection_phase", [core.RequestPhase.BLOCKED, core.RequestPhase.COMPLETED]
)
@pytest.mark.parametrize("dispatch_phase", [core.RequestPhase.BLOCKED, core.RequestPhase.COMPLETED])
def test_an_unknown_turn_whose_inspection_ended_unanswered_is_released_as_lost(
    inspection_phase: core.RequestPhase, dispatch_phase: core.RequestPhase
) -> None:
    """The session waits on the cancellation that releases the turn, as on an unknown answer.

    Before, the session dropped its wait and stayed unknown with nothing in flight: no orphan
    was reported, nothing released the turn, and the session refused every later turn.
    """
    unknown = _unknown_turn()
    [inspection] = unknown.requests
    [session] = unknown.state.sessions.sessions
    assert session.phase == core.SessionPhase.UNKNOWN
    assert session.wait is not None
    phases = {
        request: inspection_phase if request == inspection.request_id else dispatch_phase
        for request in session.wait.requests
    }

    result = reload_step(_ended(unknown.state, phases), core.ClockAdvanced(now_at=2.0))

    [cancel] = [request for request in result.requests if isinstance(request, core.CancelTurn)]
    assert cancel.invocation == unknown.state.sessions.invocations[0].invocation
    [session] = result.state.sessions.sessions
    assert session.wait is not None
    assert cancel.request_id in session.wait.requests


def _turn_ended(state: core.CoreState) -> core.CoreState:
    """The unknown session's turn already ended, so only the session's acceptance is unknown."""
    sessions = state.sessions
    invocations = tuple(
        invocation.model_copy(update={"phase": core.SessionPhase.TERMINAL})
        for invocation in sessions.invocations
    )
    return state.model_copy(
        update={"sessions": sessions.model_copy(update={"invocations": invocations})}
    )


@pytest.mark.parametrize(
    "inspection_phase", [core.RequestPhase.BLOCKED, core.RequestPhase.COMPLETED]
)
def test_an_unknown_session_whose_turn_ended_closes_as_lost_and_waits_on_the_close(
    inspection_phase: core.RequestPhase,
) -> None:
    """The turn succeeded but its inputs' acceptance is unknown, and the inspection ended unanswered.

    The session closes its lease as lost and waits on the close; it is no orphan. Before,
    it kept a wait on the ended requests: the completed inspection was all it would ever
    have, nothing inspected again, it refused every later turn, and the run halted.
    """
    unknown = _unknown_turn()
    [session] = unknown.state.sessions.sessions
    assert session.wait is not None
    assert session.resource_id is not None
    ended = _ended(
        _turn_ended(unknown.state), dict.fromkeys(session.wait.requests, inspection_phase)
    )

    result = reload_step(ended, core.ClockAdvanced(now_at=2.0))

    [close] = [request for request in result.requests if isinstance(request, core.CloseSession)]
    assert close.session_id == session.spec.session_id
    assert close.resource_id == session.resource_id
    [session] = result.state.sessions.sessions
    assert session.phase == core.SessionPhase.CLOSING
    assert session.wait is not None
    assert close.request_id in session.wait.requests
    assert core.orphan_waits(result.state) == ()


@pytest.mark.parametrize(
    "inspection_phase", [core.RequestPhase.BLOCKED, core.RequestPhase.COMPLETED]
)
def test_an_unknown_session_with_no_known_lease_keeps_its_wait_and_is_reported(
    inspection_phase: core.RequestPhase,
) -> None:
    """With no lease to close, the session keeps naming the ended requests: a loud orphan.

    Releasing it on the absence of an answer would free a lease core cannot account for.
    """
    unknown = _unknown_turn()
    state = _turn_ended(unknown.state)
    [session] = state.sessions.sessions
    assert session.wait is not None
    waited = session.wait.requests
    leaseless = session.model_copy(update={"resource_id": None})
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"sessions": (leaseless,)})}
    )

    result = reload_step(
        _ended(state, dict.fromkeys(waited, inspection_phase)), core.ClockAdvanced(now_at=2.0)
    )

    [session] = result.state.sessions.sessions
    assert session.phase == core.SessionPhase.UNKNOWN
    assert session.wait is not None
    assert set(session.wait.requests) == set(waited)
    assert not [request for request in result.requests if isinstance(request, core.CloseSession)]
    orphans = core.orphan_waits(result.state)
    assert [wait.waiter for wait in orphans] == [core.WaitKind.SESSION], orphans


def _ambiguous_ensure_in(epoch: int) -> core.Transition:
    """A lease request answered ambiguously (unknown acceptance) in host epoch *epoch*."""
    spec = turn()
    prepared = reload_step(
        core.initial_state(),
        core.TurnRequested(until=core.Until(at=100.0), scope=scope(), turn=spec),
    )
    records = prepared.state.records
    state = prepared.state.model_copy(
        update={"records": records.model_copy(update={"epoch": epoch})}
    )
    observation = turn_observation(
        prepared.requests[0], terminal=True, status=core.ObservationStatus.UNKNOWN
    )
    event = core.SessionObserved(session_id=spec.session.session_id, observation=observation)
    return reload_step(state, event)


@settings(max_examples=10, deadline=None)
@given(epochs=st.lists(st.integers(min_value=0, max_value=6), min_size=2, max_size=4, unique=True))
def test_a_sessions_inspection_is_a_new_request_in_each_host_epoch(epochs: list[int]) -> None:
    """Case (b): a restarted host asks afresh instead of reusing a dead host's inspection.

    The identity was one per target for the run's life, so a later epoch's ask returned the
    dead host's ended inspection and nothing new went out: an ambiguous ensure stayed
    unknown in every epoch after the first. Within one epoch the ask stays one request.
    """
    identities = set()
    for epoch in epochs:
        [inspection] = _ambiguous_ensure_in(epoch).requests
        assert isinstance(inspection, core.InspectRequest)
        [again] = _ambiguous_ensure_in(epoch).requests
        assert again == inspection
        identities.add(inspection.request_id)
    assert len(identities) == len(epochs)
