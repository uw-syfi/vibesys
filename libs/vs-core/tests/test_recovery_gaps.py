"""The gaps the runner loop found in core, each pinned: parked epochs, draining pauses, the end of a
run, resuming, and the step a loop starts.

`test_recovery_progress.py` holds the class property (no state yields an action the log cannot
hold); these name the scenarios behind it.
"""

from __future__ import annotations

import pytest

import vs_core.api as core
from vs_core.api.recovery import (
    Cancel,
    Cancelled,
    CancelReason,
    End,
    Interruption,
    RecordRefusedError,
    RefusalReason,
    Run,
    RunRest,
    Settle,
    Tick,
    Wait,
    at_rest,
    decide,
    ended_message,
    finished_message,
    fold,
    input_message,
    interrupted_message,
    may_start,
    open_step,
    started_message,
)
from vs_proto.api import Oneof

from .recovery_loop import Loop, Move, play, settle
from .recovery_worlds import seeded


def started(limits: tuple[float, ...], retries: int = 3) -> Loop:
    """A run in its first epoch with every step started."""
    state, registry = seeded(limits, retries)
    loop = Loop(state, registry)
    loop.open_epoch()
    loop.tick()
    return loop


def pause(loop: Loop, drain: float | None = None) -> None:
    play(loop, ("pause", drain))


def resume_in_next_epoch(loop: Loop) -> None:
    play(loop, ("relaunch_resume", None))


# --- a parked run is at rest ---------------------------------------------------------------


def test_a_parked_run_settles_nothing_even_when_a_step_it_holds_is_spent() -> None:
    """The step has used its attempts, so a running run would settle it; a parked one waits."""
    loop = started((100.0,), retries=0)
    play(loop, ("crash", None))
    loop.tick()  # `clear` finds nothing of the attempt: it is charged, and the step is spent
    spent = decide(loop.state, loop.clock)
    assert [type(action) for action in spent] == [Settle]
    pause(loop)
    assert loop.state.run.status is core.RunStatus.PARKED
    assert decide(loop.state, loop.clock) == (Wait(until=None),)


def test_a_result_recorded_while_the_run_is_parked_is_kept_not_dropped() -> None:
    """A parked epoch ignores what is offered to it; what the log holds is history."""
    loop = started((100.0,))
    pause(loop)
    loop.cancel_running(Interruption.PAUSE)
    assert loop.state.run.status is core.RunStatus.PARKED
    (tag,) = [row.request_id for row in loop.state.records.records]
    result = Cancelled(reason=CancelReason.WITHDRAWN)
    loop.add(Oneof("step_finished", finished_message(tag, 1, result)))
    settled = loop.state.records.record(tag)
    assert settled is not None
    assert settled.phase is core.RequestPhase.COMPLETED
    assert open_step(loop.state, tag) is None


# --- a draining pause parks --------------------------------------------------------------


@pytest.mark.parametrize("how", ["interrupted", "raised", "crash", "finished"])
def test_a_draining_pause_parks_when_its_last_attempt_ends_however_it_ends(how: str) -> None:
    loop = started((100.0,))
    pause(loop)
    assert loop.state.run.status is core.RunStatus.DRAINING
    {
        "interrupted": lambda: loop.cancel_running(Interruption.PAUSE),
        "raised": lambda: loop.raise_running(0),
        "crash": lambda: play(loop, ("crash", None)),
        "finished": lambda: loop.finish_running(0),
    }[how]()
    assert loop.state.run.status is core.RunStatus.PARKED


def test_a_pause_that_names_a_drain_cancels_what_still_runs_when_it_passes_and_then_parks() -> None:
    loop = started((100.0,))
    pause(loop, drain=5.0)
    park = loop.state.run.park
    assert park is not None
    assert park.deadline_at == loop.clock + 5.0
    loop.clock += 4.0
    assert not [a for a in decide(loop.state, loop.clock) if isinstance(a, Cancel)]
    loop.clock += 2.0
    # Nothing writes a record when the drain gives up, so the clock is one: the run parks in the log
    # with the attempt still running, and the loop cancels it.
    assert decide(loop.state, loop.clock) == (Tick(at=loop.clock),)
    loop.tick()
    assert loop.state.run.status is core.RunStatus.PARKED
    (cancel,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Cancel)]
    assert cancel.reason in (CancelReason.DRAIN_EXPIRED, CancelReason.PAUSE_NOW)
    loop.apply(cancel)
    assert loop.state.run.status is core.RunStatus.PARKED
    resume_in_next_epoch(loop)
    (rerun,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Run)]
    assert rerun.attempt == 2


def test_a_pause_now_cancels_at_once_and_parks_when_the_attempt_is_interrupted() -> None:
    loop = started((100.0,))
    play(loop, ("pause_now", None))
    (cancel,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Cancel)]
    assert cancel.reason is CancelReason.PAUSE_NOW
    loop.apply(cancel)
    assert loop.state.run.status is core.RunStatus.PARKED


# --- the end of a run is a record --------------------------------------------------------------


def stopped(mode: int = 0) -> Loop:
    state, registry = seeded((), 0)
    loop = Loop(state, registry)
    loop.open_epoch()
    play(loop, ("stop", mode))
    return loop


def test_a_stop_with_nothing_open_reaches_terminal_but_the_run_ends_when_the_end_is_recorded() -> (
    None
):
    loop = stopped()
    assert loop.state.run.status is core.RunStatus.TERMINAL
    assert at_rest(loop.state) is None
    (end,) = decide(loop.state, loop.clock)
    assert isinstance(end, End)
    loop.apply(end)
    assert at_rest(loop.state) is RunRest.ENDED
    assert decide(loop.state, loop.clock) == (Wait(until=None),)
    play(loop, ("crash", None))
    assert at_rest(loop.state) is RunRest.ENDED


def test_the_end_is_recorded_once_the_last_step_has_its_result() -> None:
    state, registry = seeded((100.0,), 0)
    loop = Loop(state, registry)
    loop.open_epoch()
    loop.tick()
    play(loop, ("stop", 0))
    assert not [a for a in decide(loop.state, loop.clock) if isinstance(a, End)]
    settle(loop)
    assert at_rest(loop.state) is RunRest.ENDED


def test_a_log_that_ends_a_run_with_work_open_or_another_result_is_refused() -> None:
    state, registry = seeded((100.0,), 0)
    loop = Loop(state, registry)
    loop.open_epoch()
    play(loop, ("stop", 0))
    result = loop.state.run.result
    assert result is not None
    with pytest.raises(RecordRefusedError, match="not over") as early:
        loop.add(Oneof("run_ended", ended_message(End(result=result))))
    quiet = stopped()
    other = core.RunResultProposal(outcome="success", reason="not the result")
    with pytest.raises(RecordRefusedError, match="result the run does not hold") as foreign:
        quiet.add(Oneof("run_ended", ended_message(End(result=other))))
    assert early.value.refusal.reason is RefusalReason.RUN_NOT_OVER
    assert foreign.value.refusal.reason is RefusalReason.RUN_NOT_OVER


def test_a_resumed_run_is_no_longer_ended() -> None:
    loop = stopped()
    (end,) = decide(loop.state, loop.clock)
    loop.apply(end)
    resume_in_next_epoch(loop)
    assert loop.state.run.status is core.RunStatus.RUNNING
    assert at_rest(loop.state) is None


# --- resuming ----------------------------------------------------------------------------------


def test_a_parked_run_resumes_in_a_later_epoch_and_never_in_its_own() -> None:
    """A pause ends the process, so the run is parked for the rest of the epoch it parked in."""
    loop = started((100.0,))
    pause(loop)
    loop.cancel_running(Interruption.PAUSE)
    assert loop.state.run.status is core.RunStatus.PARKED
    resume = loop.control("resume", when="now")
    with pytest.raises(core.ControlRefusedError, match="terminal for this epoch"):
        loop.add(Oneof("core_input", input_message(core.RunControlEvent(control=resume, now_at=0))))
    resume_in_next_epoch(loop)
    assert loop.state.run.status is core.RunStatus.RUNNING
    assert loop.state.run.park is None
    (rerun,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Run)]
    assert rerun.attempt == 2
    assert loop.state.steps.attempts[0].charged == 0, "a pause is no debit of the retry bound"


def test_a_relaunched_parked_run_stays_parked_until_it_is_resumed() -> None:
    loop = started((100.0,))
    pause(loop)
    loop.cancel_running(Interruption.PAUSE)
    play(loop, ("crash", None))
    assert loop.state.run.status is core.RunStatus.PARKED
    assert at_rest(loop.state) is RunRest.PARKED
    assert decide(loop.state, loop.clock) == (Wait(until=None),)


def test_a_pause_now_still_cancels_after_a_crash_in_its_drain() -> None:
    loop = started((100.0, 100.0))
    play(loop, ("pause_now", None))
    play(loop, ("crash", None))
    assert loop.state.run.status in (core.RunStatus.DRAINING, core.RunStatus.PARKED)
    assert not [a for a in decide(loop.state, loop.clock) if isinstance(a, Run)]


# --- the step a loop starts ------------------------------------------------------------------------


def test_open_step_is_the_step_while_it_has_no_result() -> None:
    loop = started((100.0, 50.0))
    steps = list(loop.state.records.records)
    for row in steps:
        view = open_step(loop.state, row.request_id)
        assert view is not None
        assert view.step == row.request
        assert view.started == 1
        assert view.deadline_at == row.until.at
    loop.finish_running(0)
    done = [row.request_id for row in loop.state.records.records if row.phase.value == "completed"]
    assert [open_step(loop.state, tag) for tag in done] == [None]
    assert open_step(loop.state, core.RequestId(root="no-such-step")) is None


def test_open_step_counts_the_attempts_that_began_across_epochs() -> None:
    loop = started((100.0,))
    play(loop, ("crash", None))
    loop.tick()
    loop.sleep_until_woken()
    loop.tick()
    (row,) = loop.state.records.records
    view = open_step(loop.state, row.request_id)
    assert view is not None
    assert view.started == 2


def test_the_moves_cover_a_fold_that_equals_the_state() -> None:
    """The model keeps state only through the fold: refolding its records gives the same state."""
    loop = started((100.0,))
    moves: list[Move] = [("pause", 2.0), ("clock", 3.0), ("tick", None), ("relaunch_resume", None)]
    for move in moves:
        play(loop, move)
    refolded = fold(loop.records, seeded((100.0,), 3)[0], registry=loop.registry)
    assert refolded.steps == loop.state.steps
    assert refolded.run == loop.state.run


# --- a start the rule forbids is not recorded -------------------------------------------------


def test_a_flight_that_waited_while_the_run_parked_cannot_record_its_start() -> None:
    """A flight decided to run a step, waited for its `clear`, and the run parked meanwhile."""
    loop = started((100.0, 100.0))
    steps = [row.request_id for row in loop.state.records.records]
    stale = Run(
        step=loop.state.records.records[1].request, tag=steps[1], attempt=2, deadline_at=100.0
    )
    play(loop, ("pause_now", None))
    for tag in steps:
        started_row = loop.state.steps.of(tag)
        assert started_row is not None
        loop.add(Oneof("attempt_interrupted", interrupted_message(tag, 1, Interruption.PAUSE)))
    assert loop.state.run.status is core.RunStatus.PARKED
    assert not may_start(loop.state, steps[1], loop.clock)
    with pytest.raises(RecordRefusedError, match="may not start it") as refused:
        loop.add(Oneof("step_started", started_message(stale)))
    assert refused.value.refusal.reason is RefusalReason.START_NOT_DUE


@pytest.mark.parametrize("settled", [True, False])
def test_may_start_is_false_for_a_step_with_a_result_and_for_one_no_one_knows(
    *, settled: bool
) -> None:
    loop = started((100.0,))
    (tag,) = [row.request_id for row in loop.state.records.records]
    if settled:
        loop.finish_running(0)
        assert not may_start(loop.state, tag, loop.clock)
    else:
        assert not may_start(loop.state, core.RequestId(root="no-such-step"), loop.clock)


# --- a kill in the middle of a drain ----------------------------------------------------------


@pytest.mark.parametrize("kind", ["pause", "pause_now"])
def test_a_drain_a_kill_cut_short_parks_in_the_next_epoch_and_admits_the_resume_there(
    kind: str,
) -> None:
    """The operator paused in epoch 1 and the process died; the Resume belongs to epoch 2's process.

    `RunPark.epoch` stays the epoch the pause was made in, so the park the new epoch makes of the
    drain is older than it and the resume is not refused as "terminal for this epoch".
    """
    loop = started((100.0,))
    pause(loop) if kind == "pause" else play(loop, ("pause_now", None))
    # A drain that waits is cut short mid-way; one that cancels parks at once, with the attempt
    # still running for the loop to cancel, and the kill takes it.
    waiting = core.RunStatus.DRAINING if kind == "pause" else core.RunStatus.PARKED
    assert loop.state.run.status is waiting
    draining = loop.state.run.park
    assert draining is not None
    paused_in = draining.epoch
    play(loop, ("crash", None))
    assert loop.state.run.status is core.RunStatus.PARKED
    park = loop.state.run.park
    assert park is not None
    assert park.epoch == paused_in < loop.state.records.recovery.epoch
    resume_in_next_epoch_without_a_new_one(loop)
    assert loop.state.run.status is core.RunStatus.RUNNING
    (rerun,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Run)]
    assert rerun.attempt == 2


def resume_in_next_epoch_without_a_new_one(loop: Loop) -> None:
    """The resume control of the epoch the drain parked in."""
    resume = loop.control("resume", when="now")
    loop.add(Oneof("core_input", input_message(core.RunControlEvent(control=resume, now_at=0))))
