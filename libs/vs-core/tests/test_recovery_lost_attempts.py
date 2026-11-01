"""An attempt a dead host ended is charged to its step only once ``clear`` finds its work gone.

A new epoch proves the process that ran an attempt dead, not the work: a job on a cluster, or
a turn the provider kept, may be alive. The fold holds such an attempt as unsettled, and the loop
asks ``clear`` before the next attempt. Only a ``Cleared`` is proof of loss, and the ``LOST``
record that carries it is what counts against the step's retry bound.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Failed,
    Interruption,
    RecordRefusedError,
    RefusalReason,
    Run,
    Settle,
    decide,
    interrupted_message,
    open_step,
)
from vs_proto.api import InterruptionReason, Oneof, Record
from vs_sim.api.testing import examples

from .recovery_loop import Loop, Move, invariants, play
from .recovery_worlds import seeded

if TYPE_CHECKING:
    from .recovery_loop import Body

EXHAUSTED = Failed(reason=core.FailureReason.ATTEMPTS_EXHAUSTED)


def started(retries: int) -> Loop:
    """A run in its first epoch with its one step running its first attempt."""
    state, registry = seeded((100.0,), retries)
    loop = Loop(state, registry)
    loop.open_epoch()
    loop.tick()
    return loop


def tag_of(loop: Loop) -> core.RequestId:
    (tag,) = [row.request_id for row in loop.state.records.records]
    return tag


def attempts_of(loop: Loop) -> int:
    row = loop.state.steps.of(tag_of(loop))
    return 0 if row is None else row.started


def lost(loop: Loop) -> Oneof:
    row = loop.state.steps.of(tag_of(loop))
    assert row is not None
    return Oneof(
        "attempt_interrupted", interrupted_message(row.tag, row.started, Interruption.LOST)
    )


@settings(max_examples=examples(pr=12, nightly=100), deadline=None)
@given(retries=st.integers(0, 3))
def test_a_step_whose_work_every_crash_loses_runs_its_bound_and_then_fails(retries: int) -> None:
    loop = started(retries)
    for _ in range(retries + 1):
        play(loop, ("crash", None))
        loop.tick()
        invariants(loop)
    assert attempts_of(loop) == retries + 1
    assert [action for action in decide(loop.state, loop.clock) if isinstance(action, Run)] == []
    (settle,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Settle)]
    assert settle.result == EXHAUSTED


@settings(max_examples=examples(pr=12, nightly=100), deadline=None)
@given(retries=st.integers(0, 3), crashes=st.integers(1, 8))
def test_a_step_whose_work_survives_every_crash_is_never_worn_out(
    retries: int, crashes: int
) -> None:
    loop = started(retries)
    for attempt in range(1, crashes + 1):
        assert attempts_of(loop) == attempt
        play(loop, ("crash_leaving_work", None))
        (run,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Run)]
        assert run.clear_first
        loop.tick()
        invariants(loop)
    assert attempts_of(loop) == crashes + 1
    assert not [a for a in decide(loop.state, loop.clock) if isinstance(a, Settle)]


def test_a_crash_alone_does_not_use_the_last_attempt_of_a_step() -> None:
    loop = started(retries=0)
    play(loop, ("crash", None))
    (run,) = decide(loop.state, loop.clock)
    assert isinstance(run, Run)
    assert run.attempt == 2
    assert run.clear_first


@settings(max_examples=examples(pr=50, nightly=300), deadline=None)
@given(
    retries=st.integers(0, 3),
    moves=st.lists(
        st.sampled_from(
            [
                ("tick", None),
                ("finish", 0),
                ("raise", 0),
                ("crash", None),
                ("crash_leaving_work", None),
                ("pause", None),
                ("relaunch_resume", None),
            ]
        ),
        max_size=14,
    ),
)
def test_an_attempt_is_charged_exactly_when_it_ended_with_a_raise_or_a_lost_record(
    retries: int, moves: list[Move]
) -> None:
    """The step's charge is the log's own: every ``RAISED`` and every ``LOST`` record, and no more."""
    loop = started(retries)
    for move in moves:
        play(loop, move)
        invariants(loop)
    row = loop.state.steps.of(tag_of(loop))
    if row is None:
        return  # the step has its result
    running = int(not row.ended)
    assert row.charged == sum(map(charging, loop.records)) + running


def charging(record: Record) -> bool:
    """Whether the record ends an attempt and charges it: a raise, or a loss ``clear`` proved."""
    match record.body:
        case Oneof(field="attempt_interrupted", value=message):
            return message.reason in (InterruptionReason.RAISED, InterruptionReason.LOST)
        case _:
            return False


@pytest.mark.parametrize("why", ["running", "settled", "wrong attempt"])
def test_the_fold_refuses_a_lost_record_for_an_attempt_no_dead_host_left_open(why: str) -> None:
    loop = started(retries=3)
    if why != "running":
        play(loop, ("crash", None))
        loop.add(lost(loop))
    if why == "wrong attempt":
        play(loop, ("crash", None))
        row = loop.state.steps.of(tag_of(loop))
        assert row is not None
        body: Body = Oneof(
            "attempt_interrupted",
            interrupted_message(row.tag, row.started + 1, Interruption.LOST),
        )
    else:
        body = lost(loop)
    before = loop.state
    with pytest.raises(RecordRefusedError) as refused:
        loop.add(body)
    assert refused.value.refusal.reason is RefusalReason.ATTEMPT_MISMATCH
    assert loop.state == before


def test_a_new_attempt_settles_the_one_a_dead_host_ended_without_charging_it() -> None:
    loop = started(retries=0)
    tag = tag_of(loop)
    play(loop, ("crash_leaving_work", None))
    open_ = open_step(loop.state, tag)
    assert open_ is not None
    assert open_.unsettled
    loop.tick()
    row = loop.state.steps.of(tag)
    assert row is not None
    assert not row.unsettled
    assert row.started == 2
    assert row.charged == 1
