"""Every wait names what ends it: ``orphan_waits`` reports the ones nothing will end."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_core.api import (
    ContinuationPhase,
    DispatchTurn,
    EventId,
    IntentPhase,
    Observation,
    ObservationStatus,
    Producer,
    RecoveryBarrier,
    RecoveryCheck,
    RecoveryPhase,
    RequestObserved,
    RunStatus,
    SessionPhase,
    SessionsState,
    SessionView,
    WaitingPhase,
    WaitKind,
    orphan_waits,
    phase_waits,
    waits,
)

from .test_intent_recovery import (
    initial_state,
    pending_intent,
    recovering_state,
    turn_intent,
)

if TYPE_CHECKING:
    from vs_core.api import CoreState, Intent

_WAITING = {
    IntentPhase.PREPARED,
    IntentPhase.DISPATCHED,
    IntentPhase.RECONCILING,
    SessionPhase.ACQUIRING,
    SessionPhase.EXECUTING,
    SessionPhase.CLOSING,
    RecoveryPhase.RECOVERING,
}


@pytest.mark.parametrize(
    "phase",
    [
        phase
        for enum in (IntentPhase, SessionPhase, RecoveryPhase, ContinuationPhase)
        for phase in enum
    ],
    ids=lambda phase: f"{type(phase).__name__}.{phase.name}",
)
def test_every_phase_is_classified_as_waiting_or_not(phase: WaitingPhase) -> None:
    """A new phase raises in ``phase_waits`` until someone names what ends its wait."""
    assert phase_waits(phase) is (phase in _WAITING)


def _executing(turn: Intent, *, phase: IntentPhase) -> CoreState:
    assert isinstance(turn.request, DispatchTurn)
    session = SessionView(
        spec=turn.request.turn.session,
        scope=turn.request.scope,
        generation=0,
        phase=SessionPhase.EXECUTING,
    )
    state = initial_state()
    return state.model_copy(
        update={
            "sessions": SessionsState(sessions=(session,)),
            "intents": state.intents.model_copy(
                update={"intents": (turn.model_copy(update={"phase": phase}),)}
            ),
        }
    )


def _reply(turn: Intent) -> RequestObserved:
    return RequestObserved(
        observation=Observation(
            event_id=EventId(root="reply"),
            request_id=turn.request_id,
            scope=turn.request.scope,
            sequence=0,
            observed_at=1.0,
            status=ObservationStatus.SUCCEEDED,
        )
    )


@pytest.mark.parametrize(
    ("phase", "producer"),
    [
        (IntentPhase.DISPATCHED, Producer.PENDING_REQUEST),
        (IntentPhase.RECONCILING, Producer.PENDING_REQUEST),
        (IntentPhase.COMPLETED, None),
    ],
)
def test_an_executing_session_needs_its_turn_in_flight(
    phase: IntentPhase, producer: Producer | None
) -> None:
    """The P1-1 shape: the turn's intent completed and no reply is on its way."""
    found = waits(_executing(turn_intent(), phase=phase))
    assert [(wait.waiter, wait.producer) for wait in found] == [(WaitKind.SESSION, producer)]
    assert bool(orphan_waits(_executing(turn_intent(), phase=phase))) is (producer is None)


def test_a_committed_reply_awaiting_application_ends_the_wait() -> None:
    turn = turn_intent()
    state = _executing(turn, phase=IntentPhase.COMPLETED)
    assert orphan_waits(state)
    assert not orphan_waits(state, (_reply(turn),))
    assert [wait.producer for wait in waits(state, (_reply(turn),))] == [Producer.OUTBOX_EVENT]


def test_a_terminal_run_waits_on_nothing() -> None:
    state = _executing(turn_intent(), phase=IntentPhase.COMPLETED)
    done = state.model_copy(
        update={"run": state.run.model_copy(update={"status": RunStatus.TERMINAL})}
    )
    assert orphan_waits(done) == ()


def _recovering(target: Intent) -> CoreState:
    state = recovering_state(target)
    barrier = RecoveryBarrier(
        epoch=1,
        phase=RecoveryPhase.RECOVERING,
        checks=(RecoveryCheck(target=target.request_id),),
    )
    return state.model_copy(
        update={"intents": state.intents.model_copy(update={"recovery": barrier})}
    )


def test_a_pending_recovery_check_needs_a_target_still_in_flight() -> None:
    """The P1-2 shape: the target completed, so inspection can never resolve its check."""
    live = _recovering(pending_intent(phase=IntentPhase.DISPATCHED))
    assert [wait.producer for wait in waits(live)] == [Producer.PENDING_REQUEST]
    done = _recovering(
        pending_intent(phase=IntentPhase.COMPLETED).model_copy(update={"observation": None})
    )
    assert [(wait.waiter, wait.producer) for wait in orphan_waits(done)] == [
        (WaitKind.RECOVERY_CHECK, None)
    ]


def test_a_pending_recovery_check_with_its_inspection_in_flight_is_not_an_orphan() -> None:
    done = _recovering(pending_intent(identity="target", phase=IntentPhase.COMPLETED))
    inspection = pending_intent(identity="inspection", phase=IntentPhase.PREPARED)
    check = done.intents.recovery.checks[0].model_copy(update={"inspection": inspection.request_id})
    barrier = done.intents.recovery.model_copy(update={"checks": (check,)})
    state = done.model_copy(
        update={
            "intents": done.intents.model_copy(
                update={
                    "recovery": barrier,
                    "intents": (*done.intents.intents, inspection),
                }
            )
        }
    )
    assert orphan_waits(state) == ()


def test_a_fresh_run_has_no_waits() -> None:
    assert waits(initial_state()) == ()
