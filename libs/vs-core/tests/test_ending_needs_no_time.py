"""A run the operator stops by cancel ends at the instant its last step has a result.

Rule 4 of the recovery design: ending a run cannot fail and waits for nothing but the steps the
loop has cancelled. The loop cancels (`CancelReason.RUN_ENDING`) every step a cancelled run holds
and starts none, so core must not hold the run `closing` on an answer that only a step the loop
no longer runs could bring: a poll of a job, an inspection, a release no one will prove. Core
would otherwise ask again after a backoff, and a run that closes only when a timer fires ends
after the host's stop grace, as `stopped` and not as what it was stopped for.

The property is over the loop model of `recovery_loop.py`: whatever the run went through before
the stop (pauses, a crash, clock jumps, steps that finished or raised), the loop that cancels what
runs and settles what is open reaches the run's end at the clock the stop was given at. The worlds
hold a measurement job the way a job step reports it (one terminal, released observation of the
submission, with no poll after it) and the steps of every kind the rule treats differently.
"""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import RunRest, Wait, at_rest, decide
from vs_sim.api.testing import examples

from .recovery_loop import MOVES, Loop, Move, opens_no_unserved_step, play
from .recovery_worlds import World, build, worlds
from .test_measurements import requested

_RESTARTS = frozenset({"crash", "relaunch_resume"})
"""Moves that open a new epoch: the loop model checks its own charging rules there, not this one."""

_STEADY = st.lists(MOVES.filter(lambda move: move[0] not in _RESTARTS), max_size=10)

_SPENT = 40
"""Most rounds the cooperative loop gets to end a run: far above what a run of these worlds needs."""


class WaitedForTimeError(AssertionError):
    """The run needed the clock to move before it could end."""

    def __init__(self, status: core.RunStatus, until: float | None, at: float) -> None:
        super().__init__(f"a stopped run ({status.value}) waits until {until} at {at}")


def measurement_loop() -> Loop:
    """A run with one measurement of its own, as a job step runs it, in a fresh epoch."""
    asked = requested()
    loop = Loop(asked.state, core.OperationRegistry())
    loop.open_epoch()
    return loop


def world_loop(world: World) -> Loop:
    state, registry = build(world)
    loop = Loop(state, registry)
    loop.open_epoch()
    return loop


def stop_and_end(loop: Loop, late: list[int], *, by_search: bool) -> None:
    """Stop the run by cancel, and let the loop that cancels and settles run, with no time passing.

    The operator stops a run by a control; the search, which a paused run does not hear, ends one
    that is running with a finish that cancels. A
    runner whose step was running when the stop came may report before the loop cancels it
    (``late``: which steps), and the report is a finish the run takes while it ends.
    """
    result = core.RunResultProposal(outcome="cancelled", reason="operator")
    if by_search and loop.state.run.status is core.RunStatus.RUNNING:
        finish = core.Finish(
            decision_id=core.DecisionId(root="finish"),
            scope=core.Scope(owner=loop.state.run.run_id, generation=loop.state.run.generation),
            mode="cancel",
            result=result,
        )
        loop.offer(core.DecisionSubmitted(decision=finish, expected_revision=loop.state.revision))
    else:
        control = loop.control("stop", mode=core.StopMode.CANCEL)
        loop.offer(core.RunControlEvent(control=control, now_at=loop.clock, result=result))
    for index in late:
        loop.finish_running(index)
    opens_no_unserved_step(loop)
    clock = loop.clock
    for _ in range(_SPENT):
        actions = decide(loop.state, loop.clock)
        waits = [action for action in actions if isinstance(action, Wait)]
        if waits and waits[0].until is not None:
            raise WaitedForTimeError(loop.state.run.status, waits[0].until, clock)
        if all(isinstance(action, Wait) for action in actions):
            break
        loop.tick()
    assert loop.clock == clock
    assert at_rest(loop.state) is RunRest.ENDED, loop.state.run.status


_FINISHED = [("tick", None), ("finish", 0)]
"""A job step ran, and reported its job released."""

_LATE = st.lists(st.integers(0, 3), max_size=3)


@example(moves=_FINISHED, lag=False, late=[], by_search=False)
@example(moves=[("tick", None), ("finish", 0), ("tock", 3.0)], lag=False, late=[], by_search=False)
@example(moves=[("tick", None)], lag=False, late=[0], by_search=True)
@example(moves=[("tick", None)], lag=False, late=[0], by_search=False)
@example(moves=[("tick", None)], lag=False, late=[], by_search=True)
@example(moves=[], lag=True, late=[], by_search=False)
@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(moves=_STEADY, lag=st.booleans(), late=_LATE, by_search=st.booleans())
def test_a_cancelled_run_holding_a_measurement_ends_with_no_time_passing(
    moves: list[Move], late: list[int], *, lag: bool, by_search: bool
) -> None:
    loop = measurement_loop()
    for move in moves:
        play(loop, move)
        if not lag or move[0] == "tick":
            loop.tick()
    if loop.state.run.result is None:
        stop_and_end(loop, late, by_search=by_search)


@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(world=worlds(), moves=_STEADY, lag=st.booleans(), late=_LATE, by_search=st.booleans())
def test_a_cancelled_run_of_any_world_ends_with_no_time_passing(
    world: World, moves: list[Move], late: list[int], *, lag: bool, by_search: bool
) -> None:
    loop = world_loop(world)
    for move in moves:
        play(loop, move)
        if not lag or move[0] == "tick":
            loop.tick()
    if loop.state.run.result is None:
        stop_and_end(loop, late, by_search=by_search)
