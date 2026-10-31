"""Every action `decide` returns can be recorded, the fold accepts its record, and the run moves.

This is the class property behind the recovery rule: whatever state a run reaches, the actions
core asks for are actions the log can hold. A model of the runner loop (`recovery_loop.py`) writes,
for each action, the record the loop writes, and trajectories of operator pauses and stops, quota
pauses, runner results and failures, late reports, crashes, resumes and clock jumps drive the run
through its states. The worlds (`recovery_worlds.py`) hold steps of every kind the rule treats
differently, in two scopes, as a fresh run or a restart finds them (a legacy standing, open
reconciliation requests, an epoch the host counted). The loop keeps up with the log after each
move, or lags and meets the next input with its attempts still running. In every one:

* the fold accepts the record of every action `decide` returns, in a state of any kind;
* applying an action changes the state, so no action repeats with nothing done, and a `Wait` is
  never already due;
* a control core takes into the log does what it says, and one it cannot take is refused to its
  sender, so no control is dropped without a word;
* a parked run has no attempt running, and a step keeps the limit it was first started with;
* a run left to cooperative runners reaches rest: ended, parked, or idle with nothing open. A
  draining run never stays draining once its attempts have ended, and every end the run reaches,
  after every resume, is a record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import RunRest, at_rest
from vs_sim.api.testing import examples

from .recovery_loop import MOVES, OPEN, Loop, Move, invariants, play, settle
from .recovery_worlds import StepSpec, World, build, query_world, worlds

if TYPE_CHECKING:
    from collections.abc import Callable

type Trajectory = tuple[World, list[Move], bool]


def pinned_worlds() -> list[tuple[World, list[Move]]]:
    """The trajectories each way a run is stopped, paused, crashed and resumed goes through."""
    return [
        (query_world((100.0,), 0), [("tick", None), ("pause", 1.0), ("clock", 3.0)]),
        (query_world((100.0,), 0), [("tick", None), ("pause", 1.0), ("tock", 3.0)]),
        (query_world((100.0, 100.0), 1), [("tick", None), ("pause_now", None)]),
        (query_world((100.0,), 3), [("tick", None), ("pause", None), ("raise", 0)]),
        (query_world((100.0,), 0), [("tick", None), ("crash", None), ("pause", None)]),
        (
            query_world((100.0,), 2),
            [("tick", None), ("pause", None), ("crash", None), ("relaunch_resume", None)],
        ),
        (
            query_world((100.0,), 2),
            [("tick", None), ("pause", 1.0), ("relaunch_resume", None), ("tick", None)],
        ),
        (query_world((2.0,), 0), [("tick", None), ("stop", 1)]),
        (query_world((100.0, 2.0), 0), [("tick", None), ("stop", 0)]),
        (query_world((2.0, 100.0), 0), [("clock", 3.0), ("tick", None), ("pause", None)]),
        (query_world((100.0,), 0), [("stop", 0), ("crash", None), ("relaunch_resume", None)]),
        (
            query_world((), 0),
            [("stop", 0), ("tick", None), ("relaunch_resume", None), ("stop", 0)],
        ),
        (
            query_world((100.0,), 3),
            [("tick", None), ("pause", None), ("relaunch_resume", None), ("relaunch_resume", None)],
        ),
        (
            World((StepSpec(100.0),), host_epoch=3),
            [("tick", None), ("pause", None), ("relaunch_resume", None)],
        ),
        (
            query_world((100.0,), 3),
            [
                ("tick", None),
                ("tock", 40.0),
                ("pause", None),
                ("tock", 3.0),
                ("relaunch_resume", None),
            ],
        ),
        (query_world((100.0,), 0), [("tick", None), ("quota_pause", None), ("resume_now", None)]),
    ]


def pinned[F: Callable[..., object]](test: F) -> F:
    """Run the pinned trajectories whatever the generator draws, with and without a lagging loop."""
    for world, moves in pinned_worlds():
        for lag in (False, True):
            test = example(world=world, moves=moves, lag=lag)(test)
    return test


@pinned
@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(world=worlds(), moves=st.lists(MOVES, max_size=14), lag=st.booleans())
def test_every_action_core_returns_can_be_recorded_and_moves_the_run(
    world: World, moves: list[Move], *, lag: bool
) -> None:
    state, registry = build(world)
    loop = Loop(state, registry)
    loop.open_epoch()
    for move in moves:
        play(loop, move)
        invariants(loop)
        if not lag or move[0] == "tick":
            loop.tick()
            invariants(loop)


@pinned
@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(world=worlds(), moves=st.lists(MOVES, max_size=14), lag=st.booleans())
def test_a_run_left_to_cooperative_runners_comes_to_rest_and_records_each_end(
    world: World, moves: list[Move], *, lag: bool
) -> None:
    state, registry = build(world)
    loop = Loop(state, registry)
    loop.open_epoch()
    for move in moves:
        play(loop, move)
        if not lag:
            loop.tick()
    settle(loop)
    status = loop.state.run.status
    assert status is not core.RunStatus.DRAINING, "a draining run with nothing running never parks"
    rest = at_rest(loop.state)
    open_steps = [row for row in loop.state.records.records if row.phase in OPEN]
    if rest is None and status is not core.RunStatus.PAUSED:
        assert not open_steps, "idle with a step open and nothing running"
    if loop.state.run.result is not None:
        assert rest is RunRest.ENDED, "a run with its result never published its end"
        recorded = [
            r
            for index, r in enumerate(loop.records)
            if r.body is not None and r.body.field == "run_ended" and index >= loop.reopened
        ]
        assert recorded, "the run is ended and this end is in no record"
