"""The step loop acts on facts core records and returns, never on hints of its own.

Each case is a trajectory that crossed a seam between two recovery rules and left a run stuck, a
result replaced or an attempt charged for what it was not. The loop decided from a local hint (the
attempt number, an ``ending`` bit set by one action and not another, the state of the run it had
seen) where core's record said otherwise. Core is real here: ``decide``, ``fold`` and the loop run
over the confirmed log's records, and only the far side (a runner and its backend) is the Fake.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

import vs_core.api as core
from vs_core.api.recovery import (
    Cancelled,
    CancelReason,
    RunRest,
    finished_message,
)
from vs_proto.api import AttemptInterrupted, StepFinished, StepStarted
from vs_runtime.api.step_runner import StallBoard, StepLoopTiming, run_step_loop
from vs_runtime.api.testing import (
    AttemptHangs,
    AttemptHolds,
    AttemptReturns,
    FakeRunRecords,
    FakeRunSeed,
    FakeStepLog,
    FakeStepRunner,
    RunnerMethod,
    fake_run_seed,
)
from vs_sim.api.testing import VirtualClock, run_virtual, wait_for_state

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_runtime.api.testing import Behavior

TIMING = StepLoopTiming(
    cancel_grace_s=2.0, clear_timeout_s=3.0, clear_backoff_s=1.0, clear_backoff_max_s=4.0
)
BOUND_S = 3600.0
"""Virtual seconds a loop gets to reach its rest; it only guards against a hang."""


def stop_event(clock: VirtualClock) -> core.RunControlEvent:
    control = core.ControlInput(
        control_id=core.ControlId(root="operator-stop"), action="stop", mode=core.StopMode.CANCEL
    )
    result = core.RunResultProposal(outcome="cancelled", reason="the operator stopped the run")
    return core.RunControlEvent(control=control, now_at=clock.now(), result=result)


def pause_event(
    clock: VirtualClock, action: str = "pause", drain_s: float | None = None
) -> core.RunControlEvent:
    owner = core.PauseOwner.OPERATOR if action == "pause" else None
    control = core.ControlInput(
        control_id=core.ControlId(root=f"operator-{action}"), action=action, owner=owner
    )
    return core.RunControlEvent(control=control, now_at=clock.now(), drain_seconds=drain_s)


def resume_event(clock: VirtualClock, n: int = 1) -> core.RunControlEvent:
    control = core.ControlInput(
        control_id=core.ControlId(root=f"operator-resume-{n}"), action="resume", when="now"
    )
    return core.RunControlEvent(control=control, now_at=clock.now())


@dataclass
class World:
    """The far side of a run, which outlives the processes that host it."""

    clock: VirtualClock
    seed: FakeRunSeed
    records: FakeRunRecords = field(default_factory=FakeRunRecords)
    runner: FakeStepRunner | None = None
    processes: int = 0

    def behaving(self, behave: Callable[[core.Request, core.RequestId, int], Behavior]) -> None:
        self.runner = FakeStepRunner(self.clock, adopts=True, behave=behave)

    def start(self, *inputs: core.RunControlEvent) -> Process:
        """A new process over the run.log: it opens the next epoch, then runs the loop."""
        assert self.runner is not None
        self.processes += 1
        log = FakeStepLog(self.records, self.seed.state, self.seed.registry, self.clock)
        for event in inputs:
            log.push(event)
        board = StallBoard()
        loop = asyncio.ensure_future(run_step_loop(log, self.runner, self.clock, TIMING, board))
        return Process(self, log, board, loop)

    def fields(self, kind: type) -> list:
        return [
            r.body.value
            for r in self.records.records
            if r.body and r.body.field and isinstance(r.body.value, kind)
        ]

    def result_of(self, tag: core.RequestId) -> StepFinished | None:
        found = [m for m in self.fields(StepFinished) if m.tag == tag.root]
        assert len(found) <= 1, "a step has one result"
        return found[0] if found else None


@dataclass
class Process:
    world: World
    log: FakeStepLog
    board: StallBoard
    loop: asyncio.Task[RunRest]

    async def die(self) -> None:
        """The host is killed: the loop is abandoned with no record, and its threads die."""
        self.loop.cancel()
        await asyncio.gather(self.loop, return_exceptions=True)
        assert self.world.runner is not None
        self.world.runner.process_died()

    async def rest(self) -> RunRest | None:
        """How the loop rested within the bound, or ``None`` if it is still going."""
        timer = asyncio.ensure_future(self.world.clock.sleep(BOUND_S))
        await asyncio.wait({self.loop, timer}, return_when=asyncio.FIRST_COMPLETED)
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        return self.loop.result() if self.loop.done() else None

    async def stopped_when_done(self) -> RunRest | None:
        """The operator stops the run once every step has its result, and the loop rests."""
        await wait_for_state(
            lambda: self.world.records.count("step_finished"),
            lambda n: n == len(self.world.seed.tags),
            self.world.records.changes,
        )
        self.log.push(stop_event(self.world.clock))
        return await self.rest()


def hangs(_step: core.Request, _tag: core.RequestId, _attempt: int) -> Behavior:
    return AttemptHangs()


def test_a_stop_ends_the_run_while_the_clear_of_the_next_attempt_is_unreachable() -> None:
    """A crash, then attempt 2 waits in an unreachable ``clear``, then the operator stops the run.

    Core settles the step as cancelled for the run's end and has no attempt to cancel, so it
    asks for a ``Settle``, which the loop dropped while the step had a flight: the flight went on
    retrying a ``clear`` that an ending does not wait for.
    """
    clock = VirtualClock()

    async def main() -> None:
        world = World(clock, fake_run_seed([1000.0], 3))
        world.behaving(hangs)
        (tag,) = world.seed.tags
        first = world.start()
        await clock.sleep(1.0)
        await first.die()
        assert world.runner is not None
        with world.runner.unreachable():
            second = world.start()
            await clock.sleep(5.0)
            second.log.push(stop_event(clock))
            assert await second.rest() is RunRest.ENDED
        assert world.records.count("run_ended") == 1
        assert world.result_of(tag) == finished_message(
            tag, 1, Cancelled(reason=CancelReason.RUN_ENDING)
        )
        assert [stall.tag for stall in second.board.unproven()] == [tag]

    run_virtual(clock, main())


def test_a_run_whose_stop_is_already_in_the_log_ends_while_clear_is_unreachable() -> None:
    """The same, with the stop recorded before the process that must end the run started."""
    clock = VirtualClock()

    async def main() -> None:
        world = World(clock, fake_run_seed([1000.0], 3))
        world.behaving(hangs)
        first = world.start()
        await clock.sleep(1.0)
        first.log.push(stop_event(clock))
        await first.die()
        assert world.runner is not None
        with world.runner.unreachable():
            second = world.start()
            assert await second.rest() is RunRest.ENDED
        assert world.records.count("run_ended") == 1

    run_virtual(clock, main())


@pytest.mark.parametrize("standing", [core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING])
def test_a_seed_step_an_earlier_version_left_in_flight_is_cleared_before_it_runs_again(
    standing: core.RequestPhase,
) -> None:
    """The log has no attempt of the step, but its seed says it was sent: its work may exist.

    The loop cleared only before an attempt after the first, so the step started as attempt 1
    over whatever the earlier version left running. It is a re-run, and shows as one.
    """
    clock = VirtualClock()

    async def main() -> None:
        world = World(clock, fake_run_seed([100.0], 3, standing=standing))
        world.behaving(lambda *_: AttemptReturns(after_s=1.0))
        (tag,) = world.seed.tags
        process = world.start()
        assert await process.stopped_when_done() is RunRest.ENDED
        assert world.runner is not None
        assert [(c.kind, c.attempt) for c in world.runner.calls] == [
            (RunnerMethod.CLEAR, None),
            (RunnerMethod.RUN, 2),
        ]
        assert [m.attempt for m in world.fields(StepStarted)] == [2]
        result = world.result_of(tag)
        assert result is not None
        assert result.attempt == 2

    run_virtual(clock, main())


def test_a_seed_step_that_was_never_sent_starts_as_the_first_attempt_with_no_clear() -> None:
    clock = VirtualClock()

    async def main() -> None:
        world = World(clock, fake_run_seed([100.0], 3))
        world.behaving(lambda *_: AttemptReturns(after_s=1.0))
        process = world.start()
        assert await process.stopped_when_done() is RunRest.ENDED
        assert world.runner is not None
        assert [(c.kind, c.attempt) for c in world.runner.calls] == [(RunnerMethod.RUN, 1)]

    run_virtual(clock, main())


def test_a_job_that_finished_while_no_host_ran_keeps_its_own_result_over_the_exhaustion() -> None:
    """Two hosts died with the job running, past the step's retry bound; the job then finished.

    Core settles the step as exhausted. ``clear`` adopts the job with its result, and that
    result is the step's: the loop recorded the settlement it was handed instead.
    """
    clock = VirtualClock()

    async def main() -> None:
        world = World(clock, fake_run_seed([100.0], 1))
        world.behaving(lambda *_: AttemptReturns(after_s=10.0))
        (tag,) = world.seed.tags
        for _ in range(2):
            process = world.start()
            await clock.sleep(1.0)
            await process.die()
        await clock.sleep(20.0)
        final = world.start()
        assert await final.stopped_when_done() is RunRest.ENDED
        recorded = world.result_of(tag)
        assert recorded is not None
        assert recorded.result is not None
        assert recorded.result.field == "finished", "the adopted result was replaced"

    run_virtual(clock, main())


def test_a_pause_leaves_a_job_the_runner_adopts_running_and_the_resume_attaches_to_it() -> None:
    clock = VirtualClock()

    async def main() -> None:
        world = World(clock, fake_run_seed([100.0], 0))
        world.behaving(lambda *_: AttemptReturns(after_s=10.0))
        (tag,) = world.seed.tags
        first = world.start()
        await clock.sleep(1.0)
        first.log.push(pause_event(clock, "pause_now"))
        assert await first.rest() is RunRest.PARKED
        assert world.runner is not None
        assert world.runner.ended_by_cancel == (), "a pause cancelled the job"
        assert world.runner.running() == (tag,), "the pause left the job running"
        world.runner.process_died()
        second = world.start(resume_event(clock))
        assert await second.stopped_when_done() is RunRest.ENDED
        runs = [c.attempt for c in world.runner.calls if c.kind is RunnerMethod.RUN]
        assert runs == [1, 2]
        assert world.runner.ended_by_cancel == ()
        assert not world.fields(AttemptInterrupted)[1:], "one interruption, the pause"

    run_virtual(clock, main())


class Held:
    """A run with a step whose call ignores every cancel, drained by a pause with a deadline."""

    def __init__(self, clock: VirtualClock, other: str | None) -> None:
        self.clock = clock
        self.other = other
        limits = [60.0] if other is None else [60.0, 60.0]
        self.world = World(clock, fake_run_seed(limits, 0))
        self.held = self.world.seed.tags[0]

        def behave(_step: core.Request, tag: core.RequestId, attempt: int) -> Behavior:
            if tag == self.held:
                return AttemptHolds(for_s=500.0) if attempt == 1 else AttemptReturns(after_s=1.0)
            if other == "ends after the deadline":
                return AttemptReturns(after_s=3.0)
            return AttemptHangs()

        self.world.behaving(behave)

    async def paused(self) -> Process:
        """Pause with a drain of one second a second into the run, and let the loop rest."""
        process = self.world.start()
        await self.clock.sleep(1.0)
        process.log.push(pause_event(self.clock, "pause", drain_s=1.0))
        await process.rest()
        return process


@pytest.mark.parametrize("other", [None, "ends after the deadline", "hangs"])
def test_a_drain_that_runs_out_its_deadline_parks_in_the_log_whatever_else_happens(
    other: str | None,
) -> None:
    """The park fired only on a record, and the deadline passing writes none.

    Whether the held attempt was charged to the step then hung on an unrelated step ending after
    the deadline: its record parked the run, and a run parked at the exit spares its attempts.
    """
    clock = VirtualClock()

    async def main() -> None:
        held = Held(clock, other)
        process = await held.paused()
        assert process.loop.done()
        assert process.loop.result() is RunRest.PARKED
        assert process.log.state.run.status is core.RunStatus.PARKED, (
            "reported parked while draining"
        )
        assert held.world.runner is not None
        held.world.runner.process_died()
        relaunched = held.world.start()
        row = relaunched.log.state.steps.of(held.held)
        assert row is not None
        assert row.charged == 0, "the pause's own exit was charged to the step"
        await relaunched.die()

    run_virtual(clock, main())


def test_a_drain_with_a_held_call_and_a_flight_waiting_on_clear_still_parks() -> None:
    """One step's call ignores its cancel and another's flight waits on an unreachable ``clear``."""
    clock = VirtualClock()

    async def main() -> None:
        world = World(clock, fake_run_seed([60.0, 60.0], 3))
        held, stuck = world.seed.tags

        def behave(_step: core.Request, tag: core.RequestId, attempt: int) -> Behavior:
            return AttemptHolds(for_s=500.0) if tag == held and attempt == 2 else AttemptHangs()

        world.behaving(behave)
        first = world.start()
        await clock.sleep(1.0)
        await first.die()
        assert world.runner is not None
        with world.runner.unreachable(stuck):
            second = world.start()
            await clock.sleep(2.0)
            second.log.push(pause_event(clock, "pause", drain_s=1.0))
            assert await second.rest() is RunRest.PARKED
            assert second.log.state.run.status is core.RunStatus.PARKED

    run_virtual(clock, main())
