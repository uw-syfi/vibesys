"""What the review of the recovery rule found, each as the scenario that showed it.

`test_recovery_progress.py` holds the class property over every kind of step, a lagging loop and
the seeds a restart finds; these are the minimal runs behind it, one per finding, so a regression
names what broke.
"""

from __future__ import annotations

import pytest

import vs_core.api as core
from vs_core.api.recovery import (
    Cancel,
    CancelReason,
    End,
    Interruption,
    RecordRefusedError,
    RefusalReason,
    Run,
    RunRest,
    Settle,
    at_rest,
    decide,
    ended_message,
    fold,
    interrupted_message,
)
from vs_proto.api import AttemptInterrupted, InterruptionReason, Oneof, Record, Timestamp

from .recovery_loop import OPEN, Loop, play, settle
from .recovery_worlds import Aux, Kind, Standing, StepSpec, World, build


def opened(world: World) -> Loop:
    """A run in its first epoch."""
    state, registry = build(world)
    loop = Loop(state, registry)
    loop.open_epoch()
    return loop


def ended_records(loop: Loop) -> int:
    return sum(1 for r in loop.records if r.body is not None and r.body.field == "run_ended")


# --- the limit of a step is its own ------------------------------------------------------------


@pytest.mark.parametrize("kind", [Kind.JOB, Kind.QUERY])
def test_a_drain_deadline_does_not_shorten_the_limit_of_the_step_it_interrupts(kind: Kind) -> None:
    """The drain caps a request it cannot interrupt; the cap ends with the attempt it capped."""
    loop = opened(World((StepSpec(100.0, kind),)))
    loop.tick()
    (record,) = loop.state.records.records
    first = record.until.at
    play(loop, ("pause", 1.0))
    play(loop, ("clock", 3.0))
    loop.tick()
    play(loop, ("relaunch_resume", None))
    (record,) = loop.state.records.records
    assert record.until.at == first
    (run,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Run)]
    assert run.deadline_at == first


# --- a parked run and the attempt it still holds ------------------------------------------------


def test_a_pause_now_parks_with_the_attempt_running_and_decide_cancels_it() -> None:
    loop = opened(World((StepSpec(100.0),)))
    loop.tick()
    play(loop, ("pause_now", None))
    assert loop.state.run.status is core.RunStatus.PARKED
    assert loop.running()
    assert decide(loop.state, loop.clock) == (
        Cancel(tag=loop.running()[0], reason=CancelReason.PAUSE_NOW),
    )


def test_the_epoch_end_of_a_parked_run_does_not_charge_the_attempt_it_held() -> None:
    """A call that ignores its cancel ends with the process; the pause did that, not the step."""
    loop = opened(World((StepSpec(100.0),), retries=0))
    loop.tick()
    play(loop, ("pause_now", None))
    assert loop.running()
    play(loop, ("relaunch_resume", None))
    (run,) = [a for a in decide(loop.state, loop.clock) if isinstance(a, Run)]
    assert run.attempt == 2


def test_the_epoch_end_of_a_draining_run_charges_the_attempt_like_a_crash_once_clear_finds_it_lost() -> (
    None
):
    loop = opened(World((StepSpec(100.0),), retries=0))
    loop.tick()
    play(loop, ("pause", None))
    assert loop.state.run.status is core.RunStatus.DRAINING
    play(loop, ("relaunch_resume", None))
    (run,) = decide(loop.state, loop.clock)
    assert isinstance(run, Run)
    assert run.clear_first
    loop.tick()
    assert [type(a) for a in decide(loop.state, loop.clock)] == [Settle]


# --- the end of a run is a record, every time --------------------------------------------------


def test_a_run_stopped_again_after_a_resume_records_its_second_end() -> None:
    loop = opened(World(()))
    play(loop, ("stop", 0))
    loop.tick()
    assert ended_records(loop) == 1
    play(loop, ("relaunch_resume", None))
    assert at_rest(loop.state) is None
    play(loop, ("stop", 0))
    assert at_rest(loop.state) is None, "the first end does not stand for the second"
    loop.tick()
    assert ended_records(loop) == 2
    assert at_rest(loop.state) is RunRest.ENDED


def test_an_end_is_recorded_only_once_the_run_is_over() -> None:
    """A stop with a step open closes the run when the last step has its result, not before."""
    loop = opened(World((StepSpec(100.0),)))
    loop.tick()
    play(loop, ("stop", 0))
    assert not [a for a in decide(loop.state, loop.clock) if isinstance(a, End)]
    settle(loop)
    assert loop.state.run.status is core.RunStatus.TERMINAL
    assert at_rest(loop.state) is RunRest.ENDED


def test_a_stop_ends_a_run_whose_seed_left_commands_open() -> None:
    """An inspection, a cancel and a block settle unknown outcomes; nothing sends them here."""
    loop = opened(
        World(
            (StepSpec(100.0),),
            aux=(Aux.INSPECT, Aux.CANCEL_RESOURCE, Aux.BLOCK),
        )
    )
    play(loop, ("stop", 0))
    settle(loop)
    assert loop.state.run.status is core.RunStatus.TERMINAL
    assert at_rest(loop.state) is RunRest.ENDED


# --- a stop is not lost in a pause --------------------------------------------------------------


@pytest.mark.parametrize("pause", ["pause", "pause_now"])
def test_a_stop_in_a_pause_ends_the_run(pause: str) -> None:
    """A draining or a parked run takes the operator's stop; it was a rejected `Finish` no one saw."""
    loop = opened(World((StepSpec(100.0),)))
    loop.tick()
    play(loop, (pause, None))
    play(loop, ("stop", 0))
    assert loop.state.run.result is not None
    settle(loop)
    assert at_rest(loop.state) is RunRest.ENDED


# --- a seed numbers its own epochs -------------------------------------------------------------


def test_a_seeds_host_epoch_ahead_of_the_log_does_not_delay_the_resume() -> None:
    loop = opened(World((StepSpec(100.0),), host_epoch=3))
    loop.tick()
    play(loop, ("pause", None))
    play(loop, ("relaunch_resume", None))
    assert loop.state.run.status is core.RunStatus.RUNNING


# --- what an earlier version left in flight ----------------------------------------------------


@pytest.mark.parametrize("standing", [Standing.DISPATCHED, Standing.RECONCILING])
def test_a_pause_parks_a_seed_whose_step_was_in_flight_with_no_attempt_in_this_log(
    standing: Standing,
) -> None:
    loop = opened(World((StepSpec(100.0, Kind.QUERY, 0, standing),)))
    play(loop, ("pause", None))
    assert loop.state.run.status is core.RunStatus.PARKED
    (record,) = loop.state.records.records
    assert record.phase is core.RequestPhase.PREPARED


# --- the rule and the park agree about what starts ---------------------------------------------


def test_a_pause_parks_when_the_rule_holds_back_the_only_step_a_drain_would_let_through() -> None:
    """A snapshot is drain work, but an unfinished writer of its scope runs first and cannot in a drain."""
    loop = opened(World((StepSpec(100.0, Kind.WRITE, 0, Standing.RECONCILING),), retains=(0,)))
    play(loop, ("pause", None))
    assert loop.state.run.status is core.RunStatus.PARKED


# --- the log is read as strictly as it is written ----------------------------------------------


def test_a_finish_of_an_attempt_that_was_replaced_is_refused() -> None:
    loop = opened(World((StepSpec(100.0),)))
    loop.tick()
    play(loop, ("raise", 0))
    loop.tick()
    assert loop.finish_late(0), "attempt 2 runs and a finish of attempt 1 arrived"


@pytest.mark.parametrize("reason", [InterruptionReason.UNSPECIFIED, InterruptionReason(99)])
def test_an_interruption_this_version_does_not_know_is_refused_as_a_log_it_cannot_read(
    reason: InterruptionReason,
) -> None:
    loop = opened(World((StepSpec(100.0),)))
    loop.tick()
    tag = loop.running()[0]
    record = Record(
        seq=len(loop.records) + 1,
        epoch=loop.epoch,
        at=Timestamp(seconds=len(loop.records) + 1),
        clock=loop.clock,
        body=Oneof(
            "attempt_interrupted", AttemptInterrupted(tag=tag.root, attempt=1, reason=reason)
        ),
    )
    with pytest.raises(RecordRefusedError, match="does not know") as refused:
        fold([record], loop.state, registry=loop.registry)
    assert refused.value.refusal.reason is RefusalReason.UNREADABLE


def test_an_end_the_run_is_not_over_for_is_refused() -> None:
    loop = opened(World((StepSpec(100.0),)))
    loop.tick()
    play(loop, ("stop", 0))
    result = loop.state.run.result
    assert result is not None
    with pytest.raises(RecordRefusedError, match="not over") as refused:
        loop.add(Oneof("run_ended", ended_message(End(result=result))))
    assert refused.value.refusal.reason is RefusalReason.RUN_NOT_OVER


# --- a step core ended itself is concluded the way any step ends --------------------------------


def test_a_job_step_a_late_clock_input_ended_is_concluded_so_a_stopped_run_closes() -> None:
    """The kernel ends a step at its limit when a clock input comes first; the job it owns is
    released by the runner's end of the attempt, like any step's."""
    loop = opened(World((StepSpec(2.0, Kind.JOB, 0, Standing.DISPATCHED),)))
    play(loop, ("tock", 3.0))
    play(loop, ("stop", 1))
    settle(loop)
    assert loop.state.run.status is core.RunStatus.TERMINAL
    assert ended_records(loop) == 1


def test_the_interruption_record_of_a_core_ended_attempt_is_history() -> None:
    loop = opened(World((StepSpec(2.0),)))
    loop.tick()
    play(loop, ("tock", 3.0))
    tag = loop.running()[0]
    loop.add(Oneof("attempt_interrupted", interrupted_message(tag, 1, Interruption.RAISED)))
    assert not loop.running()


# --- a scope is closed after its steps --------------------------------------------------------


def test_the_close_of_a_scope_waits_for_every_other_step_of_the_scope() -> None:
    """A step that began before the close and reports its job after it would leave that job running."""
    loop = opened(
        World((StepSpec(100.0, Kind.JOB, 0), StepSpec(100.0, Kind.QUERY, 1)), closes=(0,))
    )
    started = [a.step for a in decide(loop.state, loop.clock) if isinstance(a, Run)]
    assert core.CloseAttemptScope not in {type(step) for step in started}
    assert {type(step.scope.generation) for step in started} == {int}
    loop.tick()
    assert all(not isinstance(row.request, core.CloseAttemptScope) for row in running_steps(loop))
    loop.finish_running(0)
    loop.finish_running(0)
    closing = [a.step for a in decide(loop.state, loop.clock) if isinstance(a, Run)]
    assert [type(step) for step in closing] == [core.CloseAttemptScope]


def test_a_step_that_waits_for_the_close_of_its_scope_does_not_hold_the_close_back() -> None:
    loop = opened(
        World(
            (StepSpec(100.0, Kind.QUERY, 0), StepSpec(100.0, Kind.QUERY, 0, after_close=True)),
            closes=(0,),
        )
    )
    settle(loop)
    assert not [row for row in loop.state.records.records if row.phase in OPEN]


def running_steps(loop: Loop) -> list[core.RequestRecord]:
    return [
        row
        for row in loop.state.records.records
        if (attempt := loop.state.steps.of(row.request_id)) is not None and not attempt.ended
    ]
