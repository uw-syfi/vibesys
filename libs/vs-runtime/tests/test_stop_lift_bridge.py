"""A process the operator resumed lifts the stop core recorded; any other start keeps it.

The stop reached core and was committed before the process ended. The next process arms the
channel's stop again, as the run's stop bound, unless it was started to resume the run, in
which case its first poll records the lift and the channel never stops. A run whose ending has
committed stays stopping: core takes no resume from there.
"""

from __future__ import annotations

import pytest

from vs_core.api import (
    DEADLINE_STOP,
    ControlId,
    ControlInput,
    RunResultProposal,
    RunStatus,
    StopLiftRecorded,
    StopMode,
    requested_stop_mode,
    step,
)
from vs_core.api import RunControlEvent as ControlEvent
from vs_project.api.testing import FakeControlInbox
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import StopRequest

from .test_pause_record import _restarted, _Run, _run, _Texts


def _stopped_run() -> _Run:
    run = _run()
    run.control.submit(StopRequest(control_id="s1"))
    run.poll()
    assert requested_stop_mode(run.core.run) is StopMode.DRAIN
    return run


def _new_process(old: _Run, *, resumed: bool) -> _Run:
    fresh = _restarted(old)
    if resumed:
        fresh.bridge = RunControlBridge(
            fresh.channel,
            _Texts(),
            inbox=OperatorInbox(
                FakeControlInbox() if old.inbox is None else old.inbox, fresh.control
            ),
            lifts_standing_stop=True,
        )
    return fresh


def test_a_restart_keeps_the_stop_in_force() -> None:
    run = _new_process(_stopped_run(), resumed=False)
    run.poll()
    assert run.channel.stop_requested()
    assert requested_stop_mode(run.core.run) is StopMode.DRAIN


def test_a_resumed_process_records_the_lift_and_never_stops() -> None:
    """Regression: a run the operator stopped could not be resumed; it stopped again at once."""
    run = _new_process(_stopped_run(), resumed=True)
    events = run.poll(commit=False)
    assert not run.channel.stop_requested()
    committed = []
    for event in events:
        transition = step(run.core, event)
        run.core = transition.state
        committed += transition.events
    assert any(isinstance(event, StopLiftRecorded) for event in committed)
    assert requested_stop_mode(run.core.run) is None
    run.poll()
    assert not run.channel.stop_requested()


def test_a_resumed_process_that_stops_again_stops() -> None:
    run = _new_process(_stopped_run(), resumed=True)
    run.poll()
    run.control.submit(StopRequest(control_id="s2"))
    run.poll()
    assert run.channel.stop_requested()
    assert requested_stop_mode(run.core.run) is StopMode.DRAIN


def _ended_by_the_stop() -> _Run:
    old = _run()
    stopping = ControlEvent(
        control=ControlInput(control_id=ControlId(root="stop"), action="stop", mode=StopMode.DRAIN),
        now_at=1.0,
        result=RunResultProposal(outcome="cancelled", reason="operator stop"),
    )
    old.core = step(old.core, stopping).state
    return old


def test_a_resumed_process_lifts_the_stop_that_ended_the_run_and_reopens_it() -> None:
    """A stop is a pause: the run its ending left terminal is reopened by the resume."""
    old = _ended_by_the_stop()
    assert old.core.run.status is RunStatus.TERMINAL
    run = _new_process(old, resumed=True)
    run.poll()
    assert not run.channel.stop_requested()
    assert run.core.run.status is RunStatus.RUNNING
    assert requested_stop_mode(run.core.run) is None


def test_a_run_whose_ending_is_still_closing_stays_stopping_even_when_resumed() -> None:
    """Cleanup finishes first: core takes no resume until the ending's cleanup is done."""
    old = _ended_by_the_stop()
    old.core = old.core.model_copy(
        update={"run": old.core.run.model_copy(update={"status": RunStatus.CLOSING})}
    )
    run = _new_process(old, resumed=True)
    run.bridge.poll(run.core, 2.0)
    assert run.channel.stop_requested()


def _stopped_by_the_host(stopper: str) -> _Run:
    """A run the host stopped while it ran; the process died before the run closed."""
    old = _run()
    if stopper == "environment":
        old.channel.request_environment_stop()
        old.poll()
    else:
        deadline = ControlInput(
            control_id=ControlId(root=DEADLINE_STOP), action="stop", mode=StopMode.DRAIN
        )
        old.core = step(old.core, ControlEvent(control=deadline, now_at=1.0)).state
    assert requested_stop_mode(old.core.run) is not None
    assert old.core.run.status is RunStatus.RUNNING
    return old


@pytest.mark.parametrize("replayed", [False, True], ids=["fresh", "failure-replayed"])
@pytest.mark.parametrize("stopper", ["environment", "deadline"])
def test_a_resumed_process_keeps_the_stop_the_host_delivered(
    stopper: str, *, replayed: bool
) -> None:
    """Regression (review B1 of #3327): a resume lifted the run environment's stop for good.

    The environment's stop was recorded, the process died before the run closed (a force stop,
    a crash), and ``--resume`` lifted the stop. The stored probe record replayed the failure,
    but core already held the stop's id, so the bridge sent nothing: the channel was stopped,
    core held no stop, and the run went on with every Slurm job refused. The deadline's stop was
    lifted the same way. The host's stops are no pause: the resumed process keeps them in force.
    """
    run = _new_process(_stopped_by_the_host(stopper), resumed=True)
    run.poll()
    if replayed and stopper == "environment":
        run.channel.request_environment_stop()
        run.poll()
    assert run.channel.stop_requested()
    assert requested_stop_mode(run.core.run) is not None
