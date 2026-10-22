"""A run's stop escalates the same for the episodes it holds in admission.

A cancel stop retires every episode still waiting for a slot (one cancel closure each);
a drain stop leaves them queued until the work in its slots ends. The stop in force is
the strongest asked for, whatever the order of the operator's stops and the search's own
end of the run, whether the run is paused, and however often core restarts in between.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_cancel_stop_run_jobs import _operator_stop, _reload, _strategy_finish
from .test_scheduling import _owner, _request, _slot, _state

if TYPE_CHECKING:
    from collections.abc import Iterable

_QUEUED = 2


def _held_and_queued() -> core.CoreState:
    """A run whose one slot is held and whose next episodes wait in admission."""
    held = _request(0)
    queued = tuple(_request(index) for index in range(1, 1 + _QUEUED))
    return _state(
        owners=(
            _owner(held, phase=core.AttemptPhase.ACTIVE),
            *(_owner(request) for request in queued),
        ),
        slots=(_slot(held),),
        queue=queued,
    )


def _pause(now_at: float) -> core.RunControlEvent:
    control = core.ControlInput(
        control_id=core.ControlId(root="op-pause"),
        action="pause",
        owner=core.PauseOwner.QUOTA,
    )
    return core.RunControlEvent(control=control, now_at=now_at)


class _Run:
    """Core, and every closure it asked for the queued episodes."""

    def __init__(self, state: core.CoreState) -> None:
        self.state = state
        self.now = 1.0
        self.closures: list[core.AttemptId] = []

    def feed(self, event: core.CoreEvent) -> None:
        result = core.step(self.state, event)
        self.state = result.state
        self.closures.extend(_closed(result.requests))
        self.now += 1.0

    def tick(self) -> None:
        self.feed(core.ClockAdvanced(now_at=self.now))

    def recover(self, epoch: int) -> None:
        self.state = _reload(self.state)
        self.feed(core.RecoveryStarted(epoch=epoch, now_at=self.now))
        self.tick()

    def queued(self) -> tuple[core.AttemptId, ...]:
        return tuple(
            attempt.attempt_id
            for attempt in self.state.attempts.attempts
            if attempt.phase is core.AttemptPhase.QUEUED
        )


def _closed(requests: Iterable[core.Request]) -> list[core.AttemptId]:
    return [
        request.attempt.attempt_id
        for request in requests
        if isinstance(request, core.CloseAttemptScope)
    ]


def _assert_stop_in_force(run: _Run, *, cancelled: bool) -> None:
    waiting = tuple(_request(index).attempt_id for index in range(1, 1 + _QUEUED))
    if cancelled:
        assert sorted(run.closures, key=str) == sorted(waiting, key=str)
        assert run.queued() == ()
    else:
        assert run.closures == []
        assert run.queued() == waiting


@pytest.mark.regression
@settings(max_examples=examples(pr=20, nightly=500), deadline=None, database=None)
@example(stops=[core.StopMode.DRAIN, core.StopMode.CANCEL], finish_at=1, paused=True, restart=True)
@example(stops=[core.StopMode.CANCEL, core.StopMode.DRAIN], finish_at=0, paused=False, restart=True)
@given(
    stops=st.lists(st.sampled_from(core.StopMode), min_size=1, max_size=4),
    finish_at=st.integers(0, 4),
    paused=st.booleans(),
    restart=st.booleans(),
)
def test_every_queued_episode_is_cancelled_once_exactly_when_a_cancel_was_asked(
    stops: list[core.StopMode], finish_at: int, *, paused: bool, restart: bool
) -> None:
    """periodic-1330 MED-2 across admission, pause and restart.

    ``restart`` opens fresh epochs after each step. Same-epoch escalation keeps its
    strongest cause; completed cleanup remains unique across successor epochs.
    """
    run = _Run(_held_and_queued())
    if paused:
        run.feed(_pause(run.now))
    finished = False
    for index, mode in enumerate([*stops, None]):
        if not finished and (index >= finish_at or mode is None):
            finished = True
            run.feed(_strategy_finish(run.state, stops[0] if index else core.StopMode.DRAIN))
        if mode is not None:
            run.feed(_operator_stop(index, mode, run.now))
        run.tick()
        if restart:
            run.recover(epoch=index + 1)
    if restart:
        assert core.stop_mode(run.state.run) is None
        assert len(run.closures) == len(set(run.closures))
        waiting = {_request(index).attempt_id for index in range(1, 1 + _QUEUED)}
        assert set(run.queued()) == waiting - set(run.closures)
    else:
        _assert_stop_in_force(run, cancelled=core.StopMode.CANCEL in stops)


def _recorded_by_an_older_build(state: core.CoreState, mode: core.StopMode) -> core.CoreState:
    """The state a build before #3209 saved: it recorded the stop and acted on nothing."""
    control = core.ControlInput(
        control_id=core.ControlId(root="op-stop-late"), action="stop", mode=mode
    )
    run = state.run.model_copy(update={"controls": (*state.run.controls, control)})
    return state.model_copy(update={"run": run})


@pytest.mark.regression
@pytest.mark.parametrize("paused", [False, True])
def test_a_cancel_saved_after_a_drain_finish_cancels_on_recovery(*, paused: bool) -> None:
    """A state saved before #3209 holds a drain Finish followed by a recorded cancel.

    The older build recorded the cancel and changed nothing. The state loads, its stop
    in force is the cancel, and recovering it cancels every episode still waiting for a
    slot, once; a drain recorded the same way changes nothing. A successor epoch
    then retains that cleanup as history without inheriting the stop.
    """
    for mode in core.StopMode:
        run = _Run(_held_and_queued())
        if paused:
            run.feed(_pause(run.now))
        run.feed(_operator_stop(0, core.StopMode.DRAIN, run.now))
        run.feed(_strategy_finish(run.state, core.StopMode.DRAIN))
        run.state = _recorded_by_an_older_build(run.state, mode)
        run.recover(1)
        assert core.stop_mode(run.state.run) is mode
        _assert_stop_in_force(run, cancelled=mode is core.StopMode.CANCEL)
        before = (tuple(run.closures), run.queued())
        run.recover(2)
        assert core.stop_mode(run.state.run) is None
        assert (tuple(run.closures), run.queued()) == before
