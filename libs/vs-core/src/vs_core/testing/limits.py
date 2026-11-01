"""The end of a step at its limit, as the runner loop records it, for tests that hold no runner.

A limit that passes ends a step through one mechanism: ``decide`` settles or cancels the step, and
the loop records the result. Tests of the pure core run no loop, so ``reach`` does what the loop
does when time reaches a moment: it records, one at a time, what ``decide`` asks to end.
"""

from __future__ import annotations

from vs_core._actions import Cancel, Settle
from vs_core._decide import decide, finished
from vs_core._step import advance_run_time
from vs_core.types.kernel import CoreState, StrategyEvent, Transition
from vs_core.types.steps import Cancelled


def reach(state: CoreState, now: float) -> Transition:
    """``state`` once the run clock reaches ``now`` and the loop has recorded every end ``decide`` asks for.

    A step whose limit passed ends ``Cancelled(DEADLINE)``, and one whose watch fired ends
    ``Cancelled(WITHDRAWN)``; the events are what the search hears of them. No attempt runs here,
    so a ``Cancel`` is recorded as the attempt's own result, as a runner that honors it returns.
    """
    state = advance_run_time(state, now)
    events: list[StrategyEvent] = []
    while True:
        ending = [action for action in decide(state, now) if isinstance(action, Settle | Cancel)]
        if not ending:
            return Transition(state=state, requests=(), events=tuple(events))
        action = ending[0]
        result = action.result if isinstance(action, Settle) else Cancelled(reason=action.reason)
        attempts = state.steps.of(action.tag)
        state, heard = finished(
            state, action.tag, 0 if attempts is None else attempts.started, result
        )
        events.extend(heard)
