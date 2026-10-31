"""The class behind the seams: whatever the loop decides from a hint of its own, core already records.

The loop and core are real here (``decide``, ``fold`` and ``run_step_loop`` over the confirmed log's
records), and only the far side is the Fake: a runner with per-step reachability, work that outlives
a host and hands back its result, calls that ignore a cancel. A trajectory is a few processes over
one run.log, each reaching a different backend, taking an operator's control or being killed at a
given moment, seeded with steps an earlier version left in flight. Every trajectory holds:

- after a stop the run ends within a bound, whatever clears are unreachable;
- no attempt starts on a step that may have work unless a ``clear`` returned first;
- a result the work itself ended with is never replaced by one core settled;
- a pause never ends the work of a runner that keeps it across a host;
- a loop that reports it rested rests where the fold of the log says, never PARKED while draining,
  and a parked run holds an attempt only for a call that ignored its cancel (the process exit ends
  it, and the next epoch does not charge it);
- the charge a step carries does not depend on when an unrelated step happens to end.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import RunRest, at_rest, finished_message, fold, interruption_of
from vs_proto.api import StepFinished
from vs_runtime.api.testing import (
    AttemptHangs,
    AttemptHolds,
    AttemptRaises,
    AttemptReturns,
    FakeStepRunner,
    RunnerMethod,
    fake_run_seed,
)
from vs_sim.api.testing import VirtualClock, examples, run_virtual

from .test_step_loop_seams import (
    World,
    pause_event,
    pause_now_event,
    resume_event,
    stop_event,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_runtime.api.testing import Behavior

    from .test_step_loop_seams import Process

BEHAVIORS: tuple[Behavior, ...] = (
    AttemptReturns(after_s=1.0),
    AttemptReturns(after_s=4.0),
    AttemptReturns(after_s=9.0),
    AttemptRaises(OSError("the host lost the process")),
    AttemptHangs(),
    AttemptHangs(ignoring_signal=True),
    AttemptHolds(for_s=3.0),
    AttemptHolds(for_s=25.0),
)
CONTROLS = (None, None, "pause", "pause for 1s", "pause for 3s", "pause now", "stop")
STANDINGS = (
    core.RequestPhase.PREPARED,
    core.RequestPhase.DISPATCHED,
    core.RequestPhase.RECONCILING,
)
LIMITS = (6.0, 20.0, 60.0)
STOP_AFTER_S = 40.0
"""When the operator ends a run that is still going at the end of the last process."""


@dataclass(frozen=True)
class Epoch:
    """One process over the run.log: what its backend reaches, what the operator does, how it ends."""

    unreachable: tuple[int, ...]
    """The steps whose ``clear`` cannot reach its backend while this process runs."""
    control: str | None
    at_s: float
    killed_after_s: float | None
    """``None`` runs the process to its rest; only the last process does."""


@dataclass(frozen=True)
class Plan:
    scripts: tuple[tuple[Behavior, ...], ...]
    limits: tuple[float, ...]
    standing: core.RequestPhase
    adopts: bool
    retries: int
    epochs: tuple[Epoch, ...]


@st.composite
def plans(draw: st.DrawFn) -> Plan:
    count = draw(st.integers(1, 3))
    scripts = tuple(
        tuple(draw(st.lists(st.sampled_from(BEHAVIORS), min_size=1, max_size=3)))
        for _ in range(count)
    )
    n = draw(st.integers(1, 3))
    epochs = tuple(
        Epoch(
            unreachable=tuple(draw(st.sets(st.integers(0, count - 1), max_size=count))),
            control=draw(st.sampled_from(CONTROLS)),
            at_s=draw(st.sampled_from([0.5, 1.0, 2.0, 4.0])),
            killed_after_s=None if index == n - 1 else draw(st.sampled_from([2.0, 5.0, 12.0])),
        )
        for index in range(n)
    )
    return Plan(
        scripts=scripts,
        limits=tuple(draw(st.sampled_from(LIMITS)) for _ in range(count)),
        standing=draw(st.sampled_from(STANDINGS)),
        adopts=draw(st.booleans()),
        retries=draw(st.integers(0, 2)),
        epochs=epochs,
    )


def control_of(clock: VirtualClock, name: str) -> core.RunControlEvent:
    match name:
        case "pause":
            return pause_event(clock)
        case "pause for 1s":
            return pause_event(clock, drain_s=1.0)
        case "pause for 3s":
            return pause_event(clock, drain_s=3.0)
        case "pause now":
            return pause_now_event(clock)
        case _:
            return stop_event(clock)


class ViolationError(AssertionError):
    """A trajectory broke one of the properties of the class."""


def fail(plan: Plan, what: str) -> ViolationError:
    return ViolationError(f"{what}\n{plan}")


async def play(plan: Plan, clock: VirtualClock) -> World:
    seed = fake_run_seed(list(plan.limits), plan.retries, standing=plan.standing)
    tags = seed.tags
    world = World(clock, seed)
    scripts = dict(zip(tags, plan.scripts, strict=True))

    def behave(_step: core.Request, tag: core.RequestId, attempt: int) -> Behavior:
        script = scripts[tag]
        return script[min(attempt, len(script)) - 1]

    world.runner = FakeStepRunner(clock, adopts=plan.adopts, behave=behave)
    for index, epoch in enumerate(plan.epochs):
        last = epoch.killed_after_s is None
        unreachable = [tags[i] for i in epoch.unreachable]
        reach = world.runner.unreachable(*unreachable) if unreachable else contextlib.nullcontext()
        with reach:
            process = world.start(*((resume_event(clock, index),) if paused(world) else ()))
            stopped = await operate(process, epoch, clock)
            if not last:
                await process.die()
                continue
            rest = await process.rest()
        await check_rest(plan, world, process, rest, stopped=stopped)
        world.runner.process_died()
        if rest is not RunRest.PARKED:
            break
    return world


def paused(world: World) -> bool:
    """Whether the log leaves the run paused, which only an operator's resume ends."""
    if not world.records.records:
        return False
    state = fold(world.records.read(), world.seed.state, registry=world.seed.registry)
    return state.run.status in (core.RunStatus.PARKED, core.RunStatus.DRAINING)


async def operate(process: Process, epoch: Epoch, clock: VirtualClock) -> bool:
    """The operator's control, then the wait to the process's kill or the moment it is stopped.

    Returns whether the run was stopped while it was running, which obliges it to end.
    """
    await clock.sleep(epoch.at_s)
    stopped = False
    if epoch.control is not None and process.log.state.run.status is core.RunStatus.RUNNING:
        process.log.push(control_of(clock, epoch.control))
        stopped = epoch.control == "stop"
    if epoch.killed_after_s is not None:
        await clock.sleep(epoch.killed_after_s)
        return stopped
    if not stopped:
        await clock.sleep(STOP_AFTER_S)
        if process.log.state.run.status is core.RunStatus.RUNNING:
            process.log.push(stop_event(clock))
            stopped = True
    return stopped


async def check_rest(
    plan: Plan, world: World, process: Process, rest: RunRest | None, *, stopped: bool
) -> None:
    state = fold(world.records.read(), world.seed.state, registry=world.seed.registry)
    if stopped and rest is not RunRest.ENDED:
        raise fail(plan, f"the run was stopped and did not end (rested {rest})")
    if rest is RunRest.PARKED and state.run.status is not core.RunStatus.PARKED:
        raise fail(plan, f"the loop reported PARKED and the log says {state.run.status}")
    if rest is RunRest.PARKED:
        running = {row.tag for row in state.steps.attempts if not row.ended}
        if not running <= process.board.held_ever:
            raise fail(plan, "the run parked with an attempt running that no held call explains")
    if rest is RunRest.ENDED and at_rest(state) is not RunRest.ENDED:
        raise fail(plan, "the loop reported ENDED and the log does not end the run")
    if rest is None and process.log.state.run.status is core.RunStatus.PARKED:
        raise fail(plan, "the log is parked and the loop never returned")


def check_world(plan: Plan, world: World) -> None:
    runner = world.runner
    assert runner is not None
    if runner.violations:
        raise fail(plan, f"the runner's contract was broken: {runner.violations}")
    check_clear_precedes_every_attempt(plan, world)
    for tag, adopted in runner.adopted_results.items():
        results = [m for m in world.fields(StepFinished) if m.tag == tag.root]
        if results and results[0].result != finished_message(tag, 0, adopted).result:
            raise fail(plan, f"the adopted result of {tag.root} was replaced")
    for tag, reason in runner.ended_by_cancel:
        if plan.adopts and interruption_of(reason) is not None:
            raise fail(plan, f"a pause ended the work of {tag.root}")
    if world.records.count("run_ended") > 1:
        raise fail(plan, "the run's end was recorded twice")


def check_clear_precedes_every_attempt(plan: Plan, world: World) -> None:
    """A step that was sent, or that an attempt ran, has a ``clear`` return before its next attempt."""
    runner = world.runner
    assert runner is not None
    sent = plan.standing is not core.RequestPhase.PREPARED
    cleared = dict.fromkeys(world.seed.tags, not sent)
    for call in runner.calls:
        if call.kind is RunnerMethod.CLEAR:
            cleared[call.tag] = True
        else:
            if not cleared[call.tag]:
                raise fail(plan, f"attempt {call.attempt} of {call.tag.root} had no clear")
            cleared[call.tag] = False


@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(plan=plans())
def test_any_trajectory_of_processes_keeps_what_the_loop_owes_core_s_facts(plan: Plan) -> None:
    clock = VirtualClock()
    check_world(plan, run_virtual(clock, play(plan, clock)))


OTHERS: dict[str, Callable[[core.RequestId], Behavior | None]] = {
    "absent": lambda _tag: None,
    "ends before the pause": lambda _tag: AttemptReturns(after_s=0.5),
    "ends after the deadline": lambda _tag: AttemptReturns(after_s=4.0),
    "hangs": lambda _tag: AttemptHangs(),
    "raises": lambda _tag: AttemptRaises(OSError("lost")),
}


async def charged_at_relaunch(
    clock: VirtualClock, held: Behavior, other: str, drain_s: float, pause_at_s: float
) -> int:
    """How many tries the first step carries when a second process takes the paused run over."""
    extra = OTHERS[other](core.RequestId(root="other"))
    seed = fake_run_seed([60.0] if extra is None else [60.0, 60.0], 0)
    first = seed.tags[0]
    world = World(clock, seed)

    def behave(_step: core.Request, tag: core.RequestId, attempt: int) -> Behavior:
        if tag == first:
            return held if attempt == 1 else AttemptReturns(after_s=1.0)
        assert extra is not None
        return extra

    world.runner = FakeStepRunner(clock, behave=behave)
    process = world.start()
    await clock.sleep(pause_at_s)
    process.log.push(pause_event(clock, drain_s=drain_s))
    await process.rest()
    world.runner.process_died()
    relaunched = world.start()
    row = relaunched.log.state.steps.of(first)
    await relaunched.die()
    assert row is not None
    return row.charged


@settings(max_examples=examples(pr=6, nightly=100), deadline=None)
@given(
    held=st.sampled_from(
        [AttemptHolds(for_s=500.0), AttemptHangs(ignoring_signal=True), AttemptHangs()]
    ),
    drain_s=st.sampled_from([1.0, 3.0]),
    pause_at_s=st.sampled_from([1.0, 2.0]),
)
def test_the_charge_of_a_step_does_not_depend_on_when_an_unrelated_step_ends(
    held: Behavior, drain_s: float, pause_at_s: float
) -> None:
    charges = {}
    for other in OTHERS:
        clock = VirtualClock()
        charges[other] = run_virtual(
            clock, charged_at_relaunch(clock, held, other, drain_s, pause_at_s)
        )
    assert len(set(charges.values())) == 1, charges
