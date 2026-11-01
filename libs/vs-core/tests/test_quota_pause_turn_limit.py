"""A turn waiting out a provider quota stop does not spend its limit.

A turn that stopped on a quota waits on the shell's side of its dispatch while the run is
held by a quota pause, until the policy's timed resume. Core's limit on the dispatch counts
the run clock, so a wait longer than what was left of the limit ended the turn as lost in the
middle of the wait, cancelled it, and left the run paused with nothing to resume it.

Core learns of the wait only as the run's quota pause. While one holds the run, a dispatched
turn's limit moves by the time that passes, exactly as host downtime moves it, and the limit
counts again from the resume.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Interruption,
    Run,
    Settle,
    StepAttempts,
    StepsState,
    decide,
    fold,
    interrupted_message,
    started_message,
)
from vs_proto.api import EpochStarted, Oneof, Record, Timestamp

from .test_turn_limit_from_dispatch import _RUN_DEADLINE, _prepared, _reserved

_LIMIT = st.integers(min_value=10, max_value=1000).map(float)


def _dispatched(limit: float) -> tuple[core.CoreState, core.RequestId]:
    """A turn dispatched at time zero with ``limit``: core ends it at ``limit``, not before."""
    state = _reserved(limit)
    record = _prepared(state, core.DispatchTurn)
    started = record.model_copy(
        update={"phase": core.RequestPhase.DISPATCHED, "until": core.Until(at=limit)}
    )
    held = state.records.model_copy(update={"records": (started,)})
    return state.model_copy(update={"records": held}), record.request_id


def _control(
    state: core.CoreState,
    name: str,
    action: Literal["pause", "resume"],
    at: float,
    owner: core.PauseOwner | None = None,
) -> core.CoreState:
    when: Literal["now"] | None = "now" if action == "resume" else None
    control = core.ControlInput(
        control_id=core.ControlId(root=name), action=action, owner=owner, when=when
    )
    return core.step(state, core.RunControlEvent(control=control, now_at=at)).state


def _record(state: core.CoreState, request_id: core.RequestId) -> core.RequestRecord:
    return next(row for row in state.records.records if row.request_id == request_id)


def _ticked(state: core.CoreState, times: list[float]) -> core.CoreState:
    for at in times:
        state = core.step(state, core.ClockAdvanced(now_at=at)).state
    return state


def _blocked(state: core.CoreState, request_id: core.RequestId) -> bool:
    return _record(state, request_id).phase == core.RequestPhase.BLOCKED


@given(
    limit=_LIMIT,
    paused_at=st.integers(min_value=1, max_value=9).map(float),
    waits=st.lists(st.integers(min_value=1, max_value=5000).map(float), min_size=1, max_size=6),
)
def test_a_quota_pause_of_any_length_leaves_a_dispatched_turn_the_time_it_had(
    limit: float, paused_at: float, waits: list[float]
) -> None:
    """However long the quota pause, the turn ends ``limit`` after dispatch plus the pause.

    The waits run past the limit by far (a reset hours away against a limit of minutes), and
    the clock is read at each step of them.
    """
    state, request_id = _dispatched(limit)
    state = _ticked(state, [paused_at / 2])
    state = _control(state, "quota", "pause", paused_at, core.PauseOwner.QUOTA)

    now = paused_at
    for wait in waits:
        now += wait
        state = _ticked(state, [now])
        assert not _blocked(state, request_id), "the turn ended inside the quota wait"
    waited = now - paused_at

    state = _control(state, "capacity", "resume", now)
    assert not _blocked(state, request_id)
    assert not _blocked(_ticked(state, [limit + waited - 1.0]), request_id)
    assert _blocked(_ticked(state, [limit + waited]), request_id)


@given(limit=_LIMIT, wait=st.integers(min_value=1, max_value=5000).map(float))
def test_an_operator_pause_does_not_move_a_turns_limit(limit: float, wait: float) -> None:
    """Only a quota stop is a wait for capacity; a turn the operator's pause lets run is timed."""
    state, request_id = _dispatched(limit)
    state = _control(state, "hold", "pause", 1.0, core.PauseOwner.OPERATOR)
    state = _ticked(state, [1.0 + wait])

    assert _blocked(state, request_id) == (1.0 + wait >= limit)


@given(limit=_LIMIT)
def test_a_quota_pause_does_not_carry_a_limit_past_the_run_deadline(limit: float) -> None:
    """A pause that never resumes still ends with the run."""
    state, request_id = _dispatched(limit)
    state = _control(state, "quota", "pause", 1.0, core.PauseOwner.QUOTA)

    state = _ticked(state, [_RUN_DEADLINE - 1.0])
    assert _record(state, request_id).until.at <= _RUN_DEADLINE


@given(
    limit=_LIMIT,
    paused_at=st.integers(min_value=1, max_value=9).map(float),
    wait=st.integers(min_value=1, max_value=5000).map(float),
)
def test_a_quota_wait_the_gate_reports_is_credited_once(
    limit: float, paused_at: float, wait: float
) -> None:
    """A turn the capacity gate parks for the pause gets the pause once, not twice.

    The gate reports the parked wait (``TurnHeld``) and credits it whole once the run resumes
    (``_turn_holds``), so the run's quota pause must not move that turn's limit as well.
    """
    state, request_id = _dispatched(limit)
    request = _record(state, request_id).request
    assert isinstance(request, core.DispatchTurn)
    invocation = request.turn.invocation_id

    def held(hold: core.CapacityHold) -> core.CoreState:
        return core.step(state, core.TurnHeld(invocation_id=invocation, hold=hold)).state

    state = _ticked(state, [paused_at / 2])
    state = held(core.CapacityHold(since=paused_at, parked=True, budget=wait))
    state = _control(state, "quota", "pause", paused_at, core.PauseOwner.QUOTA)
    resumed = paused_at + wait
    state = _ticked(state, [resumed])
    assert not _blocked(state, request_id), "the turn ended inside the quota wait"

    state = held(core.CapacityHold(credit=wait, budget=wait))
    state = _control(state, "capacity", "resume", resumed)
    assert not _blocked(_ticked(state, [limit + wait - 1.0]), request_id)
    assert _blocked(_ticked(state, [limit + wait]), request_id)


# --- the credit survives every return of the step to unstarted ------------------------------------
#
# The step's own limit is kept twice: in its record, which the credit moves, and in the attempts
# the fold keeps (``StepAttempts.deadline_at``), from which a step that returns to unstarted (an
# interrupted attempt, a raise, a crash) takes its limit back. A credit written to the record alone
# was given back at the return, and ``decide`` then settled the turn ``Cancelled(DEADLINE)`` against
# the limit it had before the pause.


class Return(StrEnum):
    """How a running attempt returns its step to unstarted, without a result."""

    PAUSE = "pause"
    SHUTDOWN = "shutdown"
    RAISED = "raised"
    CRASH = "crash"


def _running(limit: float) -> tuple[core.CoreState, core.RequestId]:
    """A turn dispatched at time zero with ``limit`` whose first attempt is running."""
    state, request_id = _dispatched(limit)
    record = _record(state, request_id).model_copy(update={"dispatched_at": 0.0})
    records = state.records.model_copy(update={"records": (record,)})
    bound = state.run.limits.request_retries.model_copy(update={"limit": 10})
    run = state.run.model_copy(
        update={"limits": state.run.limits.model_copy(update={"request_retries": bound})}
    )
    row = StepAttempts(tag=request_id, started=1, deadline_at=limit)
    steps = StepsState(epoch=1, attempts=(row,))
    return state.model_copy(update={"records": records, "run": run, "steps": steps}), request_id


def _started(
    state: core.CoreState, request_id: core.RequestId, at: float, *, epoch: int
) -> core.CoreState:
    """``state`` once the step ``request_id`` starts its next attempt at ``at``."""
    row = state.steps.of(request_id)
    assert row is not None
    record = _record(state, request_id)
    run = Run(record.request, request_id, row.started + 1, record.until.at)
    body = Oneof("step_started", started_message(run))
    return fold([Record(seq=1000, epoch=epoch, at=Timestamp(), clock=at, body=body)], state)


def _returned(
    state: core.CoreState, request_id: core.RequestId, how: Return, at: float, *, epoch: int = 1
) -> core.CoreState:
    """``state`` once the running attempt of ``request_id`` has ended as ``how`` says, at ``at``."""
    seq = 1000
    if how is Return.CRASH:
        body = Oneof("epoch_started", EpochStarted(epoch=epoch + 1))
        epoch += 1
    else:
        why = {
            Return.PAUSE: Interruption.PAUSE,
            Return.SHUTDOWN: Interruption.SHUTDOWN,
            Return.RAISED: Interruption.RAISED,
        }[how]
        row = state.steps.of(request_id)
        assert row is not None
        body = Oneof("attempt_interrupted", interrupted_message(request_id, row.started, why))
        epoch = 1
    record = Record(seq=seq, epoch=epoch, at=Timestamp(seconds=seq), clock=at, body=body)
    return fold([record], state)


@pytest.mark.regression
@example(limit=100.0, paused_at=9.0, wait=2000.0, how=Return.PAUSE)
@example(limit=100.0, paused_at=9.0, wait=2000.0, how=Return.RAISED)
@example(limit=100.0, paused_at=9.0, wait=2000.0, how=Return.CRASH)
@given(
    limit=st.integers(min_value=20, max_value=1000).map(float),
    paused_at=st.integers(min_value=1, max_value=9).map(float),
    wait=st.integers(min_value=1, max_value=5000).map(float),
    how=st.sampled_from(Return),
)
def test_a_return_to_unstarted_keeps_the_time_a_quota_pause_credited(
    limit: float, paused_at: float, wait: float, how: Return
) -> None:
    """However the attempt ended after the wait, the turn's limit is its own plus the pause.

    The step runs again (a ``Run``, never ``Settle(Cancelled(DEADLINE))``) with the credited limit,
    once the backoff of a raised attempt has passed.
    """
    state, request_id = _running(limit)
    state = _ticked(state, [paused_at / 2])
    state = _control(state, "quota", "pause", paused_at, core.PauseOwner.QUOTA)
    now = paused_at + wait
    state = _ticked(state, [now])
    state = _control(state, "capacity", "resume", now)
    credited = _record(state, request_id).until.at

    state = _returned(state, request_id, how, now)

    assert _record(state, request_id).until.at >= credited, "the return gave the credit back"
    retry_at = now + state.run.limits.request_retries.backoff
    actions = decide(state, retry_at)
    runs = [action for action in actions if isinstance(action, Run)]
    assert [a for a in actions if isinstance(a, Settle)] == [], "the turn was ended as timed out"
    assert [run.deadline_at for run in runs] == [credited]
    assert credited == limit + wait


@pytest.mark.regression
@example(limit=100.0, rounds=[(9.0, 2000.0, Return.PAUSE), (3.0, 40.0, Return.CRASH)])
@given(
    limit=st.integers(min_value=600, max_value=1000).map(float),  # the attempts never run it out
    rounds=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=50).map(float),
            st.integers(min_value=1, max_value=5000).map(float),
            st.sampled_from(Return),
        ),
        min_size=1,
        max_size=5,
    ),
)
def test_every_quota_wait_a_step_lived_through_is_in_its_limit_whatever_returned_it(
    limit: float, rounds: list[tuple[float, float, Return]]
) -> None:
    """Over any run of quota pauses and returns, the step's limit is its own plus every wait.

    Each round: the attempt runs a while, a quota pause holds the run for ``wait`` seconds, the run
    resumes, the attempt ends one of the ways that return a step to unstarted, and the step starts
    again. The record's limit and the one the step takes back (``StepAttempts.deadline_at``)
    both carry the waits, so no return gives one back.
    """
    state, request_id = _running(limit)
    now, epoch, credited = 0.0, 1, limit
    for number, (ran, wait, how) in enumerate(rounds):
        now += ran
        state = _ticked(state, [now])
        state = _control(state, f"quota-{number}", "pause", now, core.PauseOwner.QUOTA)
        now += wait
        credited += wait
        state = _ticked(state, [now])
        state = _control(state, f"capacity-{number}", "resume", now)
        assert _record(state, request_id).until.at == credited
        state = _returned(state, request_id, how, now, epoch=epoch)
        epoch += how is Return.CRASH
        row = state.steps.of(request_id)
        assert row is not None
        assert row.deadline_at == credited, f"{how} gave back a credit"
        assert _record(state, request_id).until.at == credited
        row = state.steps.of(request_id)
        assert row is not None
        now += state.run.limits.request_retries.delay(row.charged)
        state = _started(state, request_id, now, epoch=epoch)
        assert _record(state, request_id).until.at == credited
