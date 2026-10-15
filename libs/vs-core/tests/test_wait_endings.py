"""How a wait ends, and what its search hears about it.

Each way a wait can end (a watch fires mid-wait, a limit passes for a record that is only
reattached) ends it once, with the typed failure that names the cause, and leaves nothing
for the clock to act on again. Results and failures that are core's own never reach the
search. Pure core, no real time: a test moves the clock by stepping ``ClockAdvanced``.
"""

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import value_digest
from .test_attempt_retirement import fixture, retirement
from .test_measurements import plan as measurement_plan
from .test_session_turns import invocation, scope, turn, waiting_turn_state
from .wait_support import (
    dispatch_all,
    failures,
    fresh,
    open_records,
    operation_codec,
    operations,
    submit,
    wait,
)

LIMITS = st.floats(min_value=5.0, max_value=200.0, allow_nan=False)


def _quiet(state: core.CoreState) -> None:
    """The ended wait is not raised again, however far the clock moves."""
    assert open_records(state) == []
    again = core.step(state, core.ClockAdvanced(now_at=state.run.now_at + 1_000.0))
    assert [f for f in failures(again) if f.decision is not None] == []
    assert operations(again.state) == operations(state)


def _with_active_workspace() -> tuple[core.CoreState, core.AttemptRef, core.OperationRegistry]:
    """An active attempt (a workspace that is open) and a run that can start a test operation."""
    state, ref = fixture()
    codec = operation_codec()
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=codec.descriptors)}
            ),
        }
    )
    return state, ref, codec


@given(limit=LIMITS)
def test_a_wait_watching_a_workspace_ends_when_it_closes_mid_wait(limit: float) -> None:
    state, ref, codec = _with_active_workspace()
    watch = core.WhileOpen(workspace=ref)
    state = dispatch_all(submit(state, wait(codec, "w", core.Within(limit=limit) | watch)).state)
    assert [r.phase for r in open_records(state)] == [core.RequestPhase.DISPATCHED]

    closed = core.step(state, retirement(ref))

    (failure,) = failures(closed)
    assert failure.reason is core.FailureReason.WORKSPACE_CLOSED
    assert failure.decision == core.DecisionId(root="w")
    assert failure.watch == watch
    (record,) = operations(closed.state)
    assert record.phase is core.RequestPhase.BLOCKED
    assert record.failure == failure
    assert closed.state.run.now_at < record.until.at, "it ended before its limit"
    _quiet(closed.state)


def _with_running_job(state: core.CoreState) -> tuple[core.CoreState, core.ResourceId]:
    """The run holds a measurement job that has not ended."""
    job = core.ResourceId(root="job")
    owned = core.OwnedJob(
        wait=core.WaitOn(timer=0.0),
        resource_id=job,
        submission_id=core.RequestId(root="submission"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        plan=measurement_plan(),
        status=core.ObservationStatus.PENDING,
    )
    evaluation = state.evaluation.model_copy(update={"jobs": (owned,)})
    return state.model_copy(update={"evaluation": evaluation}), job


def _job_ended(state: core.CoreState, job: core.ResourceId) -> core.CoreState:
    """The owner of the job recorded that it is terminal and released."""
    jobs = tuple(
        row.model_copy(
            update={
                "terminal": True,
                "released": True,
                "status": core.ObservationStatus.SUCCEEDED,
                "wait": None,
            }
        )
        if row.resource_id == job
        else row
        for row in state.evaluation.jobs
    )
    return state.model_copy(
        update={"evaluation": state.evaluation.model_copy(update={"jobs": jobs})}
    )


@given(limit=LIMITS)
def test_a_wait_watching_a_job_ends_with_owner_gone_when_the_job_is_gone_mid_wait(
    limit: float,
) -> None:
    state, codec = fresh()
    state, job = _with_running_job(state)
    watch = core.WhileAlive(job=job)
    state = dispatch_all(submit(state, wait(codec, "w", core.Within(limit=limit) | watch)).state)
    assert [r.phase for r in open_records(state)] == [core.RequestPhase.DISPATCHED]
    assert failures(core.step(state, core.ClockAdvanced(now_at=state.run.now_at))) == []

    gone = core.step(_job_ended(state, job), core.ClockAdvanced(now_at=state.run.now_at))

    (failure,) = failures(gone)
    assert failure.reason is core.FailureReason.OWNER_GONE
    assert failure.decision == core.DecisionId(root="w")
    assert failure.watch == watch
    (record,) = operations(gone.state)
    assert record.phase is core.RequestPhase.BLOCKED
    assert record.failure == failure
    _quiet(gone.state)


def _hearing(state: core.CoreState, *events: core.StrategyEvent) -> core.Translated:
    return core.translate(state, events)


def _late_results(record: core.RequestRecord) -> list[core.StrategyEvent]:
    """The results a request can still report after it ended, one per kind that has a decision."""
    done = core.Observation(
        event_id=core.EventId(root="late"),
        request_id=record.request_id,
        scope=record.request.scope,
        sequence=1,
        observed_at=11.0,
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )
    invocation = core.InvocationRef(
        session_id=core.SessionId(root="s"), invocation_id=core.InvocationId(root="i"), generation=0
    )
    return [
        core.MeasurementResult(
            scope=record.request.scope,
            source_request=record.request_id,
            evidence=(),
            status=core.ObservationStatus.SUCCEEDED,
        ),
        core.TurnResult(invocation=invocation, observation=done),
    ]


@given(limit=LIMITS)
def test_a_result_that_arrives_after_its_request_ended_is_discarded_not_delivered(
    limit: float,
) -> None:
    state, codec = fresh()
    state = dispatch_all(submit(state, wait(codec, "w", core.Within(limit=limit))).state)
    (live,) = operations(state)
    for result in _late_results(live):
        heard = _hearing(state, result)
        (event,) = heard.events
        assert isinstance(event, core.TurnEnded | core.MeasurementFinished), "while it lasts"
        assert event.decision == core.DecisionId(root="w")
        assert heard.discarded == ()

    ended = core.step(state, core.ClockAdvanced(now_at=limit + 1.0))
    (record,) = operations(ended.state)
    assert record.failure is not None
    for result in _late_results(record):
        heard = _hearing(ended.state, result)
        assert heard.events == (), "the search already heard Failed for this decision"
        (dropped,) = heard.discarded
        assert dropped.reason is core.DiscardReason.LATE_RESULT
        assert dropped.request == record.request_id


@given(
    reason=st.sampled_from(
        [
            core.FailureReason.LOST,
            core.FailureReason.OUT_OF_BUDGET,
            core.FailureReason.MEASUREMENT_UNTRUSTED,
        ]
    ),
    own=st.booleans(),
)
def test_a_search_hears_only_failures_of_its_own_decisions(
    reason: core.FailureReason, *, own: bool
) -> None:
    state, _ = fresh()
    failed = core.Failed(reason=reason, decision=core.DecisionId(root="mine") if own else None)
    heard = _hearing(state, failed)
    if own:
        assert heard.events == (failed,)
        assert heard.discarded == ()
    else:
        assert heard.events == ()
        assert heard.discarded == (
            core.Discarded(reason=core.DiscardReason.INTERNAL_FAILURE, failure=failed),
        )


def _past_the_run_deadline(budget: float) -> tuple[core.CoreState, core.AttemptRef]:
    """An active attempt in a run whose deadline passed long ago, with a cleanup budget."""
    state, ref = fixture()
    run = state.run.model_copy(
        update={
            "deadline_at": 5.0,
            "now_at": 50.0,
            "limits": state.run.limits.model_copy(update={"cleanup_bound": budget}),
        }
    )
    return state.model_copy(update={"run": run}), ref


BUDGETS = st.floats(min_value=1.0, max_value=300.0, allow_nan=False)


@given(budget=BUDGETS, left=st.floats(min_value=1.0, max_value=900.0, allow_nan=False))
def test_cleanup_before_the_run_deadline_gets_the_time_left_and_never_less_than_its_budget(
    budget: float, left: float
) -> None:
    state, ref = _past_the_run_deadline(budget)
    run = state.run.model_copy(update={"deadline_at": state.run.now_at + left})

    closing = core.step(state.model_copy(update={"run": run}), retirement(ref))

    issued = [r for r in closing.state.records.records if r.phase is core.RequestPhase.PREPARED]
    assert issued
    # The run deadline does not cut a cleanup issued just before it short of its budget.
    assert {r.until.at for r in issued} == {max(run.deadline_at, run.now_at + budget)}
    assert all(r.until.watches == () for r in issued)


@given(budget=BUDGETS, left=st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False))
def test_cleanup_after_a_stop_is_bounded_by_its_budget_not_the_run_left(
    budget: float, left: float
) -> None:
    state, ref = _past_the_run_deadline(budget)
    run = state.run.model_copy(update={"deadline_at": state.run.now_at + left})
    stop = core.RunControlEvent(
        control=core.ControlInput(
            control_id=core.ControlId(root="stop"), action="stop", mode=core.StopMode.CANCEL
        ),
        result=core.RunResultProposal(outcome="cancelled", reason="operator stop"),
        now_at=run.now_at,
    )
    state = core.step(state.model_copy(update={"run": run}), stop).state
    assert state.run.status is core.RunStatus.CLOSING
    state = dispatch_all(core.step(state, retirement(ref)).state)
    issued = [r for r in state.records.records if r.phase is core.RequestPhase.DISPATCHED]
    assert issued
    assert {r.until.at for r in issued} == {state.run.now_at + budget}

    # The answer is lost: once the bound passes, nothing core sent is still awaited.
    lapsed = core.step(state, core.ClockAdvanced(now_at=state.run.now_at + budget + 1.0)).state
    assert all(
        r.phase is not core.RequestPhase.DISPATCHED
        for r in lapsed.records.records
        if r.request_id in {i.request_id for i in issued}
    )


@given(budget=BUDGETS)
def test_cleanup_after_the_run_deadline_gets_its_own_bounded_time(budget: float) -> None:
    state, ref = _past_the_run_deadline(budget)

    closing = core.step(state, retirement(ref))

    issued = [r for r in closing.state.records.records if r.phase is core.RequestPhase.PREPARED]
    assert issued, "closing the workspace is not refused because the run is over"
    assert {r.until.at for r in issued} == {state.run.now_at + budget}
    assert all(r.until.watches == () for r in issued)


@given(budget=BUDGETS)
def test_cleanup_after_the_run_deadline_completes_within_its_budget(budget: float) -> None:
    state, ref = _past_the_run_deadline(budget)
    state = core.step(state, retirement(ref)).state
    (first,) = [r for r in state.records.records if r.phase is core.RequestPhase.PREPARED]
    state = core.step(state, core.DispatchAuthorized(request_id=first.request_id)).state

    inside = core.step(state, core.ClockAdvanced(now_at=state.run.now_at + budget * 0.5))
    assert failures(inside) == []
    answered = core.step(
        inside.state,
        core.RequestObserved(
            observation=core.Observation(
                event_id=core.EventId(root="closed"),
                request_id=first.request_id,
                scope=first.request.scope,
                sequence=1,
                observed_at=inside.state.run.now_at,
                status=core.ObservationStatus.SUCCEEDED,
                accepted=True,
                terminal=True,
                released=True,
                children_complete=True,
                admission_id=first.request.admission_id,
            )
        ),
    )
    (done,) = [r for r in answered.state.records.records if r.request_id == first.request_id]
    assert done.phase is core.RequestPhase.COMPLETED
    assert done.failure is None


@given(budget=BUDGETS)
def test_unanswered_cleanup_ends_at_its_budget_and_only_core_hears_of_it(budget: float) -> None:
    state, ref = _past_the_run_deadline(budget)
    state = core.step(state, retirement(ref)).state
    for record in state.records.records:
        if record.phase is core.RequestPhase.PREPARED:
            state = core.step(state, core.DispatchAuthorized(request_id=record.request_id)).state

    ended = core.step(state, core.ClockAdvanced(now_at=state.run.now_at + budget))

    own = failures(ended)
    assert own, "an unanswered cleanup request does not wait forever"
    assert {f.reason for f in own} == {core.FailureReason.TIMED_OUT}
    assert all(f.decision is None for f in own), "core's own requests name no decision"
    heard = core.translate(ended.state, ended.events)
    assert [e for e in heard.events if isinstance(e, core.Failed)] == []
    assert {d.reason for d in heard.discarded} == {core.DiscardReason.INTERNAL_FAILURE}


@given(identity=st.text("abcdefgh", min_size=1, max_size=6))
def test_a_turn_that_never_started_reaches_the_decision_that_asked_for_it(identity: str) -> None:
    """A stalled acquisition ends the turn with an observation that names no request.

    The search still asked for that turn, so it hears `TurnEnded` for its decision, found by
    the receipt of the `RequestTurn`; a turn nobody asked for is core's own and stays quiet.
    """
    spec = turn(identity)
    asked = core.RequestTurn(decision_id=core.DecisionId(root="ask"), scope=scope(), turn=spec)
    receipt = core.DecisionReceipt(
        decision_id=asked.decision_id,
        decision=asked,
        payload_digest=value_digest(asked),
        feedback=core.Accepted(decision_id=asked.decision_id),
    )
    state = waiting_turn_state(spec)
    run = state.run.model_copy(update={"receipts": (receipt,)})
    seeded = state.model_copy(update={"run": run})
    ended = core.TurnResult(
        invocation=invocation(spec),
        observation=core.Observation(
            event_id=core.EventId(root=f"acquisition-stalled:{identity}"),
            request_id=core.RequestId(root=f"acquisition-stalled:{identity}"),
            scope=scope(),
            sequence=0,
            observed_at=1.0,
            status=core.ObservationStatus.CANCELLED,
            terminal=True,
            released=True,
            children_complete=True,
        ),
    )

    heard = core.translate(seeded, (ended,))
    assert heard.events == (core.TurnEnded(decision=asked.decision_id, result=ended),)
    assert heard.discarded == ()

    unasked = core.translate(waiting_turn_state(spec), (ended,))
    assert unasked.events == ()


def _with_later_requests(state: core.CoreState, extra: int, gap: float) -> core.CoreState:
    """The decision's one record plus *extra* more of the same decision that outlast it by *gap*."""
    (record,) = operations(state)
    clones = tuple(
        record.model_copy(
            update={
                "request_id": core.RequestId(root=f"w/more/{n}"),
                "request": record.request.model_copy(
                    update={"request_id": core.RequestId(root=f"w/more/{n}")}
                ),
                "until": core.Until(at=record.until.at + gap),
            }
        )
        for n in range(extra)
    )
    records = state.records.model_copy(update={"records": (*state.records.records, *clones)})
    return state.model_copy(update={"records": records})


@given(
    limit=LIMITS,
    extra=st.integers(min_value=1, max_value=3),
    gap=st.floats(min_value=1.0, max_value=100.0, allow_nan=False),
)
def test_a_decision_hears_of_its_ending_once_however_many_requests_it_held(
    limit: float, extra: int, gap: float
) -> None:
    """A later request of a decision that already ended with a failure ends quietly."""
    state, codec = fresh()
    state = dispatch_all(submit(state, wait(codec, "w", core.Within(limit=limit))).state)
    state = _with_later_requests(state, extra, gap)
    first_limit = min(row.until.at for row in operations(state))
    last_limit = max(row.until.at for row in operations(state))

    first = core.step(state, core.ClockAdvanced(now_at=first_limit))
    last = core.step(first.state, core.ClockAdvanced(now_at=last_limit))

    (failure,) = failures(first)
    assert failure.reason is core.FailureReason.TIMED_OUT
    assert failure.decision == core.DecisionId(root="w")
    assert [f for f in failures(last) if f.decision is not None] == []
    assert all(row.failure is not None for row in operations(last.state)), "every request ended"
