"""A run's stop escalates the same for the episodes it holds in admission.

A cancel stop retires every episode still waiting for a slot (one cancel closure each);
a drain stop leaves them queued until the work in its slots ends. The stop in force is
the strongest asked for, whatever the order of the operator's stops and the search's own
end of the run, whether the run is paused, and however often core restarts in between.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import vs_core.api as core

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


def _recorded_by_an_older_build(state: core.CoreState, mode: core.StopMode) -> core.CoreState:
    """The state a build before #3209 saved: it recorded the stop and acted on nothing."""
    control = core.ControlInput(
        control_id=core.ControlId(root="op-stop-late"), action="stop", mode=mode
    )
    run = state.run.model_copy(update={"controls": (*state.run.controls, control)})
    return state.model_copy(update={"run": run})
