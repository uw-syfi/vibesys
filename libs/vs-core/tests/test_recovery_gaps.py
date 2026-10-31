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
    Run,
    RunRest,
    Settle,
    Wait,
    at_rest,
    decide,
    ended_message,
    finished_message,
    fold,
    input_message,
    open_step,
)
from vs_proto.api import Oneof

from .test_recovery_progress import Loop, Move, play, seeded, settle


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
    assert loop.state.records.record(tag).phase is core.RequestPhase.COMPLETED  # ty: ignore[unresolved-attribute]
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
    (cancel,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Cancel)]
    assert cancel.reason is CancelReason.DRAIN_EXPIRED
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
    with pytest.raises(ValueError, match="step open"):
        loop.add(Oneof("run_ended", ended_message(End(result=result))))
    quiet = stopped()
    other = core.RunResultProposal(outcome="success", reason="not the result")
    with pytest.raises(ValueError, match="result the run does not hold"):
        quiet.add(Oneof("run_ended", ended_message(End(result=other))))


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
