"""A closing run never waits on a blocked record that nothing can end.

A request whose executor answered without a conclusion is blocked at its reconciliation
bound. The strategy hears ``RequestBlocked`` and may act on it, but once the run closes the
close itself waits on the record. Core ends such a record with a typed failure unless a live
cancellation or inspection still addresses it (``BlockedRecordStalled``), so a close always
reaches a terminal status: for any record kind, answer, host restart and lateness.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_measurements import submitted
from .test_silent_executor_deadline import BOUND, _silent

if TYPE_CHECKING:
    from collections.abc import Iterable

_STOP = core.RunControlEvent(
    control=core.ControlInput(
        control_id=core.ControlId(root="stop"), action="stop", mode=core.StopMode.DRAIN
    ),
    result=core.RunResultProposal(outcome="cancelled", reason="operator stop"),
    now_at=0.5,
)

_SILENT_ANSWERS = [
    (core.RequestPhase.RECONCILING, core.ObservationStatus.UNKNOWN),
    (core.RequestPhase.DISPATCHED, core.ObservationStatus.FAILED),
]

# The lifecycles whose closure hold ends with their record: nothing is owned beyond it.
_ENDS_WITH_RECORD = (core.LifecycleClass.IDEMPOTENT_WRITE, core.LifecycleClass.QUERY)


def _record(
    lifecycle: core.LifecycleClass,
    answer: tuple[core.RequestPhase, core.ObservationStatus],
    *,
    accepted: bool,
    named: bool,
    answered: bool = True,
) -> core.CoreState:
    """A run with one open record whose executor answered without a conclusion and went silent.

    Unless ``answered`` is false: then the executor never answered at all (a slow submission).
    """
    phase, status = answer
    state = _silent(phase, status, terminal=False)
    [record] = state.records.records
    assert record.observation is not None
    observation = record.observation.model_copy(
        update={
            "accepted": accepted,
            "resource_id": core.ResourceId(root="held") if named else None,
        }
    )
    held = record.model_copy(
        update={
            "lifecycle": lifecycle,
            "observation": observation if answered else None,
            "phase": phase if answered else core.RequestPhase.DISPATCHED,
        }
    )
    return state.model_copy(
        update={"records": state.records.model_copy(update={"records": (held,)})}
    )


def _original(state: core.CoreState) -> core.RequestRecord:
    """The silent record, not the block or cancellation requests core sent about it."""
    return next(row for row in state.records.records if row.request_id.root == "setup")


def _answer(request: core.Request, now_at: float) -> core.Observation:
    """What a well-behaved executor says to the requests core itself sends while closing."""
    cancelled = isinstance(request, core.CancelOwnedResource)
    return core.Observation(
        event_id=core.EventId(
            root=f"answer:{request.request_id.root if request.request_id else ''}"
        ),
        request_id=request.request_id or core.RequestId(root="unnamed"),
        scope=request.scope,
        sequence=0,
        observed_at=now_at,
        status=core.ObservationStatus.CANCELLED if cancelled else core.ObservationStatus.SUCCEEDED,
        resource_id=request.resource_id if cancelled else None,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
        admission_id=request.admission_id,
    )


def _settle(
    state: core.CoreState, requests: Iterable[core.Request], now_at: float
) -> core.CoreState:
    """Authorize and answer every request core sends, until it sends no more."""
    pending = list(requests)
    for _ in range(20):
        if not pending:
            break
        request = pending.pop(0)
        if request.request_id is None:
            continue
        state = core.step(state, core.DispatchAuthorized(request_id=request.request_id)).state
        result = core.step(state, core.RequestObserved(observation=_answer(request, now_at)))
        state = result.state
        pending.extend(result.requests)
    return state


def _close_after(
    state: core.CoreState, *, restart: bool, late_by: float, stop_first: bool
) -> core.CoreState:
    """Close the run around a record that goes blocked, as the shell would drive it."""
    late = BOUND + late_by
    if stop_first:
        state = core.step(state, _STOP).state
    result = (
        core.step(state, core.RecoveryStarted(epoch=2, now_at=late))
        if restart
        else core.step(state, core.ClockAdvanced(now_at=late))
    )
    state = _settle(result.state, result.requests, late)
    record = _original(state)
    if restart:
        # The restart moved the record's limit past the downtime: the deadline comes then.
        late = max(late, record.until.at)
        result = core.step(
            state, core.ReconciliationDeadline(request_id=record.request_id, now_at=late)
        )
        state = _settle(result.state, result.requests, late)
    if not stop_first:
        result = core.step(state, _STOP)
        state = _settle(result.state, result.requests, late)
    for tick in (late + 1.0, late + 100.0):
        result = core.step(state, core.ClockAdvanced(now_at=tick))
        state = _settle(result.state, result.requests, tick)
    return state


def test_a_closing_run_ends_the_blocked_record_it_waits_on() -> None:
    """The reported stall: the record is accepted but names nothing, so no cancellation exists."""
    state = _record(
        core.LifecycleClass.IDEMPOTENT_WRITE, _SILENT_ANSWERS[0], accepted=True, named=False
    )
    closed = _close_after(state, restart=False, late_by=1.0, stop_first=True)
    assert closed.run.status == core.RunStatus.TERMINAL
    record = _original(closed)
    assert record.phase == core.RequestPhase.COMPLETED
    assert record.observation is not None
    assert record.observation.status == core.ObservationStatus.FAILED
    assert record.observation.terminal
    assert "closed while this request was blocked" in record.observation.diagnostic


class _Case(NamedTuple):
    """One generated world: a record kind and answer, a host restart, and how late it is."""

    lifecycle: core.LifecycleClass
    answer: tuple[core.RequestPhase, core.ObservationStatus]
    accepted: bool
    named: bool
    restart: bool
    stop_first: bool
    late_by: float


_CASES = st.builds(
    _Case,
    lifecycle=st.sampled_from(list(core.LifecycleClass)),
    answer=st.sampled_from(_SILENT_ANSWERS),
    accepted=st.booleans(),
    named=st.booleans(),
    restart=st.booleans(),
    stop_first=st.booleans(),
    late_by=st.floats(min_value=0.0, max_value=500.0),
)


def _assert_records_name_what_addresses_them(state: core.CoreState) -> None:
    """Each record names, in registration order, exactly the commands core sent about it."""
    commands = (core.InspectRequest, core.CancelOwnedResource, core.BlockRequest)
    for record in state.records.records:
        expected = tuple(
            row.request_id
            for row in state.records.records
            if isinstance(row.request, commands) and row.request.target == record.request_id
        )
        assert record.addressed_by == expected


@given(case=_CASES)
def test_a_closing_run_never_waits_on_a_blocked_record_nothing_can_end(case: _Case) -> None:
    """Every open record is a counted wait with a producer, and a closing run terminates."""
    lifecycle, accepted, named = case.lifecycle, case.accepted, case.named
    state = _record(lifecycle, case.answer, accepted=accepted, named=named)
    closed = _close_after(
        state, restart=case.restart, late_by=case.late_by, stop_first=case.stop_first
    )
    assert core.orphan_waits(closed) == ()
    _assert_records_name_what_addresses_them(closed)
    if lifecycle in _ENDS_WITH_RECORD or not (accepted or named):
        assert closed.run.status == core.RunStatus.TERMINAL
    record = _original(closed)
    assert (
        closed.run.status == core.RunStatus.TERMINAL
        or record.phase != core.RequestPhase.BLOCKED
        or any(wait.waiter == core.WaitKind.BLOCKED_RECORD for wait in core.waits(closed))
    )


def test_a_blocked_record_outside_a_close_is_left_to_the_strategy() -> None:
    """Only a close waits on the record: a running run keeps it blocked and counts no wait."""
    state = _record(
        core.LifecycleClass.IDEMPOTENT_WRITE, _SILENT_ANSWERS[0], accepted=True, named=False
    )
    result = core.step(state, core.ClockAdvanced(now_at=BOUND + 1.0))
    blocked = _settle(result.state, result.requests, BOUND + 1.0)
    assert blocked.run.status == core.RunStatus.RUNNING
    assert _original(blocked).phase == core.RequestPhase.BLOCKED
    assert core.waits(blocked) == ()


@pytest.mark.parametrize("kind", list(core.WaitKind))
def test_every_wait_kind_has_a_resolution(kind: core.WaitKind) -> None:
    assert kind in core.RESOLUTIONS


def _poll_blocked_by_late_restart(down_for: float) -> core.CoreState:
    """A paced job whose poll was in flight when the host died and came back ``down_for`` late."""
    state, _ = submitted()
    [job] = state.evaluation.jobs
    assert job.wait is not None
    assert job.wait.timer is not None
    polled = core.step(state, core.ClockAdvanced(now_at=job.wait.timer))
    state = polled.state
    for request in polled.requests:
        assert request.request_id is not None
        state = core.step(state, core.DispatchAuthorized(request_id=request.request_id)).state
    poll = next(row for row in state.records.records if row.request.kind == "observe_owned_job")
    restart = poll.until.at + down_for
    state = core.step(state, core.RecoveryStarted(epoch=2, now_at=restart)).state
    # The restart inspects the poll first; its passed limit ends it once that inspection's own
    # bound passes with no answer.
    late = restart + state.run.limits.reconciliation_bound
    result = core.step(state, core.ReconciliationDeadline(request_id=poll.request_id, now_at=late))
    return _settle_blocks(result.state, result.requests, late)


def _settle_blocks(
    state: core.CoreState, requests: Iterable[core.Request], now_at: float
) -> core.CoreState:
    for request in requests:
        if isinstance(request, core.BlockRequest) and request.request_id is not None:
            state = core.step(state, core.DispatchAuthorized(request_id=request.request_id)).state
            answer = _answer(request, now_at).model_copy(
                update={"admission_id": request.admission_id}
            )
            state = core.step(state, core.RequestObserved(observation=answer)).state
    return state


@settings(max_examples=examples(pr=4, nightly=100))
@given(down_for=st.floats(min_value=0.0, max_value=10_000.0))
def test_a_job_whose_poll_blocked_while_the_host_was_down_is_polled_again(down_for: float) -> None:
    """Downtime past any reconciliation bound leaves the job a producer, never an orphan.

    The blocked poll wakes the job, which waits on its backoff timer and polls when it comes.
    """
    state = _poll_blocked_by_late_restart(down_for)
    assert any(row.phase == core.RequestPhase.BLOCKED for row in state.records.records)
    assert [
        wait for wait in core.orphan_waits(state) if wait.waiter == core.WaitKind.OWNED_JOB
    ] == []
    [job] = state.evaluation.jobs
    assert job.wait is not None
    assert job.wait.requests == ()
    assert job.wait.timer is not None
    result = core.step(state, core.ClockAdvanced(now_at=max(job.wait.timer, state.run.now_at)))
    assert "observe_owned_job" in [request.kind for request in result.requests]


@settings(max_examples=examples(pr=4, nightly=100))
@given(case=_CASES)
def test_a_closed_blocked_request_survives_writer_recovery_and_operator_resume(
    case: _Case,
) -> None:
    """A new epoch opens admission; the old record's forced outcome remains history."""
    state = _record(case.lifecycle, case.answer, accepted=case.accepted, named=case.named)
    closed = _close_after(
        state, restart=case.restart, late_by=case.late_by, stop_first=case.stop_first
    )
    if closed.run.status != core.RunStatus.TERMINAL:
        return
    recovered = core.step(closed, core.RecoveryStarted(epoch=3, now_at=BOUND + case.late_by + 200))
    assert recovered.requests == ()
    assert recovered.state.run.status == core.RunStatus.TERMINAL
    restarted = core.step(
        recovered.state, core.EpochResumed(epoch=4, now_at=BOUND + case.late_by + 201)
    )
    assert all(isinstance(request, core.InspectRequest) for request in restarted.requests)
    assert restarted.state.run.status == core.RunStatus.RUNNING
    assert restarted.state.run.park is None
    assert not restarted.state.scheduling.admission_closed
    assert _original(restarted.state).request == _original(closed).request
    assert _original(restarted.state).failure == _original(closed).failure


@settings(max_examples=examples(pr=4, nightly=100))
@given(
    down_for=st.floats(min_value=0.0, max_value=10_000.0),
    down_again_for=st.floats(min_value=0.0, max_value=10_000.0),
)
def test_a_host_that_restarts_while_a_blocked_job_poll_is_inspected_still_has_a_producer(
    down_for: float, down_again_for: float
) -> None:
    """The next host inspects the blocked poll before polling again: the job is waited on."""
    state = _poll_blocked_by_late_restart(down_for)
    restart = core.RecoveryStarted(epoch=3, now_at=state.run.now_at + down_again_for)
    restarted = core.step(state, restart).state
    assert [
        wait for wait in core.orphan_waits(restarted) if wait.waiter == core.WaitKind.OWNED_JOB
    ] == []


_DID_NOT_SUCCEED = {
    core.ObservationStatus.FAILED,
    core.ObservationStatus.REJECTED,
    core.ObservationStatus.CANCELLED,
}


class _Later(NamedTuple):
    """What a later host's executor says about a record the close already ended."""

    status: core.ObservationStatus
    accepted: bool
    released: bool
    children_complete: bool
    terminal: bool


_LATER_ANSWERS = st.builds(
    _Later,
    status=st.sampled_from(list(core.ObservationStatus)),
    accepted=st.booleans(),
    released=st.booleans(),
    children_complete=st.booleans(),
    terminal=st.booleans(),
)


@settings(max_examples=examples(pr=20, nightly=300))
@given(later=_LATER_ANSWERS)
def test_a_later_answer_for_a_record_the_close_ended_proves_its_release_or_is_typed(
    later: _Later,
) -> None:
    """A later host inspects a record core failed while closing; the executor answers.

    An answer that does not contradict the failure (it did not succeed, or the executor cannot
    tell, with the same acceptance) adds the release facts it proves and leaves the failure as
    it was. An executor that cannot tell and holds no record of the request (unknown,
    unaccepted) proves nothing, so the record stays as it was. Any other answer raises the
    typed contradiction, never a bare contract error.
    """
    state = _record(
        core.LifecycleClass.IDEMPOTENT_WRITE, _SILENT_ANSWERS[0], accepted=True, named=False
    )
    run = _close_after(state, restart=False, late_by=1.0, stop_first=True)
    closed = _original(run)
    ended = closed.observation
    assert closed.phase == core.RequestPhase.COMPLETED
    assert ended is not None
    assert not ended.released
    assert not ended.children_complete
    answer = ended.model_copy(
        update={
            "event_id": core.EventId(root="later-host:inspection"),
            "sequence": ended.sequence + 1,
            "diagnostic": "",
            **later._asdict(),
        }
    )
    unknown = later.status == core.ObservationStatus.UNKNOWN
    if unknown and not later.accepted:
        assert _original(core.step(run, core.RequestObserved(observation=answer)).state) == closed
        return
    agrees = later.accepted and (unknown or later.status in _DID_NOT_SUCCEED)
    if not agrees:
        with pytest.raises(core.ClosedRecordContradictionError):
            core.step(run, core.RequestObserved(observation=answer))
        return
    record = _original(core.step(run, core.RequestObserved(observation=answer)).state)
    assert record.phase == core.RequestPhase.COMPLETED
    assert record.observation is not None
    assert record.observation.status == ended.status
    assert record.observation.diagnostic == ended.diagnostic
    assert record.observation.released == later.released
    assert record.observation.children_complete == later.children_complete


def _judged(run: core.CoreState, answer: core.Observation) -> tuple[object, ...]:
    """What core makes of ``answer``: the typed contradiction, a refusal, or the record it leaves.

    The record is read without the answer's own name and number, which differ by construction.
    """
    try:
        after = core.step(run, core.RequestObserved(observation=answer)).state
    except core.ClosedRecordContradictionError:
        return ("contradiction",)
    except core.ContractError as error:
        return ("refused", error.path, error.detail)
    record = _original(after)
    seen = record.observation
    assert seen is not None
    return (
        record.phase,
        seen.status,
        seen.accepted,
        seen.resource_id,
        seen.released,
        seen.children_complete,
        seen.diagnostic,
    )


@pytest.mark.regression
@settings(max_examples=examples(pr=20, nightly=300))
@example(
    later=_Later(
        status=core.ObservationStatus.FAILED,
        accepted=True,
        released=True,
        children_complete=True,
        terminal=True,
    ),
    answered=True,
)
@example(
    later=_Later(
        status=core.ObservationStatus.PENDING,
        accepted=True,
        released=False,
        children_complete=False,
        terminal=False,
    ),
    answered=False,
)
@given(later=_LATER_ANSWERS, answered=st.booleans())
def test_the_executors_next_answer_to_a_record_the_close_ended_is_judged_like_a_later_one(
    later: _Later, *, answered: bool
) -> None:
    """Core's end of a blocked record takes no number from the executor that may still answer.

    The end is core's own conclusion, numbered right after the executor's last answer (0 when
    it never answered). The executor's own next answer carries that same number: a submission
    whose job id came back only after its bound passed and the run closed (a slow ``sbatch``).
    Core judges that answer on what it says, exactly as one numbered a step later: it refines
    the record or is the typed contradiction, never a sequence conflict that halts the host.
    """
    state = _record(
        core.LifecycleClass.IDEMPOTENT_WRITE,
        _SILENT_ANSWERS[0],
        accepted=answered,
        named=False,
        answered=answered,
    )
    run = _close_after(state, restart=False, late_by=1.0, stop_first=True)
    ended = _original(run).observation
    assert ended is not None
    answers = [
        ended.model_copy(
            update={
                "event_id": core.EventId(root=f"executor:{sequence}"),
                "sequence": sequence,
                "diagnostic": "",
                **later._asdict(),
            }
        )
        for sequence in (ended.sequence, ended.sequence + 1)
    ]
    at_the_end, after_it = (_judged(run, answer) for answer in answers)
    assert at_the_end == after_it
