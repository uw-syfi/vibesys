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
    ClockAdvanced,
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

from .test_pause_record import _restarted, _Run, _run, _Texts


def _stopped_run() -> _Run:
    run = _run()
    run.control.stop()
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
    run.control.stop()
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
def test_a_resumed_process_lifts_each_old_host_stop(stopper: str, *, replayed: bool) -> None:
    run = _new_process(_stopped_by_the_host(stopper), resumed=True)
    run.poll()
    assert not run.channel.stop_requested()
    assert requested_stop_mode(run.core.run) is None
    if replayed and stopper == "environment":
        run.core = step(run.core, ClockAdvanced(now_at=1.0)).state
        run.channel.request_environment_stop()
        run.poll()
        assert run.channel.stop_requested()
        assert requested_stop_mode(run.core.run) is StopMode.CANCEL


@pytest.mark.parametrize("lifts", [1, 2, 3])
def test_the_environments_stop_is_recorded_again_after_every_resume(lifts: int) -> None:
    """Each process's environment stop is its own control: a Resume lifts one, not the next.

    The stop's id was derived from ``records.recovery.epoch``, which no process ever advances,
    so the second process's stop carried the first's id, core held it already and the bridge
    recorded nothing: the channel stopped the run and core never heard.
    """
    run = _run()
    for _ in range(lifts):
        run.channel.request_environment_stop()
        run.poll()
        assert requested_stop_mode(run.core.run) is StopMode.CANCEL
        run = _new_process(run, resumed=True)
        run.poll()
        assert requested_stop_mode(run.core.run) is None
    run.channel.request_environment_stop()
    run.poll()
    assert requested_stop_mode(run.core.run) is StopMode.CANCEL
