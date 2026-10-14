"""An unknown session whose last request ends without settling its turn is still carried."""

import pytest

import vs_core.api as core

from .test_session_acceptance_reconciliation import reload_step, unacknowledged_success


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
