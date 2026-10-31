"""The runner loop over core's own rule: never stuck, one result per step, nothing left running.

The world is real core: open registered-operation steps with a retry bound, ``decide`` and ``fold``
from ``vs_core.api.recovery``, the confirmed log's records, and a pause (``pause_now``) and a stop entered through
core's own control events. Faults are generated: a runner that raises, hangs or ignores its cancel
signal, a backend that is unreachable, a pause or a stop at any time, and a host killed at any
boundary (every log append and every runner call) and restarted over the records it left. Named
scenarios pin the loop's individual rules.

Two things core has no record for yet are stood in for by ``FakeStepLog`` (the run's end, and an
operator resuming a parked run). Not generated, because core cannot express it yet: a pause that
drains (``pause``, not ``pause_now``) never reaches ``PARKED`` once its steps end, and a pause does
not record the deadline of its drain.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Cancelled,
    CancelReason,
    End,
    Failed,
    Interruption,
    RecordRefusal,
    Run,
    RunRest,
    finished_message,
    fold,
)
from vs_proto.api import (
    AttemptInterrupted,
    EpochStarted,
    InterruptionReason,
    StepFinished,
    StepStarted,
)
from vs_runtime.api.step_runner import (
    Adopted,
    CancelSignal,
    Cleared,
    OpenStep,
    StallBoard,
    StallKind,
    StepLoopTiming,
    StepLoopTimingError,
    StepRecord,
    run_step_loop,
)
from vs_runtime.api.testing import (
    AttemptHangs,
    AttemptHolds,
    AttemptRaises,
    AttemptReturns,
    FakeRunRecords,
    FakeRunSeed,
    FakeStepLog,
    FakeStepRunner,
    RunnerMethod,
    fake_run_seed,
)
from vs_sim.api.testing import (
    VirtualClock,
    examples,
    loop_threads,
    run_virtual,
    swept,
    wait_for_state,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_core.api.recovery import StepResult
    from vs_runtime.api.step_runner import StepLog
    from vs_runtime.api.testing import Behavior

type Message = EpochStarted | StepStarted | StepFinished | AttemptInterrupted

TIMING = StepLoopTiming(
    cancel_grace_s=2.0, clear_timeout_s=3.0, clear_backoff_s=1.0, clear_backoff_max_s=4.0
)
FINISHES = AttemptReturns(after_s=1.0)
RAISES = AttemptRaises(OSError("the host lost the process"))
HANGS = AttemptHangs()
DEAF = AttemptHangs(ignoring_signal=True)
UNCHARGED = (InterruptionReason.PAUSE, InterruptionReason.SHUTDOWN)


@dataclass(frozen=True)
class StepSpec:
    """A step the scenario opens: how long core lets it run, and what each attempt does."""

    limit_s: float
    behaviors: tuple[Behavior, ...]


def step(*behaviors: Behavior, limit_s: float = 30.0) -> StepSpec:
    return StepSpec(limit_s, behaviors)


@dataclass(frozen=True)
class Scenario:
    steps: tuple[StepSpec, ...]
    retries: int = 2
    adopts: bool = False
    clear_takes_s: float = 0.0
    unavailable: int = 0
    cancel_polls: int = 0
    """How many times a cancel leaves the work alive and its call raising, as a job still completing."""
    clear_holds: int = 0
    """How many calls of ``clear`` ignore cancellation for ``CLEAR_HOLD_S`` seconds."""
    clear_failures: int = 0
    """How many calls of ``clear`` raise an error that is not ``ClearUnavailableError``."""
    pause_at: float | None = None
    """When the operator pauses the run now (core's ``pause_now``)."""
    stop_at: float | None = None
    """When the operator stops the run; ``None`` waits until every step has its result."""
    race: bool = False
    """Whether a pause lands, and the run parks, just before the first result is recorded."""
    start_waits_s: tuple[float, ...] = ()
    """How long the start of each attempt waits behind the writer before the log stamps it, by
    attempt number; the last entry holds for the attempts after it."""


CLEAR_HOLD_S = 20.0
CLEAR_BUG = RuntimeError("a bug inside clear")


@dataclass
class World:
    scenario: Scenario
    seed: FakeRunSeed
    records: FakeRunRecords
    runner: FakeStepRunner
    watch: Watch
    boundaries: int = 0
    exits: list[RunRest] = field(default_factory=list)
    unproven: list[core.RequestId] = field(default_factory=list)
    """The steps an ending recorded without proof that their work ended."""
    running_at_park: list[str] = field(default_factory=list)
    """The attempts the log had running each time the loop returned with the run parked."""
    accepted: set[str] = field(default_factory=set)
    """The controls the operator sent and the log took."""


class HeldBoard(StallBoard):
    """A stall board that remembers every tag it showed held, for a run that parks over one.

    The board empties as the loop returns, and a parked run may still have an attempt open in the
    log for a call that ignored its cancel: this says which tags were held when it parked.
    """

    def __init__(self, held: set[str]) -> None:
        super().__init__()
        self._held = held

    def note(
        self, tag: core.RequestId, kind: StallKind, error: BaseException | None, now: float
    ) -> None:
        if kind.held:
            self._held.add(tag.root)
        super().note(tag, kind, error, now)


class Watch:
    """What the loop owes a tag it holds, checked at every boundary and before every record.

    A boundary is a log append, a runner call, and every start and end of a wait on the clock the
    loop and the runner share (``WatchedClock``), so a stall is seen while it lasts.
    """

    def __init__(self) -> None:
        self.held: set[str] = set()
        """Every tag the board showed held, in any process of the run."""
        self.board: StallBoard = HeldBoard(self.held)
        self.runner: FakeStepRunner | None = None
        self.problems: list[str] = []
        """Each way the loop acted on a tag it should have left alone, as it was seen."""
        self.shown: set[StallKind] = set()
        """Every kind of stall the board showed at some boundary."""
        self.errors: list[BaseException] = []
        """Every error the board showed with a stall."""

    def new_board(self) -> StallBoard:
        """The board of the next process: a restart starts with nothing stalled."""
        self.board = HeldBoard(self.held)
        return self.board

    def boundary(self) -> None:
        if self.runner is None:
            return
        for tag in self.runner.resisting():
            if not self.board.holds(tag):
                self.problems.append(f"{tag.root} resists a cancel and is not shown held")
        for stall in self.board.stalls():
            self.shown.add(stall.kind)
            if stall.error is not None:
                self.errors.append(stall.error)

    def before_record(self, what: str, tag: core.RequestId) -> None:
        """No record on a tag while a call on it is unfinished, nor a result over live work."""
        if self.runner is None:
            return
        if tag in self.runner.pending():
            self.problems.append(f"{what} on {tag.root} while a call of it is unfinished")
        unproven = any(stall.tag == tag for stall in self.board.unproven())
        if what == "step_finished" and tag in self.runner.running() and not unproven:
            self.problems.append(f"step_finished on {tag.root} over work still going")


class WatchedClock:
    """The virtual clock, with the watch run at each end of every wait."""

    def __init__(self, inner: VirtualClock, watch: Watch) -> None:
        self._inner = inner
        self._watch = watch

    def now(self) -> float:
        return self._inner.now()

    async def sleep(self, seconds: float) -> None:
        self._watch.boundary()
        await self._inner.sleep(seconds)
        self._watch.boundary()


class Switch:
    """Counts the loop's boundaries and kills the host at one of them."""

    def __init__(self, kill_at: int | None) -> None:
        self.ticks = 0
        self.kill_at = kill_at
        self.dead = False
        self.loop: asyncio.Task[RunRest] | None = None

    def tick(self) -> None:
        if self.dead:
            raise asyncio.CancelledError
        if self.ticks == self.kill_at:
            self.dead = True
            if self.loop is not None:
                self.loop.cancel()
            raise asyncio.CancelledError
        self.ticks += 1


@dataclass(frozen=True)
class LogFaults:
    """What goes wrong around the log's appends."""

    start_waits_s: tuple[float, ...] = ()
    """How long each attempt's start waits behind the writer, as ``Scenario.start_waits_s``."""
    before_result: Callable[[core.RequestId, int], Awaitable[None]] | None = None
    """Runs just before a result is appended: where a racing pause lands."""


class CrashingLog:
    def __init__(
        self, inner: StepLog, switch: Switch, watch: Watch, clock: WatchedClock, faults: LogFaults
    ) -> None:
        self._inner = inner
        self._switch = switch
        self._watch = watch
        self._clock = clock
        self._faults = faults

    @property
    def state(self) -> core.CoreState:
        return self._inner.state

    async def _around[T](self, write: Callable[[], Awaitable[T]]) -> T:
        self._watch.boundary()
        self._switch.tick()
        written = await write()
        self._watch.boundary()
        self._switch.tick()
        return written

    def open_step(self, tag: core.RequestId) -> OpenStep | None:
        return self._inner.open_step(tag)

    async def step_started(self, run: Run) -> Run | RecordRefusal:
        self._watch.before_record("step_started", run.tag)

        async def behind_the_writer() -> Run | RecordRefusal:
            waits = self._faults.start_waits_s
            wait = waits[min(run.attempt, len(waits)) - 1] if waits else 0.0
            if wait > 0:
                await self._clock.sleep(wait)
            return await self._inner.step_started(run)

        return await self._around(behind_the_writer)

    async def step_finished(
        self, tag: core.RequestId, attempt: int, result: StepResult
    ) -> StepRecord:
        self._watch.before_record("step_finished", tag)
        if self._faults.before_result is not None:
            await self._faults.before_result(tag, attempt)
        return await self._around(lambda: self._inner.step_finished(tag, attempt, result))

    async def attempt_interrupted(
        self, tag: core.RequestId, attempt: int, why: Interruption
    ) -> None:
        self._watch.before_record("attempt_interrupted", tag)
        await self._around(lambda: self._inner.attempt_interrupted(tag, attempt, why))

    async def run_ended(self, end: End) -> None:
        await self._around(lambda: self._inner.run_ended(end))

    async def clock_advanced(self, at: float) -> None:
        await self._around(lambda: self._inner.clock_advanced(at))

    async def next_input(self) -> None:
        await self._inner.next_input()

    def stalled(self) -> str | None:
        return self._inner.stalled()


class CrashingRunner:
    def __init__(self, inner: FakeStepRunner, switch: Switch, watch: Watch) -> None:
        self._inner = inner
        self._switch = switch
        self._watch = watch

    async def run(
        self,
        step: core.Request,
        tag: core.RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        self._watch.boundary()
        self._switch.tick()
        result = await self._inner.run(step, tag, attempt, deadline_at, cancel)
        self._watch.boundary()
        self._switch.tick()
        return result

    async def clear(self, step: core.Request, tag: core.RequestId) -> Adopted | Cleared:
        self._watch.boundary()
        self._switch.tick()
        outcome = await self._inner.clear(step, tag)
        self._watch.boundary()
        self._switch.tick()
        return outcome


def pause_event(clock: VirtualClock) -> core.RunControlEvent:
    control = core.ControlInput(
        control_id=core.ControlId(root="operator-pause"), action="pause_now"
    )
    return core.RunControlEvent(control=control, now_at=clock.now())


def stop_event(clock: VirtualClock) -> core.RunControlEvent:
    control = core.ControlInput(
        control_id=core.ControlId(root="operator-stop"),
        action="stop",
        mode=core.StopMode.CANCEL,
    )
    result = core.RunResultProposal(outcome="cancelled", reason="the operator stopped the run")
    return core.RunControlEvent(control=control, now_at=clock.now(), result=result)


async def operator(
    world: World, log: FakeStepLog, clock: VirtualClock, delivered: set[str]
) -> None:
    """The person at the controls: pause, then stop. What was delivered is in the log already.

    A stop that meets a pause waits for the resume, which is the next process: this operator
    ends and the next one tries again.
    """
    scenario = world.scenario
    if scenario.pause_at is not None and "pause" not in delivered:
        await clock.sleep(max(0.0, scenario.pause_at - clock.now()))
        delivered.add("pause")
        log.push(pause_event(clock))
        world.accepted.add("operator-pause")
    if "stop" in delivered:
        return
    if scenario.stop_at is not None:
        await clock.sleep(max(0.0, scenario.stop_at - clock.now()))
    else:
        every = len(scenario.steps)
        await wait_for_state(
            lambda: world.records.count("step_finished"),
            lambda n: n == every,
            world.records.changes,
        )
    if log.state.run.status in (core.RunStatus.PARKED, core.RunStatus.DRAINING):
        return
    log.push(stop_event(clock))
    world.accepted.add("operator-stop")
    delivered.add("stop")


def run_world(scenario: Scenario, kill_at: int | None = None) -> World:
    """Run the scenario until the run ends, killing the host once at boundary ``kill_at``."""
    clock = VirtualClock()
    seed = fake_run_seed([spec.limit_s for spec in scenario.steps], scenario.retries)
    specs = dict(zip(seed.tags, scenario.steps, strict=True))

    def behave(_step: core.Request, tag: core.RequestId, attempt: int) -> Behavior:
        behaviors = specs[tag].behaviors
        return behaviors[min(attempt, len(behaviors)) - 1]

    watch = Watch()
    runner = FakeStepRunner(
        WatchedClock(clock, watch),
        adopts=scenario.adopts,
        clear_takes_s=scenario.clear_takes_s,
        behave=behave,
        cancel_needs_polls=scenario.cancel_polls,
    )
    runner.clears_unavailable(scenario.unavailable)
    runner.clears_hold(CLEAR_HOLD_S, scenario.clear_holds)
    runner.clears_fail(CLEAR_BUG, scenario.clear_failures)
    watch.runner = runner
    world = World(scenario, seed, FakeRunRecords(), runner, watch)
    return run_virtual(clock, run_epochs(world, clock, kill_at))


def racing(
    scenario: Scenario, log: FakeStepLog, clock: VirtualClock, delivered: set[str]
) -> Callable[[core.RequestId, int], Awaitable[None]] | None:
    """The pause that lands between the loop's last look at the state and its append, once.

    The pause is appended and the attempt the result belongs to is interrupted, which parks the
    run: the loop's own check of the state, made before, said the run goes on.
    """
    if not scenario.race or "raced" in delivered:
        return None

    async def land(tag: core.RequestId, attempt: int) -> None:
        if "raced" in delivered:
            return
        delivered.add("raced")
        log.push(pause_event(clock))
        await log.attempt_interrupted(tag, attempt, Interruption.PAUSE)

    return land


async def run_epochs(world: World, clock: VirtualClock, kill_at: int | None) -> World:
    delivered: set[str] = set()
    parked = False
    killed = False
    watched = WatchedClock(clock, world.watch)
    while True:
        log = FakeStepLog(
            world.records, world.seed.state, world.seed.registry, clock, loop_threads()
        )
        if parked:
            resume = core.ControlInput(
                control_id=core.ControlId(root=f"resume-{len(world.exits)}"),
                action="resume",
                when="now",
            )
            log.push(core.RunControlEvent(control=resume, now_at=clock.now()))
            world.accepted.add(resume.control_id.root)
        switch = Switch(None if killed else kill_at)
        loop = asyncio.ensure_future(
            run_step_loop(
                CrashingLog(
                    log,
                    switch,
                    world.watch,
                    watched,
                    LogFaults(
                        world.scenario.start_waits_s, racing(world.scenario, log, clock, delivered)
                    ),
                ),
                CrashingRunner(world.runner, switch, world.watch),
                watched,
                TIMING,
                world.watch.new_board(),
            )
        )
        switch.loop = loop
        driver = asyncio.ensure_future(operator(world, log, clock, delivered))
        (outcome,) = await asyncio.gather(loop, return_exceptions=True)
        driver.cancel()
        (operated,) = await asyncio.gather(driver, return_exceptions=True)
        if isinstance(operated, Exception):
            raise operated
        world.boundaries += switch.ticks
        world.unproven.extend(stall.tag for stall in world.watch.board.unproven())
        if outcome is RunRest.PARKED:
            # A call that ignored its cancel is the one residual the loop shows and leaves to the
            # process exit, so the log may still have its attempt running. The next epoch ends it
            # uncharged (``epoch_opened``).
            world.running_at_park.extend(
                a.tag.root for a in log.state.steps.attempts if not a.ended
            )
        world.runner.process_died()
        killed = True
        if isinstance(outcome, asyncio.CancelledError):
            parked = False
            continue
        if isinstance(outcome, BaseException):
            raise outcome
        world.exits.append(outcome)
        if outcome is RunRest.ENDED:
            return world
        parked = True


def messages(world: World) -> list[Message]:
    """The core records of the log, as typed messages in order."""
    found: list[Message] = []
    for record in world.records.records:
        body = record.body
        if body is not None and isinstance(
            body.value, EpochStarted | StepStarted | StepFinished | AttemptInterrupted
        ):
            found.append(body.value)
    return found


def result_matches(world: World, index: int, expected: StepResult) -> bool:
    """Whether the ``index``th step's one recorded result is ``expected``."""
    tag = world.seed.tags[index]
    found = [m for m in messages(world) if isinstance(m, StepFinished) and m.tag == tag.root]
    return len(found) == 1 and found[0] == finished_message(tag, found[0].attempt, expected)


def was_finished(world: World, index: int) -> bool:
    """Whether the ``index``th step ended with the work's own report, not a decision of core."""
    tag = world.seed.tags[index].root
    return any(
        isinstance(m, StepFinished)
        and m.tag == tag
        and m.result is not None
        and m.result.field == "finished"
        for m in messages(world)
    )


def attempts_started(world: World, index: int) -> list[int]:
    tag = world.seed.tags[index].root
    return [m.attempt for m in messages(world) if isinstance(m, StepStarted) and m.tag == tag]


def interruption_reasons(world: World) -> list[InterruptionReason]:
    return [m.reason for m in messages(world) if isinstance(m, AttemptInterrupted)]


def check_log_of(world: World, tag: core.RequestId) -> None:
    """One step's records: attempts in order, each after an end of the last, one result."""
    last = 0
    over = True
    finished = False
    charged = 0
    for message in messages(world):
        if isinstance(message, EpochStarted):
            over = True
        elif message.tag != tag.root:
            continue
        else:
            assert not finished, f"{tag.root} has records after its result"
            if isinstance(message, StepStarted):
                assert message.attempt == last + 1, f"{tag.root} skipped an attempt"
                assert over, f"{tag.root} started attempt {message.attempt} over a running one"
                last, over = message.attempt, False
                charged += 1
            elif isinstance(message, AttemptInterrupted):
                assert message.attempt == last
                over = True
                charged -= int(message.reason in UNCHARGED)
            else:
                assert message.attempt == last, f"{tag.root} finished naming {message.attempt}"
                finished = True
    assert finished, f"{tag.root} has no result"
    assert charged <= world.scenario.retries + 1, f"{tag.root} ran past its retry bound"


def check_world(world: World) -> None:
    assert world.exits[-1] is RunRest.ENDED
    assert world.records.count("run_ended") <= 1, "the run's end was recorded twice"
    for tag in world.seed.tags:
        check_log_of(world, tag)
    check_clear_precedes_every_rerun(world)
    check_runs_carry_the_step_limit(world)
    assert set(world.runner.running()) <= set(world.unproven), "work was left running"
    assert world.runner.violations == ()
    assert world.watch.problems == []
    if cooperative(world.scenario):
        assert not world.running_at_park, "the run parked with an attempt the log has running"
    assert set(world.running_at_park) <= world.watch.held, "parked over an attempt no call holds"
    check_every_accepted_control_is_effective(world)


def cooperative(scenario: Scenario) -> bool:
    """Whether every attempt of the scenario ends when it is told to.

    A deaf or held attempt can outlive the park: the loop shows it and leaves it to the process
    exit, and the log still has it running. A parked run therefore holds an attempt only for a call
    the loop showed held (``check_world``), and the next epoch ends it uncharged.
    """
    return not any(
        isinstance(behavior, AttemptHolds)
        or (isinstance(behavior, AttemptHangs) and behavior.ignoring_signal)
        for spec in scenario.steps
        for behavior in spec.behaviors
    )


def check_every_accepted_control_is_effective(world: World) -> None:
    """A control the log took is in the run's controls once the log is folded: none is swallowed."""
    state = fold(world.records.read(), world.seed.state, registry=world.seed.registry)
    held = {control.control_id.root for control in state.run.controls}
    assert world.accepted <= held, f"controls taken and not effective: {world.accepted - held}"


def check_clear_precedes_every_rerun(world: World) -> None:
    cleared: dict[core.RequestId, bool] = {}
    for call in world.runner.calls:
        if call.kind is RunnerMethod.CLEAR:
            cleared[call.tag] = True
        else:
            assert call.attempt is not None
            assert call.attempt == 1 or cleared.get(call.tag), (
                f"attempt {call.attempt} had no clear"
            )
            cleared[call.tag] = False


def check_runs_carry_the_step_limit(world: World) -> None:
    """The limit a runner gets for an attempt is the one made durable in its ``StepStarted``."""
    recorded = {
        (m.tag, m.attempt): m.deadline_at for m in messages(world) if isinstance(m, StepStarted)
    }
    for call in world.runner.calls:
        key = (call.tag.root, call.attempt or 0)
        if call.kind is RunnerMethod.RUN and key in recorded:
            assert call.deadline_at == recorded[key]


def clear_calls(world: World) -> int:
    return sum(1 for call in world.runner.calls if call.kind is RunnerMethod.CLEAR)


@given(
    field=st.sampled_from(
        ["cancel_grace_s", "clear_timeout_s", "clear_backoff_s", "clear_backoff_max_s"]
    ),
    bad=st.sampled_from([0.0, -1.0, math.inf, math.nan]),
)
def test_a_bound_that_is_not_a_positive_finite_number_is_refused_by_name(
    field: str, bad: float
) -> None:
    bounds = {
        "cancel_grace_s": 1.0,
        "clear_timeout_s": 1.0,
        "clear_backoff_s": 1.0,
        "clear_backoff_max_s": 1.0,
    }
    with pytest.raises(StepLoopTimingError, match=field):
        StepLoopTiming(**{**bounds, field: bad})


def test_steps_that_finish_run_once_with_no_clear() -> None:
    world = run_world(Scenario((step(FINISHES), step(FINISHES))))
    check_world(world)
    assert attempts_started(world, 0) == [1]
    assert clear_calls(world) == 0
    assert all(was_finished(world, index) for index in range(2))


def test_a_runner_that_raises_is_cleared_and_run_again_charged_as_a_try() -> None:
    world = run_world(Scenario((step(RAISES, FINISHES),)))
    check_world(world)
    assert attempts_started(world, 0) == [1, 2]
    assert interruption_reasons(world) == [InterruptionReason.RAISED]


def test_a_step_that_raises_past_its_retry_bound_ends_as_failed() -> None:
    world = run_world(Scenario((step(RAISES),), retries=1))
    check_world(world)
    assert attempts_started(world, 0) == [1, 2]
    assert result_matches(world, 0, Failed(reason=core.FailureReason.ATTEMPTS_EXHAUSTED))


def test_a_step_past_its_limit_ends_cancelled_for_that_reason() -> None:
    world = run_world(Scenario((step(HANGS, limit_s=5.0),)))
    check_world(world)
    assert result_matches(world, 0, Cancelled(reason=CancelReason.DEADLINE))


def test_a_runner_that_ignores_its_cancel_signal_is_cancelled_cleared_and_ended() -> None:
    for adopts in (False, True):
        world = run_world(Scenario((step(DEAF, limit_s=5.0),), adopts=adopts))
        check_world(world)
        assert result_matches(world, 0, Cancelled(reason=CancelReason.DEADLINE))


def test_a_pause_now_interrupts_uncharged_parks_and_resume_runs_the_step_again() -> None:
    world = run_world(Scenario((step(HANGS, FINISHES),), pause_at=0.5, retries=0))
    check_world(world)
    assert world.exits == [RunRest.PARKED, RunRest.ENDED]
    assert attempts_started(world, 0) == [1, 2]
    assert interruption_reasons(world) == [InterruptionReason.PAUSE]
    assert was_finished(world, 0)


def test_a_runner_that_returns_late_from_a_pause_leaves_one_result_and_one_clear() -> None:
    world = run_world(Scenario((step(DEAF, FINISHES, limit_s=50.0),), pause_at=1.0))
    check_world(world)
    assert attempts_started(world, 0) == [1, 2]


def test_a_run_parks_over_an_open_attempt_only_for_a_call_that_ignored_its_cancel() -> None:
    """The log may park with the attempt of a deaf call running; the exit ends it, uncharged.

    With no tries to spare, the step still finishes after the resume: the pause is not a crash.
    """
    world = run_world(Scenario((step(DEAF, FINISHES, limit_s=50.0),), pause_at=1.0, retries=0))
    assert world.exits == [RunRest.PARKED, RunRest.ENDED]
    assert set(world.running_at_park) <= world.watch.held
    assert was_finished(world, 0), "the held attempt was charged as a crash"


def test_an_unreachable_backend_is_retried_and_the_step_then_runs_again() -> None:
    world = run_world(Scenario((step(RAISES, FINISHES),), unavailable=3))
    check_world(world)
    assert clear_calls(world) >= 4


def test_a_pause_does_not_wait_for_an_unreachable_backend() -> None:
    scenario = Scenario((step(RAISES, FINISHES),), unavailable=50, pause_at=0.5)
    world = run_world(scenario)
    assert world.exits[0] is RunRest.PARKED


def test_the_run_ends_even_when_the_backend_stays_unreachable() -> None:
    world = run_world(Scenario((step(RAISES, FINISHES),), unavailable=1000, stop_at=20.0))
    assert world.exits[-1] is RunRest.ENDED
    assert result_matches(world, 0, Cancelled(reason=CancelReason.RUN_ENDING))
    assert world.runner.running() != ()
    assert world.unproven == [world.seed.tags[0]]


def test_a_stop_cancels_what_runs_and_ends_the_run_once() -> None:
    world = run_world(Scenario((step(HANGS), step(DEAF), step(FINISHES)), stop_at=3.0))
    check_world(world)


def test_a_kill_at_every_boundary_of_a_one_step_run_still_ends_it_once() -> None:
    scenario = Scenario((step(RAISES, FINISHES),))
    for kill_at in range(run_world(scenario).boundaries):
        check_world(run_world(scenario, kill_at))


def test_a_kill_at_swept_boundaries_of_a_rich_run_ends_it_once() -> None:
    scenario = Scenario(
        (step(RAISES, FINISHES), step(HANGS, FINISHES), step(DEAF, FINISHES, limit_s=9.0)),
        adopts=True,
        unavailable=1,
        pause_at=2.0,
    )
    for kill_at in swept(run_world(scenario).boundaries, sample=8):
        check_world(run_world(scenario, kill_at))


def test_a_pause_that_lands_between_the_loops_look_and_its_append_parks_the_run_and_loses_nothing() -> (
    None
):
    world = run_world(Scenario((step(FINISHES), step(FINISHES)), race=True))
    check_world(world)
    assert world.exits == [RunRest.PARKED, RunRest.ENDED]


def test_a_pause_at_any_moment_parks_and_resumes_to_the_same_end() -> None:
    steps = (step(RAISES, FINISHES), step(HANGS, FINISHES), step(FINISHES))
    for pause_at in (moment / 2 for moment in range(20)):
        check_world(run_world(Scenario(steps, pause_at=pause_at)))


def test_a_job_still_running_after_its_cancel_is_not_recorded_cancelled_until_clear_proves_it_ended() -> (
    None
):
    scenario = Scenario((step(HANGS, limit_s=5.0),), adopts=True, cancel_polls=3)
    world = run_world(scenario)
    check_world(world)
    assert result_matches(world, 0, Cancelled(reason=CancelReason.DEADLINE))
    assert StallKind.STILL_RUNNING in world.watch.shown
    assert clear_calls(world) == scenario.cancel_polls, (
        "clear is asked after each cancel that proved nothing"
    )


def test_an_ending_waits_for_a_job_that_is_still_running_after_its_cancel() -> None:
    scenario = Scenario((step(HANGS),), adopts=True, cancel_polls=4, stop_at=3.0)
    world = run_world(scenario)
    check_world(world)
    assert result_matches(world, 0, Cancelled(reason=CancelReason.RUN_ENDING))
    assert StallKind.STILL_RUNNING in world.watch.shown
    assert world.unproven == []


def test_a_run_that_ignores_its_cancel_holds_its_tag_and_shows_it_until_it_returns() -> None:
    scenario = Scenario((step(AttemptHolds(for_s=30.0), FINISHES, limit_s=5.0),))
    world = run_world(scenario)
    check_world(world)
    assert StallKind.RUN_HELD in world.watch.shown
    assert result_matches(world, 0, Cancelled(reason=CancelReason.DEADLINE))


def test_a_clear_that_outlives_its_timeout_holds_its_tag_and_nothing_overlaps_it() -> None:
    world = run_world(Scenario((step(RAISES, FINISHES),), clear_holds=1))
    check_world(world)
    assert StallKind.CLEAR_HELD in world.watch.shown
    assert clear_calls(world) == 2, (
        "one clear after the raise and one before attempt 2, never overlapping"
    )
    assert was_finished(world, 0)


def test_an_ending_waits_for_a_held_tag_and_records_the_run_end_after_it_returns() -> None:
    world = run_world(Scenario((step(AttemptHolds(for_s=40.0)),), stop_at=3.0))
    check_world(world)
    assert StallKind.RUN_HELD in world.watch.shown
    assert result_matches(world, 0, Cancelled(reason=CancelReason.RUN_ENDING))


def test_a_pause_leaves_a_held_tag_to_the_process_exit_and_does_not_charge_it() -> None:
    """The pause ended the held attempt, not the step: with no tries to spare it still finishes."""
    scenario = Scenario((step(AttemptHolds(for_s=500.0), FINISHES),), pause_at=1.0, retries=0)
    world = run_world(scenario)
    assert world.exits == [RunRest.PARKED, RunRest.ENDED]
    assert attempts_started(world, 0) == [1, 2]
    assert was_finished(world, 0), "the held attempt was charged as a crash"
    assert world.runner.violations == ()
    assert world.watch.problems == []


def test_a_clear_that_raises_anything_is_retried_and_its_error_is_shown() -> None:
    world = run_world(Scenario((step(RAISES, FINISHES),), clear_failures=3))
    check_world(world)
    assert StallKind.CLEAR_FAILED in world.watch.shown
    assert CLEAR_BUG in world.watch.errors
    assert clear_calls(world) >= 4
    assert was_finished(world, 0)


def test_a_clear_that_keeps_raising_does_not_stop_the_run_ending() -> None:
    scenario = Scenario((step(RAISES, FINISHES),), clear_failures=1000, stop_at=20.0)
    world = run_world(scenario)
    assert world.exits[-1] is RunRest.ENDED
    assert result_matches(world, 0, Cancelled(reason=CancelReason.RUN_ENDING))
    assert world.unproven == [world.seed.tags[0]]
    assert CLEAR_BUG in world.watch.errors


def test_a_start_recorded_after_the_limit_passed_is_cancelled_without_waiting_for_a_wake() -> None:
    """A restarted attempt whose ``clear`` failed past the step's limit was never cancelled.

    The main task woke only for ends, inputs and future deadlines, so a deadline that passed
    before the start was recorded left it waiting on nothing: a deadlock, now a nudge.
    """
    steps = (step(HANGS, limit_s=6.0), step(AttemptReturns(after_s=0.0), limit_s=6.0))
    scenario = Scenario(steps, retries=1, clear_failures=3)
    world = run_world(scenario, kill_at=1)
    check_world(world)
    assert result_matches(world, 0, Cancelled(reason=CancelReason.DEADLINE))


def test_a_start_held_behind_the_writer_runs_with_the_limit_of_its_stamp() -> None:
    """The limit counts from the instant the log stamps the start, not from the decision.

    The writer holds the start for longer than the step's limit. Stamped with the decision's
    limit, the attempt would begin already past it and be cancelled before it ran.
    """
    world = run_world(Scenario((step(FINISHES, limit_s=6.0),), start_waits_s=(7.0,)))
    check_world(world)
    assert was_finished(world, 0)
    assert attempts_started(world, 0) == [1]


def test_a_retry_held_behind_the_writer_past_the_limit_is_refused_and_the_step_cancelled() -> None:
    """The first start fixed the limit, and it ran out while the retry waited for the writer.

    The log refuses the retry by type instead of raising, and the step ends at its limit.
    """
    scenario = Scenario((step(RAISES, FINISHES, limit_s=6.0),), start_waits_s=(0.0, 7.0))
    world = run_world(scenario)
    check_world(world)
    assert result_matches(world, 0, Cancelled(reason=CancelReason.DEADLINE))
    assert attempts_started(world, 0) == [1]


def test_a_pause_that_lands_while_a_start_waits_behind_the_writer_is_not_started_over() -> None:
    """The pause is in the log before the start would be: the start is refused, and the step
    runs once the run resumes."""
    scenario = Scenario((step(FINISHES),), start_waits_s=(2.0,), pause_at=2.0)
    world = run_world(scenario)
    check_world(world)
    assert world.exits[0] is RunRest.PARKED
    assert was_finished(world, 0)
    assert attempts_started(world, 0) == [1]


behaviors = st.one_of(
    st.builds(AttemptReturns, after_s=st.sampled_from([0.0, 1.0, 4.0])),
    st.just(RAISES),
    st.just(HANGS),
    st.just(DEAF),
    st.builds(AttemptHolds, for_s=st.sampled_from([3.0, 10.0, 25.0]), returns_anyway=st.booleans()),
)


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    steps = tuple(
        StepSpec(
            draw(st.sampled_from([6.0, 20.0, 60.0])),
            tuple(draw(st.lists(behaviors, min_size=1, max_size=3))),
        )
        for _ in range(draw(st.integers(1, 3)))
    )
    stop_at = draw(st.sampled_from([None, None, 3.0, 8.0]))
    return Scenario(
        steps,
        retries=draw(st.integers(1, 3)),
        adopts=draw(st.booleans()),
        clear_takes_s=draw(st.sampled_from([0.0, 0.5])),
        cancel_polls=draw(st.sampled_from([0, 0, 1, 3])),
        clear_holds=draw(st.sampled_from([0, 0, 1])),
        clear_failures=draw(st.sampled_from([0, 0, 1, 3])),
        unavailable=0 if stop_at is not None else draw(st.sampled_from([0, 0, 1, 2])),
        pause_at=draw(st.sampled_from([None, 0.0, 0.5, 1.0, 2.0, 3.5, 5.0, 8.0])),
        stop_at=stop_at,
        start_waits_s=draw(
            st.sampled_from([(), (), (0.5,), (2.0,), (7.0,), (0.0, 2.0), (0.0, 7.0)])
        ),
    )


@settings(max_examples=examples(pr=10, nightly=300))
@given(scenario=scenarios(), data=st.data())
def test_any_fault_schedule_with_a_kill_at_any_boundary_ends_with_every_step_finished_once(
    scenario: Scenario, data: st.DataObject
) -> None:
    clean = run_world(scenario)
    check_world(clean)
    kill_at = data.draw(st.integers(0, max(0, clean.boundaries - 1)))
    check_world(run_world(scenario, kill_at))
