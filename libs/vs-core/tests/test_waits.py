"""Every wait names what ends it: ``orphan_waits`` reports the ones nothing will end."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_core.api import (
    RESOLUTIONS,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptsState,
    AttemptView,
    ClockAdvanced,
    ContinuationPhase,
    DispatchTurn,
    EventId,
    IntentPhase,
    ItemId,
    Observation,
    ObservationStatus,
    ObservePacing,
    Outcome,
    Producer,
    RecoveryBarrier,
    RecoveryCheck,
    RecoveryPhase,
    ReleaseDependency,
    RequestObserved,
    RunStatus,
    SessionPhase,
    SessionsState,
    SessionView,
    WaitingPhase,
    WaitKind,
    WorkspaceMode,
    WorkspacePlan,
    orphan_waits,
    phase_waits,
    step,
    waits,
)

from .test_intent_recovery import (
    fixture_digest,
    initial_state,
    pending_intent,
    recovering_state,
    turn_intent,
)
from .test_measurements import submitted

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


def test_an_observed_job_is_not_kept_alive_by_its_own_submission_intent() -> None:
    state, _ = submitted()
    job = state.evaluation.jobs[0]
    assert job.observation is not None
    assert [w for w in orphan_waits(state) if w.subject == job.resource_id.root] == []
    unpaced = job.model_copy(update={"pacing": ObservePacing()})
    stalled = state.model_copy(
        update={"evaluation": state.evaluation.model_copy(update={"jobs": (unpaced,)})}
    )
    assert [w.subject for w in orphan_waits(stalled)] == [job.resource_id.root]


def _closing_over(*, job_ended: bool) -> tuple[CoreState, str]:
    state, _ = submitted()
    job = state.evaluation.jobs[0]
    assert job.observation is not None
    ended = job.observation.model_copy(
        update={"status": ObservationStatus.FAILED, "terminal": True, "released": True}
    )
    job = job.model_copy(
        update={
            "observation": ended if job_ended else job.observation,
            "status": ended.status if job_ended else job.status,
            "terminal": job_ended,
            "released": job_ended,
        }
    )
    attempt = AttemptView(
        attempt_id=AttemptId(root="attempt"),
        item_id=ItemId(root="item"),
        generation=0,
        phase=AttemptPhase.CLOSING,
        workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline),
        budget=AttemptBudget(),
        release_dependencies=(ReleaseDependency(kind="job", identity=job.resource_id),),
    )
    closing = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(attempt,)),
            "evaluation": state.evaluation.model_copy(update={"jobs": (job,)}),
        }
    )
    return closing, job.resource_id.root


def test_a_closing_attempt_still_listing_a_job_that_has_been_released_is_an_orphan() -> None:
    """The release already happened, so nothing is left to tell the attempt (the v8 stall)."""
    state, resource = _closing_over(job_ended=True)
    found = [w for w in orphan_waits(state) if w.waiter == WaitKind.RELEASE_DEPENDENCY]
    assert [w.subject for w in found] == [resource]


def test_a_closing_attempt_waiting_on_a_job_that_still_runs_is_named_by_the_job() -> None:
    state, _ = _closing_over(job_ended=False)
    assert [w for w in waits(state) if w.waiter == WaitKind.RELEASE_DEPENDENCY] == []


def _prepared_behind_failed_dependency() -> CoreState:
    """A request prepared after the request it depends on had already failed for good."""
    failed = pending_intent("dependency", IntentPhase.COMPLETED)
    answer = Observation(
        event_id=EventId(root="dependency-failed"),
        request_id=failed.request_id,
        scope=failed.request.scope,
        sequence=1,
        observed_at=1.0,
        status=ObservationStatus.FAILED,
        accepted=False,
        terminal=True,
    )
    failed = failed.model_copy(update={"observation": answer, "sequence": 1})
    dependent = pending_intent("dependent", IntentPhase.PREPARED)
    request = dependent.request.model_copy(update={"depends_on": (failed.request_id,)})
    dependent = dependent.model_copy(
        update={"request": request, "payload_digest": fixture_digest(request)}
    )
    state = initial_state()
    return state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (failed, dependent)})}
    )


def test_a_prepared_request_behind_a_failed_dependency_has_no_dispatcher_and_is_cancelled() -> None:
    """Dispatch is refused for good once a dependency failed, so nothing would ever send it.

    The step closes it unsent, as it does for dependents that were already prepared when
    the dependency failed, instead of leaving the run to idle until its deadline.
    """
    state = _prepared_behind_failed_dependency()
    assert [w.waiter for w in orphan_waits(state)] == [WaitKind.DISPATCH]
    result = step(state, ClockAdvanced(now_at=2.0))
    dependent = result.state.intents.intents[1]
    assert dependent.phase == IntentPhase.COMPLETED
    assert dependent.observation is not None
    assert dependent.observation.status == ObservationStatus.CANCELLED
    assert not dependent.observation.accepted
    assert orphan_waits(result.state) == ()


def test_every_kind_of_wait_declares_how_core_ends_it_when_nothing_else_does() -> None:
    """A new ``WaitKind`` cannot be added without saying what resolves it."""
    assert set(RESOLUTIONS) == set(WaitKind)
    assert all(resolution.outcome in Outcome for resolution in RESOLUTIONS.values())
