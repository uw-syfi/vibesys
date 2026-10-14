"""Every wait names what ends it: ``orphan_waits`` reports the ones nothing will end."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_core.api import (
    RESOLUTIONS,
    Access,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptsState,
    AttemptView,
    ChildLease,
    ClockAdvanced,
    ContinuationPhase,
    DispatchTurn,
    EventId,
    InspectRequest,
    ItemId,
    Observation,
    ObservationStatus,
    ObservePacing,
    OperationId,
    Outcome,
    PoolId,
    Producer,
    RecoveryBarrier,
    RecoveryCheck,
    RecoveryPhase,
    RegisteredOwnedJob,
    ReleaseDependency,
    RequestObserved,
    RequestPhase,
    RunStatus,
    SessionPhase,
    SessionsState,
    SessionView,
    TurnFailureKind,
    TurnResult,
    WaitingPhase,
    WaitKind,
    WorkspaceMode,
    WorkspacePlan,
    orphan_waits,
    phase_waits,
    step,
    waits,
)

from .test_measurements import submitted
from .test_request_recovery import (
    fixture_digest,
    initial_state,
    pending_record,
    recovering_state,
    turn_record,
)
from .test_write_turn_retention import write_turn_world

if TYPE_CHECKING:
    from vs_core.api import CoreState, RequestRecord

_WAITING = {
    RequestPhase.PREPARED,
    RequestPhase.DISPATCHED,
    RequestPhase.RECONCILING,
    SessionPhase.ACQUIRING,
    SessionPhase.EXECUTING,
    SessionPhase.CLOSING,
    RecoveryPhase.RECOVERING,
}


@pytest.mark.parametrize(
    "phase",
    [
        phase
        for enum in (RequestPhase, SessionPhase, RecoveryPhase, ContinuationPhase)
        for phase in enum
    ],
    ids=lambda phase: f"{type(phase).__name__}.{phase.name}",
)
def test_every_phase_is_classified_as_waiting_or_not(phase: WaitingPhase) -> None:
    """A new phase raises in ``phase_waits`` until someone names what ends its wait."""
    assert phase_waits(phase) is (phase in _WAITING)


def _executing(turn: RequestRecord, *, phase: RequestPhase) -> CoreState:
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
            "records": state.records.model_copy(
                update={"records": (turn.model_copy(update={"phase": phase}),)}
            ),
        }
    )


def _reply(turn: RequestRecord) -> RequestObserved:
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
        (RequestPhase.DISPATCHED, Producer.PENDING_REQUEST),
        (RequestPhase.RECONCILING, Producer.PENDING_REQUEST),
        (RequestPhase.COMPLETED, None),
    ],
)
def test_an_executing_session_needs_its_turn_in_flight(
    phase: RequestPhase, producer: Producer | None
) -> None:
    """The P1-1 shape: the turn's record completed and no reply is on its way."""
    found = waits(_executing(turn_record(), phase=phase))
    assert [(wait.waiter, wait.producer) for wait in found] == [(WaitKind.SESSION, producer)]
    assert bool(orphan_waits(_executing(turn_record(), phase=phase))) is (producer is None)


def test_a_committed_reply_awaiting_application_ends_the_wait() -> None:
    turn = turn_record()
    state = _executing(turn, phase=RequestPhase.COMPLETED)
    assert orphan_waits(state)
    assert not orphan_waits(state, (_reply(turn),))
    assert [wait.producer for wait in waits(state, (_reply(turn),))] == [Producer.OUTBOX_EVENT]


def test_a_terminal_run_waits_on_nothing() -> None:
    state = _executing(turn_record(), phase=RequestPhase.COMPLETED)
    done = state.model_copy(
        update={"run": state.run.model_copy(update={"status": RunStatus.TERMINAL})}
    )
    assert orphan_waits(done) == ()


def _recovering(target: RequestRecord) -> CoreState:
    state = recovering_state(target)
    barrier = RecoveryBarrier(
        epoch=1,
        phase=RecoveryPhase.RECOVERING,
        checks=(RecoveryCheck(target=target.request_id),),
    )
    return state.model_copy(
        update={"records": state.records.model_copy(update={"recovery": barrier})}
    )


def test_a_pending_recovery_check_needs_a_target_still_in_flight() -> None:
    """The P1-2 shape: the target completed, so inspection can never resolve its check."""
    live = _recovering(pending_record(phase=RequestPhase.DISPATCHED))
    assert [wait.producer for wait in waits(live)] == [Producer.PENDING_REQUEST]
    done = _recovering(
        pending_record(phase=RequestPhase.COMPLETED).model_copy(update={"observation": None})
    )
    assert [(wait.waiter, wait.producer) for wait in orphan_waits(done)] == [
        (WaitKind.RECOVERY_CHECK, None)
    ]


def test_a_pending_recovery_check_with_its_inspection_in_flight_is_not_an_orphan() -> None:
    done = _recovering(pending_record(identity="target", phase=RequestPhase.COMPLETED))
    inspection = pending_record(identity="inspection", phase=RequestPhase.PREPARED)
    check = done.records.recovery.checks[0].model_copy(update={"inspection": inspection.request_id})
    barrier = done.records.recovery.model_copy(update={"checks": (check,)})
    state = done.model_copy(
        update={
            "records": done.records.model_copy(
                update={
                    "recovery": barrier,
                    "records": (*done.records.records, inspection),
                }
            )
        }
    )
    assert orphan_waits(state) == ()


def _left_a_child(*, inspecting: bool, job_terminal: bool | None) -> CoreState:
    """A completed target whose observation named a child resource, its check still pending.

    *inspecting*: a child inspection of that resource is in flight. *job_terminal*: the job
    ledger tracks the resource as a job (ended or not); ``None``: no job holds it.
    """
    jobs, _ = submitted()
    job = jobs.evaluation.jobs[0]
    target = pending_record(identity="target", phase=RequestPhase.COMPLETED)
    target = target.model_copy(
        update={"request": target.request.model_copy(update={"scope": job.scope})}
    )
    state = _recovering(target)
    records = (target,)
    if inspecting:
        child = pending_record(identity="child", phase=RequestPhase.PREPARED)
        records += (
            child.model_copy(
                update={
                    "request": InspectRequest(
                        request_id=child.request_id,
                        scope=job.scope,
                        target=target.request_id,
                        resource_id=job.resource_id,
                    )
                }
            ),
        )
    lease = ChildLease(
        resource_id=job.resource_id, scope=job.scope, source_requests=(target.request_id,)
    )
    evaluation = state.evaluation
    if job_terminal is not None:
        evaluation = jobs.evaluation.model_copy(
            update={"jobs": (job.model_copy(update={"terminal": job_terminal}),)}
        )
    return state.model_copy(
        update={
            "evaluation": evaluation,
            "records": state.records.model_copy(update={"records": records, "children": (lease,)}),
        }
    )


@pytest.mark.parametrize("job_terminal", [None, False, True])
@pytest.mark.parametrize("inspecting", [True, False])
def test_a_recovery_check_waits_on_every_party_that_can_settle_the_child_its_target_left(
    *, inspecting: bool, job_terminal: bool | None
) -> None:
    """Chaos seeds 268 and 479: a close left a job it could not end, as a child of the close.

    Either a child inspection still in flight or the job's own ledger, still polling the job,
    can settle that child, so the check is an orphan only when neither is left.
    """
    state = _left_a_child(inspecting=inspecting, job_terminal=job_terminal)

    found = [(wait.waiter, wait.producer) for wait in waits(state)]

    expected = (
        Producer.PENDING_REQUEST
        if inspecting
        else Producer.TIMER
        if job_terminal is False
        else None
    )
    assert (WaitKind.RECOVERY_CHECK, expected) in found


@pytest.mark.parametrize("ledger", ["jobs", "registered", "unpaced"])
def test_a_check_on_a_left_job_waits_on_that_job_in_either_ledger_and_only_while_it_is_polled(
    ledger: str,
) -> None:
    """A left job settles the check only through its own wait: polled, in either job ledger.

    A registered job counts like a measurement job. A job nothing polls any more leaves the
    check an orphan too, instead of a timer that never fires hiding it.
    """
    state = _left_a_child(inspecting=False, job_terminal=False)
    job = state.evaluation.jobs[0]
    if ledger == "registered":
        registered = RegisteredOwnedJob(
            operation_id=OperationId(root="left-operation"),
            request_id=job.submission_id,
            scope=job.scope,
            resource_pool=PoolId(root="jobs"),
            resource_id=job.resource_id,
            pacing=job.pacing,
        )
        evaluation = state.evaluation.model_copy(
            update={"jobs": (), "registered_jobs": (registered,)}
        )
    else:
        pacing = ObservePacing() if ledger == "unpaced" else job.pacing
        evaluation = state.evaluation.model_copy(
            update={"jobs": (job.model_copy(update={"pacing": pacing}),)}
        )
    state = state.model_copy(update={"evaluation": evaluation})

    found = {(wait.waiter, wait.producer) for wait in waits(state)}

    expected = None if ledger == "unpaced" else Producer.TIMER
    assert (WaitKind.RECOVERY_CHECK, expected) in found
    assert (WaitKind.OWNED_JOB, expected) in found


def test_a_fresh_run_has_no_waits() -> None:
    assert waits(initial_state()) == ()


def test_an_observed_job_is_not_kept_alive_by_its_own_submission_record() -> None:
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
    failed = pending_record("dependency", RequestPhase.COMPLETED)
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
    dependent = pending_record("dependent", RequestPhase.PREPARED)
    request = dependent.request.model_copy(update={"depends_on": (failed.request_id,)})
    dependent = dependent.model_copy(
        update={"request": request, "payload_digest": fixture_digest(request)}
    )
    state = initial_state()
    return state.model_copy(
        update={"records": state.records.model_copy(update={"records": (failed, dependent)})}
    )


def test_a_prepared_request_behind_a_failed_dependency_has_no_dispatcher_and_is_cancelled() -> None:
    """Dispatch is refused for good once a dependency failed, so nothing would ever send it.

    The step closes it unsent, as it does for dependents that were already prepared when
    the dependency failed, instead of leaving the run to idle until its deadline.
    """
    state = _prepared_behind_failed_dependency()
    assert [w.waiter for w in orphan_waits(state)] == [WaitKind.DISPATCH]
    result = step(state, ClockAdvanced(now_at=2.0))
    dependent = result.state.records.records[1]
    assert dependent.phase == RequestPhase.COMPLETED
    assert dependent.observation is not None
    assert dependent.observation.status == ObservationStatus.CANCELLED
    assert not dependent.observation.accepted
    assert orphan_waits(result.state) == ()


def test_every_kind_of_wait_declares_how_core_ends_it_when_nothing_else_does() -> None:
    """A new ``WaitKind`` cannot be added without saying what resolves it."""
    assert set(RESOLUTIONS) == set(WaitKind)
    assert all(resolution.outcome in Outcome for resolution in RESOLUTIONS.values())


def _acquiring_without_a_request() -> CoreState:
    """A charged write turn still acquiring, with no request about its session (#2167)."""
    state, _ = write_turn_world(access=Access.WRITE_CANDIDATE, charged=True)
    [invocation] = state.sessions.invocations
    acquiring = invocation.model_copy(
        update={"phase": SessionPhase.ACQUIRING, "observation": None, "output_json": None}
    )
    idle = state.sessions.sessions[0].model_copy(update={"phase": SessionPhase.IDLE})
    return state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={"invocations": (acquiring,), "sessions": (idle,)}
            ),
            "records": state.records.model_copy(update={"records": ()}),
        }
    )


def test_a_turn_acquiring_with_no_request_is_a_wait_that_core_ends_with_a_named_reason() -> None:
    """Nothing carries the turn forward, so the step cancels it and says why."""
    state = _acquiring_without_a_request()
    assert [(w.waiter, w.producer) for w in orphan_waits(state)] == [
        (WaitKind.TURN_ACQUISITION, None)
    ]

    result = step(state, ClockAdvanced(now_at=2.0))

    assert orphan_waits(result.state) == ()
    [invocation] = result.state.sessions.invocations
    assert invocation.phase == SessionPhase.TERMINAL
    [ended] = [event for event in result.events if isinstance(event, TurnResult)]
    assert ended.failure == TurnFailureKind.CANCELLED
    assert "acquisition stalled" in ended.detail
