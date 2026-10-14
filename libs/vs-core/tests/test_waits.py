"""Every wait names what ends it: ``orphan_waits`` reports the ones nothing will end."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import (
    RESOLUTIONS,
    Access,
    AreaChange,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptsState,
    AttemptView,
    ChildLease,
    ClockAdvanced,
    ContinuationPhase,
    ContractError,
    CoreReducers,
    DispatchTurn,
    EnsureSession,
    EvaluationContext,
    EvaluationEvent,
    EvaluationState,
    EventId,
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
    SessionPhase,
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
    phase_waits,
    producers,
    step,
    subjects,
    waits,
)
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
    return state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(
                update={"jobs": (), "registered_jobs": (registered,)}
            )
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


def _acquiring_with(records: tuple[RequestRecord, ...]) -> CoreState:
    state = _acquiring_without_a_request()
    return state.model_copy(
        update={"records": state.records.model_copy(update={"records": records})}
    )


def test_a_turn_whose_session_lease_is_being_inspected_is_carried_and_not_ended() -> None:
    """The step ends an acquiring turn only when its wait is an orphan, never before.

    The turn's lease request completed and an inspection of it is still in flight: its answer
    is what moves the session, so the wait has a producer. The step's resolver once read a
    narrower rule than the wait (requests about the session only, not their inspections) and
    cancelled the turn while that inspection was out.
    """
    lease = _session_record(_acquiring_without_a_request(), "lease", RequestPhase.COMPLETED)
    inspecting = _acquiring_with((lease, _inspection("inspect", lease, RequestPhase.DISPATCHED)))

    assert orphan_waits(inspecting) == ()
    [carried] = step(inspecting, ClockAdvanced(now_at=2.0)).state.sessions.invocations
    assert carried.phase == SessionPhase.ACQUIRING

    answered = _acquiring_with((lease, _inspection("inspect", lease, RequestPhase.COMPLETED)))
    assert [w.waiter for w in orphan_waits(answered)] == [WaitKind.TURN_ACQUISITION]
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
def test_every_wait_reports_the_one_derivation_and_the_step_ends_exactly_the_orphans(
    records: tuple[RequestRecord, ...], answered: set[int]
) -> None:
    """``waits`` and the step's resolvers read one derivation, ``producers``.

    Each wait has a producer exactly when ``producers`` names one, with or without committed
    answers in the outbox, and the step ends the acquiring turn exactly when its wait is an
    orphan: never one a live request about its session, or an inspection of one, can carry.
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
        for wait in found:
            named = producers(state, WaitSubject(kind=wait.waiter, identity=wait.subject), pending)
            assert (wait.producer is None) == (not named)
            assert wait.producer is None or wait.producer in named

    orphaned = WaitKind.TURN_ACQUISITION in {w.waiter for w in orphan_waits(state)}
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
