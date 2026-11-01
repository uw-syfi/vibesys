"""Every wait names what ends it: ``orphan_waits`` reports the ones nothing will end."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import (
    MAY_WAIT_ON,
    RESOLUTIONS,
    Access,
    AreaChange,
    ArtifactId,
    ArtifactRef,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptsState,
    AttemptView,
    ChildLease,
    ClockAdvanced,
    CloseSession,
    ContinuationPhase,
    ContractError,
    CoreReducers,
    DispatchTurn,
    EnsureSession,
    EvaluationContext,
    EvaluationEvent,
    EvaluationState,
    EventId,
    InputId,
    InspectRequest,
    ItemId,
    JobTerminationRequested,
    Observation,
    ObservationStatus,
    ObserveOwnedJob,
    OperationId,
    Outcome,
    PoolId,
    Producer,
    RecoveryBarrier,
    RecoveryCheck,
    RecoveryPhase,
    RegisteredOwnedJob,
    ReleaseDependency,
    RequestId,
    RequestObserved,
    RequestPhase,
    ResourceId,
    RunStatus,
    ScopeInputTarget,
    SessionInput,
    SessionInputReceived,
    SessionPhase,
    SessionsContext,
    SessionsEvent,
    SessionsState,
    SessionView,
    TurnFailureKind,
    TurnResult,
    WaitingPhase,
    WaitKind,
    WaitOn,
    WaitSubject,
    WorkspaceMode,
    WorkspacePlan,
    orphan_waits,
    owned_job_subject,
    phase_waits,
    step,
    subjects,
    waits,
)
from vs_core.api.testing import reach
from vs_sim.api.testing import examples

from .test_measurements import submitted
from .test_request_recovery import (
    fixture_digest,
    initial_state,
    pending_record,
    recovering_state,
    turn_record,
)
from .test_write_turn_retention import write_turn_world
from .wait_support import OPEN

if TYPE_CHECKING:
    from collections.abc import Callable

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
        wait=WaitOn(requests=(turn.request_id,)),
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


@pytest.mark.parametrize("phase", [RequestPhase.DISPATCHED, RequestPhase.COMPLETED])
def test_a_reply_queued_for_the_named_turn_keeps_the_session_moving(
    phase: RequestPhase,
) -> None:
    """A turn's reply travels on its session's owner event, applied a step after the turn ends.

    The shell commits the turn's observation and the owner event together and applies the
    event next, so a session naming the turn is moved by the queued reply even once the turn's
    record completed. Without that reply, a completed turn moves the session no more.
    """
    turn = turn_record()
    state = _executing(turn, phase=phase)
    assert not orphan_waits(state, (_reply(turn),))
    assert bool(orphan_waits(state)) is (phase == RequestPhase.COMPLETED)


def test_a_terminal_run_waits_on_nothing() -> None:
    state = _executing(turn_record(), phase=RequestPhase.COMPLETED)
    done = state.model_copy(
        update={"run": state.run.model_copy(update={"status": RunStatus.TERMINAL})}
    )
    assert orphan_waits(done) == ()


def _recovering(target: RequestRecord) -> CoreState:
    """A pending check on *target*, recorded while the target was open (#2428)."""
    state = recovering_state(target)
    barrier = RecoveryBarrier(
        epoch=1,
        phase=RecoveryPhase.RECOVERING,
        checks=(
            RecoveryCheck(target=target.request_id, wait=WaitOn(requests=(target.request_id,))),
        ),
    )
    return state.model_copy(
        update={"records": state.records.model_copy(update={"recovery": barrier})}
    )


def _check_naming(
    state: CoreState, *requests: RequestId, subjects: tuple[WaitSubject, ...] = ()
) -> CoreState:
    """*state* with its one recovery check waiting on *requests* and *subjects* as well."""
    (check,) = state.records.recovery.checks
    assert check.wait is not None
    wait = WaitOn(
        requests=(*check.wait.requests, *requests), subjects=(*check.wait.subjects, *subjects)
    )
    barrier = state.records.recovery.model_copy(
        update={"checks": (check.model_copy(update={"wait": wait}),)}
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
    done = _check_naming(done, inspection.request_id)
    barrier = barrier.model_copy(update={"checks": done.records.recovery.checks})
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
            update={
                "jobs": (
                    job.model_copy(
                        update={
                            "terminal": job_terminal,
                            "wait": None if job_terminal else job.wait,
                        }
                    ),
                )
            }
        )
    state = state.model_copy(
        update={
            "evaluation": evaluation,
            "records": state.records.model_copy(update={"records": records, "children": (lease,)}),
        }
    )
    named = tuple(row.request_id for row in records[1:])
    subjects = (owned_job_subject(job),) if job_terminal is not None else ()
    return _check_naming(state, *named, subjects=subjects)


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

    # Both parties named: the report is the one that fires last, the job's poll timer.
    expected = (
        Producer.TIMER
        if job_terminal is False
        else Producer.PENDING_REQUEST
        if inspecting
        else None
    )
    assert (WaitKind.RECOVERY_CHECK, expected) in found


def _waiting_on(state: CoreState, job_wait: str) -> CoreState:
    """The left job of *state* waiting on its poll timer, or on a poll still open or ended.

    The poll is a ledger record naming the job; "ended" is one core already closed.
    """
    (job,) = state.evaluation.jobs
    if job_wait == "timer":
        assert job.wait is not None
        assert job.wait.timer is not None
        return state
    poll = pending_record(
        identity="poll",
        phase=RequestPhase.DISPATCHED if job_wait == "open-poll" else RequestPhase.COMPLETED,
    )
    poll = poll.model_copy(
        update={
            "request": ObserveOwnedJob(
                request_id=poll.request_id, scope=job.scope, resource_id=job.resource_id
            )
        }
    )
    waiting = job.model_copy(update={"wait": WaitOn(requests=(poll.request_id,))})
    return state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(update={"jobs": (waiting,)}),
            "records": state.records.model_copy(update={"records": (*state.records.records, poll)}),
        }
    )


def _as_registered(state: CoreState) -> CoreState:
    """The same left job, held by the registered-job ledger instead."""
    (job,) = state.evaluation.jobs
    registered = RegisteredOwnedJob(
        operation_id=OperationId(root="left-operation"),
        request_id=job.submission_id,
        scope=job.scope,
        resource_pool=PoolId(root="jobs"),
        resource_id=job.resource_id,
        wait=job.wait,
    )
    barrier = state.records.recovery.model_copy(
        update={
            "checks": tuple(
                check.model_copy(
                    update={
                        "wait": check.wait.model_copy(
                            update={"subjects": (owned_job_subject(registered),)}
                        )
                    }
                )
                if check.wait is not None
                else check
                for check in state.records.recovery.checks
            )
        }
    )
    return state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(
                update={"jobs": (), "registered_jobs": (registered,)}
            ),
            "records": state.records.model_copy(update={"recovery": barrier}),
        }
    )


_JOB_WAIT_PRODUCER = {
    "timer": Producer.TIMER,
    "open-poll": Producer.PENDING_REQUEST,
    "ended-poll": None,
}


@pytest.mark.parametrize("observed", [False, True], ids=["no-outbox", "answer-in-outbox"])
@pytest.mark.parametrize("job_wait", sorted(_JOB_WAIT_PRODUCER))
@pytest.mark.parametrize("ledger", ["jobs", "registered"])
def test_a_check_on_a_left_job_waits_on_what_the_job_recorded_in_either_ledger(
    ledger: str, job_wait: str, *, observed: bool
) -> None:
    """A left job settles the check only through the wait it recorded, with the same producer.

    A registered job counts like a measurement job. A job whose recorded wait names only an
    ended request leaves the check an orphan too. The shell's pending inputs take no part: an
    answer still queued in the shell belongs to a request core still holds open, so the
    queue cannot change the verdict (it once could, and a resolver ended a check the queue
    was about to settle).
    """
    state = _waiting_on(_left_a_child(inspecting=False, job_terminal=False), job_wait)
    if ledger == "registered":
        state = _as_registered(state)
    outbox = (
        (
            RequestObserved(
                observation=Observation(
                    event_id=EventId(root="poll-answered"),
                    request_id=RequestId(root="poll"),
                    scope=state.records.children[0].scope,
                    sequence=1,
                    observed_at=1.0,
                    status=ObservationStatus.PENDING,
                )
            ),
        )
        if observed
        else ()
    )

    found = {(wait.waiter, wait.producer) for wait in waits(state, outbox)}

    expected = _JOB_WAIT_PRODUCER[job_wait]
    assert (WaitKind.RECOVERY_CHECK, expected) in found
    assert (WaitKind.OWNED_JOB, expected) in found


@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(kinds=st.lists(st.sampled_from(["timer", "open-poll"]), min_size=1, max_size=4))
def test_a_check_on_several_left_jobs_waits_on_the_one_that_settles_last_in_any_order(
    kinds: list[str],
) -> None:
    """A check whose target left several jobs ends once the last of them settles.

    Each child job is moved by what its own wait names. The check reports the one that fires
    last (a timer after a request in flight), and never depends on the order the children
    were recorded in; it once took the first child's.
    """
    state = _left_a_child(inspecting=False, job_terminal=False)
    (job,) = state.evaluation.jobs
    (lease,) = state.records.children
    jobs, leases, records = [], [], list(state.records.records)
    for index, kind in enumerate(kinds):
        resource = ResourceId(root=f"left-{index}")
        wait = job.wait
        if kind == "open-poll":
            poll = pending_record(identity=f"poll-{index}", phase=RequestPhase.DISPATCHED)
            records.append(
                poll.model_copy(
                    update={
                        "request": ObserveOwnedJob(
                            request_id=poll.request_id, scope=job.scope, resource_id=resource
                        )
                    }
                )
            )
            wait = WaitOn(requests=(poll.request_id,))
        jobs.append(job.model_copy(update={"resource_id": resource, "wait": wait}))
        leases.append(lease.model_copy(update={"resource_id": resource}))
    expected = Producer.TIMER if "timer" in kinds else Producer.PENDING_REQUEST

    state = _check_naming(state, subjects=tuple(owned_job_subject(row) for row in jobs))
    for order in (leases, leases[::-1]):
        left = state.model_copy(
            update={
                "evaluation": state.evaluation.model_copy(update={"jobs": tuple(jobs)}),
                "records": state.records.model_copy(
                    update={"records": tuple(records), "children": tuple(order)}
                ),
            }
        )
        checks = [wait.producer for wait in waits(left) if wait.waiter == WaitKind.RECOVERY_CHECK]
        assert checks == [expected]


def test_a_fresh_run_has_no_waits() -> None:
    assert waits(initial_state()) == ()


def test_an_observed_job_is_not_kept_alive_by_its_own_submission_record() -> None:
    """The submission stays open until its job ends, but the job no longer waits on it."""
    state, _ = submitted()
    job = state.evaluation.jobs[0]
    assert job.observation is not None
    assert job.wait is not None
    assert job.submission_id not in job.wait.requests
    assert [w for w in orphan_waits(state) if w.subject == job.resource_id.root] == []


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
            "wait": None if job_ended else job.wait,
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
    """A charged write turn acquiring on an idle session that waits on nothing (#2167)."""
    state, _ = write_turn_world(access=Access.WRITE_CANDIDATE, charged=True)
    [invocation] = state.sessions.invocations
    session = WaitSubject(kind=WaitKind.SESSION, identity=invocation.invocation.session_id.root)
    acquiring = invocation.model_copy(
        update={
            "phase": SessionPhase.ACQUIRING,
            "observation": None,
            "output_json": None,
            "wait": WaitOn(subjects=(session,)),
        }
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


def _session_record(state: CoreState, identity: str, phase: RequestPhase) -> RequestRecord:
    """A lease request about the acquiring turn's session, in *phase*."""
    [invocation] = state.sessions.invocations
    record = pending_record(identity=identity, phase=phase)
    assert isinstance(record.request, EnsureSession)
    spec = record.request.spec.model_copy(update={"session_id": invocation.invocation.session_id})
    return record.model_copy(update={"request": record.request.model_copy(update={"spec": spec})})


def _inspection(identity: str, target: RequestRecord, phase: RequestPhase) -> RequestRecord:
    """An inspection of *target*, as recovery or a bound sends, in *phase*."""
    record = pending_record(identity=identity, phase=phase)
    request = InspectRequest(
        request_id=record.request_id, scope=target.request.scope, target=target.request_id
    )
    return record.model_copy(update={"request": request})


def _acquiring_with(
    records: tuple[RequestRecord, ...],
    named: tuple[RequestRecord, ...] | None = None,
    phase: SessionPhase = SessionPhase.ACQUIRING,
) -> CoreState:
    """The acquiring turn, its session in *phase* waiting on *named* (default: all *records*)."""
    state = _acquiring_without_a_request()
    named = records if named is None else named
    [session] = state.sessions.sessions
    if named:
        session = session.model_copy(
            update={
                "phase": phase,
                "wait": WaitOn(requests=tuple(row.request_id for row in named)),
            }
        )
    return state.model_copy(
        update={
            "records": state.records.model_copy(update={"records": records}),
            "sessions": state.sessions.model_copy(update={"sessions": (session,)}),
        }
    )


def test_a_turn_whose_session_lease_is_being_inspected_is_carried_and_not_ended() -> None:
    """The step ends an acquiring turn only when its wait is an orphan, never before.

    The turn's lease request completed ambiguously, so its session is unknown and waits on
    the inspection it sent: its answer is what moves the session, and the turn waits on the
    session, so the turn's wait has a producer. Once the inspection ended without moving the
    session, nothing carries the turn and the step ends it.
    """
    lease = _session_record(_acquiring_without_a_request(), "lease", RequestPhase.COMPLETED)
    out = _inspection("inspect", lease, RequestPhase.DISPATCHED)
    inspecting = _acquiring_with((lease, out), (out,), SessionPhase.UNKNOWN)

    assert orphan_waits(inspecting) == ()
    [carried] = step(inspecting, ClockAdvanced(now_at=2.0)).state.sessions.invocations
    assert carried.phase == SessionPhase.ACQUIRING

    done = _inspection("inspect", lease, RequestPhase.COMPLETED)
    answered = _acquiring_with((lease, done), (done,), SessionPhase.UNKNOWN)
    assert {w.waiter for w in orphan_waits(answered)} == {
        WaitKind.SESSION,
        WaitKind.TURN_ACQUISITION,
    }
    [ended] = step(answered, ClockAdvanced(now_at=2.0)).state.sessions.invocations
    assert ended.phase == SessionPhase.TERMINAL


_PHASES = st.sampled_from(list(RequestPhase))


@st.composite
def _records_about_the_turn(draw: st.DrawFn) -> tuple[RequestRecord, ...]:
    """Lease requests about the acquiring turn's session, and inspections of earlier records."""
    base = _acquiring_without_a_request()
    records: list[RequestRecord] = []
    for index in range(draw(st.integers(0, 4))):
        if records and draw(st.booleans()):
            target = draw(st.sampled_from(records))
            records.append(_inspection(f"inspect-{index}", target, draw(_PHASES)))
        else:
            records.append(_session_record(base, f"lease-{index}", draw(_PHASES)))
    return tuple(records)


@settings(max_examples=examples(pr=20, nightly=500), deadline=None)
@given(records=_records_about_the_turn(), answered=st.sets(st.integers(0, 3)))
def test_every_wait_reads_the_recorded_waits_and_the_step_ends_exactly_the_orphans(
    records: tuple[RequestRecord, ...], answered: set[int]
) -> None:
    """``waits`` and the step's resolvers read the same recorded waits.

    Each subject is reported once, with or without committed answers in the outbox. The
    acquiring turn waits on its session, which waits on the requests it named: the turn's wait
    is an orphan exactly when none of them is open, and the step ends the turn exactly then.
    """
    state = _acquiring_with(records)
    outbox = tuple(
        RequestObserved(
            observation=Observation(
                event_id=EventId(root=f"answer-{index}"),
                request_id=records[index].request_id,
                scope=records[index].request.scope,
                sequence=1,
                observed_at=1.0,
                status=ObservationStatus.SUCCEEDED,
            )
        )
        for index in answered
        if index < len(records)
    )
    for pending in ((), outbox):
        found = waits(state, pending)
        assert [(w.waiter, w.subject) for w in found] == [
            (subject.kind, subject.identity) for subject in subjects(state)
        ]

    orphaned = WaitKind.TURN_ACQUISITION in {w.waiter for w in orphan_waits(state)}
    assert orphaned == (not any(phase_waits(row.phase) for row in records))
    [turn] = step(state, ClockAdvanced(now_at=2.0)).state.sessions.invocations
    assert (turn.phase == SessionPhase.TERMINAL) == orphaned


@settings(max_examples=examples(pr=30, nightly=1000), deadline=None)
@given(
    phases=st.lists(_PHASES, max_size=3),
    timer=st.booleans(),
    registered=st.booleans(),
)
def test_a_job_is_an_orphan_exactly_when_its_recorded_wait_names_nothing_open(
    phases: list[RequestPhase], *, timer: bool, registered: bool
) -> None:
    """A job's wait is an orphan if and only if no request it names is open and it has no timer.

    The verdict reads only the wait the job recorded and the phases of the requests it names;
    nothing else in the ledger, such as other polls of the same job, is consulted.
    """
    if not phases and not timer:
        timer = True
    state, _ = submitted()
    (job,) = state.evaluation.jobs
    polls = [
        record.model_copy(
            update={
                "request": ObserveOwnedJob(
                    request_id=record.request_id, scope=job.scope, resource_id=job.resource_id
                )
            }
        )
        for index, phase in enumerate(phases)
        for record in [pending_record(identity=f"poll-{index}", phase=phase)]
    ]
    wait = WaitOn(requests=tuple(poll.request_id for poll in polls), timer=1.0 if timer else None)
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(
                update={"jobs": (job.model_copy(update={"wait": wait}),)}
            ),
            "records": state.records.model_copy(
                update={"records": (*state.records.records, *polls)}
            ),
        }
    )
    if registered:
        state = _as_registered(state)
    open_phases = {RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING}

    orphaned = [wait for wait in orphan_waits(state) if wait.waiter == WaitKind.OWNED_JOB]

    assert bool(orphaned) == (not timer and not open_phases.intersection(phases))


def _rewaiting(
    wait: WaitOn | None, *, terminal: bool
) -> Callable[[EvaluationState, EvaluationContext, EvaluationEvent], AreaChange[EvaluationState]]:
    """An evaluation reducer that records *wait* on the one live job, ending it if *terminal*."""

    def reduce(
        state: EvaluationState, _context: EvaluationContext, _event: EvaluationEvent
    ) -> AreaChange[EvaluationState]:
        (job,) = state.jobs
        changed = job.model_copy(update={"wait": wait, "terminal": terminal})
        return AreaChange(state=state.model_copy(update={"jobs": (changed,)}))

    return reduce


@settings(max_examples=examples(pr=20, nightly=500), deadline=None)
@given(
    named=st.one_of(st.none(), _PHASES),
    timer=st.booleans(),
    terminal=st.booleans(),
)
def test_a_step_that_records_a_wait_on_nothing_open_fails_loudly(
    named: RequestPhase | None, *, timer: bool, terminal: bool
) -> None:
    """I2 of #2428: every request a recorded wait names exists and is open when it is recorded.

    *named* is the phase of the one request the new wait names (None: the wait names a request
    the ledger never held). A live job without a wait, or an ended one with a wait, is refused
    the same way.
    """
    state, _ = submitted()
    (job,) = state.evaluation.jobs
    poll = pending_record(identity="poll", phase=named or RequestPhase.DISPATCHED)
    poll = poll.model_copy(
        update={
            "request": ObserveOwnedJob(
                request_id=poll.request_id, scope=job.scope, resource_id=job.resource_id
            )
        }
    )
    if named is not None:
        state = state.model_copy(
            update={
                "records": state.records.model_copy(
                    update={"records": (*state.records.records, poll)}
                )
            }
        )
    requests = (poll.request_id,) if named is not None or not timer else ()
    wait = WaitOn(requests=requests, timer=state.run.now_at + 1.0 if timer else None)
    if terminal:
        wait = None
    reducers = CoreReducers(evaluation=_rewaiting(wait, terminal=terminal))
    event = JobTerminationRequested(resource_id=job.resource_id, cause="deadline")
    open_phases = {RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING}
    sound = terminal or (named in open_phases if requests else timer)

    if sound:
        step(state, event, reducers=reducers)
    else:
        with pytest.raises(ContractError, match="wait"):
            step(state, event, reducers=reducers)


@pytest.mark.parametrize("terminal", [False, True])
def test_a_step_that_leaves_a_job_live_without_a_wait_or_ended_with_one_fails_loudly(
    *, terminal: bool
) -> None:
    state, _ = submitted()
    (job,) = state.evaluation.jobs
    wait = WaitOn(timer=state.run.now_at + 1.0) if terminal else None
    reducers = CoreReducers(evaluation=_rewaiting(wait, terminal=terminal))
    event = JobTerminationRequested(resource_id=job.resource_id, cause="deadline")

    with pytest.raises(ContractError, match="live without a wait, or ended with one"):
        step(state, event, reducers=reducers)


@settings(max_examples=examples(pr=30, nightly=1000), deadline=None)
@given(
    phases=st.lists(_PHASES, min_size=1, max_size=3),
    session_phase=st.sampled_from(
        [SessionPhase.ACQUIRING, SessionPhase.EXECUTING, SessionPhase.CLOSING]
    ),
)
def test_a_session_is_an_orphan_exactly_when_its_recorded_wait_names_nothing_open(
    phases: list[RequestPhase], session_phase: SessionPhase
) -> None:
    """The verdict reads only the requests the session named and their phases."""
    turn = turn_record()
    records = tuple(
        turn.model_copy(update={"request_id": RequestId(root=f"named-{index}"), "phase": phase})
        for index, phase in enumerate(phases)
    )
    state = _executing(turn, phase=RequestPhase.COMPLETED)
    (session,) = state.sessions.sessions
    waiting = session.model_copy(
        update={
            "phase": session_phase,
            "wait": WaitOn(requests=tuple(row.request_id for row in records)),
        }
    )
    state = state.model_copy(
        update={
            "sessions": SessionsState(sessions=(waiting,)),
            "records": state.records.model_copy(
                update={"records": (*state.records.records, *records)}
            ),
        }
    )
    open_phases = {RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING}

    orphaned = [wait for wait in orphan_waits(state) if wait.waiter == WaitKind.SESSION]

    assert bool(orphaned) == (not open_phases.intersection(phases))


@pytest.mark.parametrize("other", [RequestPhase.DISPATCHED, RequestPhase.BLOCKED])
def test_a_session_whose_request_ended_without_moving_it_waits_on_the_rest(
    other: RequestPhase,
) -> None:
    """A blocked request wakes its session, which waits on its other request still open.

    With none open the session keeps its wait and stays an orphan, so the run halts with it
    named (``RESOLUTIONS``) instead of the session claiming a release nobody proved.
    """
    turn = turn_record()
    blocked = turn.model_copy(
        update={"request_id": RequestId(root="blocked"), "phase": RequestPhase.BLOCKED}
    )
    rest = turn.model_copy(update={"request_id": RequestId(root="rest"), "phase": other})
    state = _executing(turn, phase=RequestPhase.COMPLETED)
    (session,) = state.sessions.sessions
    waiting = session.model_copy(
        update={"wait": WaitOn(requests=(blocked.request_id, rest.request_id))}
    )
    state = state.model_copy(
        update={
            "sessions": SessionsState(sessions=(waiting,)),
            "records": state.records.model_copy(
                update={"records": (*state.records.records, blocked, rest)}
            ),
        }
    )

    after = step(state, ClockAdvanced(now_at=state.run.now_at + 1.0)).state

    (woken,) = after.sessions.sessions
    if other == RequestPhase.DISPATCHED:
        assert woken.wait == WaitOn(requests=(rest.request_id,))
        assert not [w for w in orphan_waits(after) if w.waiter == WaitKind.SESSION]
    else:
        assert woken.wait == waiting.wait
        assert [w.waiter for w in orphan_waits(after)] == [WaitKind.SESSION]


@pytest.mark.parametrize("dispatched_in", [None, 1])
def test_a_closing_session_whose_close_ended_unanswered_closes_once_more(
    dispatched_in: int | None,
) -> None:
    """A close that blocked (sent or not) is sent once more under a new identity.

    The skeleton timing finding: closes issued near the run deadline waited for a slot until
    the deadline blocked them, and the closing sessions waited on them for good. The second
    close is the last: when it blocks too, the session is an orphan and the run halts loudly.
    """
    ensure = pending_record(phase=RequestPhase.COMPLETED)
    opened = ensure.request
    assert isinstance(opened, EnsureSession)
    resource = ResourceId(root="lease")

    def close(identity: str) -> RequestRecord:
        request = CloseSession(
            request_id=RequestId(root=identity),
            scope=opened.scope,
            session_id=opened.spec.session_id,
            resource_id=resource,
        )
        return ensure.model_copy(
            update={
                "request_id": request.request_id,
                "request": request,
                "payload_digest": fixture_digest(request),
                "phase": RequestPhase.BLOCKED,
                "dispatched_in": dispatched_in,
            }
        )

    blocked = close("close")
    session = SessionView(
        spec=opened.spec,
        scope=opened.scope,
        generation=0,
        phase=SessionPhase.CLOSING,
        resource_id=resource,
        pending_records=(ensure.request_id, blocked.request_id),
        wait=WaitOn(requests=(blocked.request_id,)),
    )
    state = initial_state()
    state = state.model_copy(
        update={
            "sessions": SessionsState(sessions=(session,)),
            "records": state.records.model_copy(update={"records": (ensure, blocked)}),
        }
    )

    first = step(state, ClockAdvanced(now_at=state.run.now_at + 1.0))

    (again,) = first.requests
    assert isinstance(again, CloseSession)
    assert again.request_id not in (blocked.request_id, ensure.request_id)
    assert again.resource_id == resource
    (closing,) = first.state.sessions.sessions
    assert closing.phase == SessionPhase.CLOSING
    assert again.request_id is not None
    assert closing.wait == WaitOn(requests=(again.request_id,))
    assert not orphan_waits(first.state)

    records = first.state.records
    ended = records.model_copy(
        update={
            "records": tuple(
                row.model_copy(update={"phase": RequestPhase.BLOCKED})
                if row.request_id == again.request_id
                else row
                for row in records.records
            )
        }
    )
    second = step(
        first.state.model_copy(update={"records": ended}),
        ClockAdvanced(now_at=state.run.now_at + 2.0),
    )

    assert not [r for r in second.requests if isinstance(r, CloseSession)]
    assert [w.waiter for w in orphan_waits(second.state)] == [WaitKind.SESSION]

    # The same when the loop ends the second close at its limit: the ended close stays among the
    # session's records, so hours of lapses send no third close.
    later, sent = first.state, []
    for hours in range(1, 6):
        moment = state.run.now_at + 3600.0 * hours
        stepped = step(reach(later, moment).state, ClockAdvanced(now_at=moment))
        later = stepped.state
        sent += [r for r in stepped.requests if isinstance(r, CloseSession)]
    assert sent == []
    phases = {row.request_id: row.phase for row in later.records.records}
    assert phases[again.request_id] not in OPEN
    (stuck,) = later.sessions.sessions
    assert again.request_id in stuck.pending_records
    assert [w.waiter for w in orphan_waits(later)] == [WaitKind.SESSION]


def test_which_kinds_may_name_which_is_acyclic() -> None:
    """I1 of #2428: following the entities waiters may name always ends at requests and timers."""

    def reachable(kind: WaitKind) -> set[WaitKind]:
        found: set[WaitKind] = set()
        frontier = set(MAY_WAIT_ON[kind])
        while frontier:
            found |= frontier
            frontier = {named for each in frontier for named in MAY_WAIT_ON[each]} - found
        return found

    assert all(kind not in reachable(kind) for kind in WaitKind)
    assert MAY_WAIT_ON[WaitKind.TURN_ACQUISITION] == {WaitKind.SESSION}


def test_a_job_that_names_a_kind_it_may_not_fails_loudly() -> None:
    """An owned job waits on its polls and timer only; naming a waiting entity is refused (I1)."""
    state, _ = submitted()
    (job,) = state.evaluation.jobs
    session = WaitSubject(kind=WaitKind.SESSION, identity="any")
    wait = WaitOn(timer=state.run.now_at + 1.0, subjects=(session,))
    reducers = CoreReducers(evaluation=_rewaiting(wait, terminal=False))
    event = JobTerminationRequested(resource_id=job.resource_id, cause="deadline")

    with pytest.raises(ContractError, match="which it may not name"):
        step(state, event, reducers=reducers)


def test_a_turn_that_names_a_session_that_is_not_waiting_fails_loudly() -> None:
    """I2 of #2428 for entities: a named entity exists and waits when the wait is recorded."""
    lease = _session_record(_acquiring_without_a_request(), "lease", RequestPhase.DISPATCHED)
    state = _acquiring_with((lease,))
    ghost = WaitSubject(kind=WaitKind.SESSION, identity="ghost")

    def renaming(
        sessions: SessionsState, _context: SessionsContext, _event: SessionsEvent
    ) -> AreaChange[SessionsState]:
        (turn,) = sessions.invocations
        assert turn.wait is not None
        named = turn.wait.model_copy(update={"subjects": (*turn.wait.subjects, ghost)})
        renamed = turn.model_copy(update={"wait": named})
        return AreaChange(state=sessions.model_copy(update={"invocations": (renamed,)}))

    (turn,) = state.sessions.invocations
    item = SessionInput(
        input_id=InputId(root="input"),
        target=ScopeInputTarget(scope=turn.scope),
        artifact=ArtifactRef(artifact_id=ArtifactId(root="artifact"), digest="digest"),
        received_at=0.0,
        sequence=0,
    )

    with pytest.raises(ContractError, match="which it may not name or which is not waiting"):
        step(
            state,
            SessionInputReceived(input=item),
            reducers=CoreReducers(session_inputs=renaming),
        )
