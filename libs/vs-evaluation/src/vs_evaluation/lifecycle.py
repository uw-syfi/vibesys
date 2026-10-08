"""The one rule for how an evaluation's published lifecycle may change.

Every executor publishes observations of its evaluations. Left to each executor,
the rule "a finished evaluation stays finished" is restated at every publish
site and is missed at the sites nobody tests (a cancel racing a completion, a
close before a task starts). This module owns the rule once:

* ``join_observation`` is the pure transition function.
* ``LifecyclePublisher`` is the process-local holder of published observations
  and their change notifications. It applies the join on every publish, so an
  executor cannot publish a regression even by accident.

``FINISHED_STATES`` and ``state_rank`` are the only definition of which states
end an evaluation and in what order the others are visited.
"""

from __future__ import annotations

import asyncio

from vs_evaluation.models import EvaluationState, ExecutorObservation

FINISHED_STATES = frozenset(
    {
        EvaluationState.SUCCEEDED,
        EvaluationState.FAILED,
        EvaluationState.CANCELED,
        EvaluationState.SUPERSEDED,
    }
)

# Non-finished states in the order an evaluation visits them. Every finished
# state ranks above all of them and equal to each other.
_ACTIVE_ORDER = (
    EvaluationState.QUEUED,
    EvaluationState.STARTING,
    EvaluationState.RUNNING,
    EvaluationState.CANCELING,
)
_FINISHED_RANK = len(_ACTIVE_ORDER)


def is_finished(state: EvaluationState) -> bool:
    """Whether ``state`` ends the evaluation: nothing may replace it."""
    return state in FINISHED_STATES


def state_rank(state: EvaluationState) -> int:
    """Position of ``state`` in the lifecycle order; all finished states tie."""
    return _ACTIVE_ORDER.index(state) if state in _ACTIVE_ORDER else _FINISHED_RANK


def join_observation(held: ExecutorObservation, new: ExecutorObservation) -> ExecutorObservation:
    """The observation to hold after ``new`` arrives, as a join in the lifecycle order.

    A lower state is not news, and the first finished state wins: a late CANCELED never
    replaces a collected SUCCEEDED, and a late SUCCEEDED never replaces CANCELED. Between
    equal active states the newer reading wins but keeps the stage the older one named
    when it names none. The result is never lower than either input, and re-joining the
    same reading changes nothing.
    """
    held_rank, new_rank = state_rank(held.state), state_rank(new.state)
    if new_rank < held_rank or (new_rank == held_rank and is_finished(held.state)):
        return held
    if new_rank == held_rank and new.current_stage is None and held.current_stage is not None:
        return new.model_copy(update={"current_stage": held.current_stage})
    return new


class LifecyclePublisher:
    """Published observations of one executor's evaluations, joined on every publish.

    Single-event-loop use only. ``publish`` is the only way an observation enters, so
    the lifecycle of every handle is monotone by construction.
    """

    def __init__(self) -> None:
        """Start with no published evaluation."""
        self._observations: dict[str, ExecutorObservation] = {}
        self._changes: dict[str, asyncio.Event] = {}

    def publish(self, handle_id: str, observation: ExecutorObservation) -> ExecutorObservation:
        """Join ``observation`` into what is held and return the observation now held."""
        held = self._observations.get(handle_id)
        joined = observation if held is None else join_observation(held, observation)
        if joined is not held:
            self._observations[handle_id] = joined
            self.change_event(handle_id).set()
        return joined

    def observation(self, handle_id: str) -> ExecutorObservation | None:
        """The held observation, or None when nothing was published for the handle."""
        return self._observations.get(handle_id)

    def is_finished(self, handle_id: str) -> bool:
        """Whether the handle's held observation is finished."""
        held = self._observations.get(handle_id)
        return held is not None and is_finished(held.state)

    def change_event(self, handle_id: str) -> asyncio.Event:
        """The sticky notification set by each publish that changed the handle."""
        event = self._changes.get(handle_id)
        if event is None:
            event = self._changes[handle_id] = asyncio.Event()
        return event

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        """Wait boundedly for a publish; a change published before the call counts."""
        event = self.change_event(handle_id)
        if not event.is_set():
            try:
                async with asyncio.timeout(timeout_s):
                    await event.wait()
            except TimeoutError:
                pass
        event.clear()
