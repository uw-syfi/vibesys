"""The runner loop never gets stuck, records one result per step, and leaves nothing running.

Properties are checked over generated fault schedules (a runner that raises, hangs or ignores its
cancel signal; a backend that is unreachable; a pause or an ending at any time) with the host
killed at a generated boundary and restarted over what the log kept. Named scenarios pin the
loop's individual rules.

``Model`` is the smallest state with the shape the loop needs: steps with attempts, a pause, an
ending. ``ModelRule`` decides over it as ``docs/contributing/run-recovery.md`` says (run an open
step, cancel one past its limit, settle one whose attempts are spent, run nothing while paused).
``run_world`` drives the loop over a ``FakeStepRunner`` and a ``FakeStepLog``, kills the host at a
chosen boundary (every log append and runner call is one), restarts it over what the log kept,
resumes after each pause, and returns everything a test asserts on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Action,
    Cancel,
    Cancelled,
    CancelReason,
    End,
    Failed,
    Finished,
    Run,
    Settle,
    StepResult,
    Wait,
)
from vs_runtime.api.step_runner import Interruption, LoopExit, StepLoopTiming, run_step_loop
from vs_runtime.api.testing import (
    AttemptHangs,
    AttemptRaises,
    AttemptReturns,
    FakeStepLog,
    FakeStepRunner,
    LogEntry,
    LoggedEnded,
    LoggedFinished,
    LoggedInterrupted,
    LoggedStarted,
    RunnerCall,
    RunnerMethod,
)
from vs_sim.api.testing import VirtualClock, examples, run_virtual, swept

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_runtime.api.step_runner import Adopted, CancelSignal, Cleared
    from vs_runtime.api.testing import Behavior

TIMING = StepLoopTiming(
    cancel_grace_s=2.0, clear_timeout_s=3.0, clear_backoff_s=1.0, clear_backoff_max_s=4.0
)


@dataclass(frozen=True)
class Spec:
    """A step the scenario asks for."""

    name: str
    deadline_at: float
    max_attempts: int
    behaviors: tuple[Behavior, ...]

    @property
    def tag(self) -> core.RequestId:
        return core.RequestId(root=self.name)


@dataclass(frozen=True)
class StepState:
    spec: Spec
    attempts: int = 0
    running: bool = False
    debits: int = 0
    result: StepResult | None = None


@dataclass(frozen=True)
class Pause:
    drain_deadline: float
    now: bool


@dataclass(frozen=True)
class Resume:
    pass


@dataclass(frozen=True)
class EndRun:
    pass


@dataclass(frozen=True)
class NewEpoch:
    """A process started over this log: every attempt still marked running is dead."""


type Input = Pause | Resume | EndRun | NewEpoch


@dataclass(frozen=True)
class Model:
    steps: tuple[StepState, ...]
    paused: Pause | None = None
    ending: bool = False
    ended: bool = False

    def with_step(self, tag: core.RequestId, **changes: object) -> Model:
        return replace(
            self,
            steps=tuple(replace(s, **changes) if s.spec.tag == tag else s for s in self.steps),
        )

    def step(self, tag: core.RequestId) -> StepState:
        return next(s for s in self.steps if s.spec.tag == tag)


def fold(state: Model, entry: LogEntry | Input) -> Model:
    match entry:
        case LoggedStarted() | LoggedFinished() | LoggedInterrupted() | LoggedEnded():
            return _fold_log(state, entry)
        case NewEpoch():
            dead = tuple(
                replace(s, running=False, debits=s.debits + int(s.running)) for s in state.steps
            )
            return replace(state, steps=dead)
        case Pause():
            return replace(state, paused=entry)
        case Resume():
            return replace(state, paused=None)
        case EndRun():
            return replace(state, ending=True)
    message = f"unhandled entry {entry!r}"
    raise AssertionError(message)


def _fold_log(state: Model, entry: LogEntry) -> Model:
    match entry:
        case LoggedStarted(run):
            return state.with_step(run.tag, attempts=run.attempt, running=True)
        case LoggedFinished(tag, result):
            return state.with_step(tag, running=False, result=result)
        case LoggedInterrupted(tag, _attempt, why):
            debit = 1 if why is Interruption.RAISED else 0
            return state.with_step(tag, running=False, debits=state.step(tag).debits + debit)
        case LoggedEnded():
            return replace(state, ended=True)
    message = f"unhandled entry {entry!r}"
    raise AssertionError(message)


def a_step(tag: core.RequestId) -> core.Request:
    return core.ObserveOwnedJob(
        request_id=tag,
        scope=core.Scope(owner=core.RunId(root="run"), generation=0),
        resource_id=core.ResourceId(root="resource"),
    )


class ModelRule:
    def decide(self, state: Model, now: float) -> tuple[Action, ...]:
        actions: list[Action] = []
        wake: list[float] = []
        for step in state.steps:
            if step.result is None:
                actions.extend(self._for_open(state, step, now, wake))
        if all(s.result is not None for s in state.steps):
            actions.append(End(result=core.RunResultProposal(outcome="success", reason="done")))
        actions.append(Wait(until=min(wake, default=None)))
        return tuple(actions)

    def _for_open(
        self, state: Model, step: StepState, now: float, wake: list[float]
    ) -> list[Action]:
        tag = step.spec.tag
        drain = state.paused.drain_deadline if state.paused else None
        if step.running:
            reason = self._cancellation(state, step, now, drain)
            if reason is not None:
                return [Cancel(tag, reason)]
            wake.extend(t for t in (step.spec.deadline_at, drain) if t is not None)
            return []
        settled = self._settlement(state, step, now)
        if settled is not None:
            return [Settle(tag, settled)]
        if state.paused:
            return []
        return [Run(a_step(tag), tag, step.attempts + 1, step.spec.deadline_at)]

    @staticmethod
    def _settlement(state: Model, step: StepState, now: float) -> StepResult | None:
        if state.ending:
            return Cancelled(reason=CancelReason.RUN_ENDING)
        if step.debits >= step.spec.max_attempts:
            return Failed(reason=core.FailureReason.ATTEMPTS_EXHAUSTED)
        if now >= step.spec.deadline_at:
            return Cancelled(reason=CancelReason.DEADLINE)
        return None

    @staticmethod
    def _cancellation(
        state: Model, step: StepState, now: float, drain: float | None
    ) -> CancelReason | None:
        if state.ending:
            return CancelReason.RUN_ENDING
        if state.paused and state.paused.now:
            return CancelReason.PAUSE_NOW
        if drain is not None and now >= drain:
            return CancelReason.DRAIN_EXPIRED
        if now >= step.spec.deadline_at:
            return CancelReason.DEADLINE
        return None

    def settled(self, state: Model) -> LoopExit | None:
        if state.ended:
            return LoopExit.ENDED
        return LoopExit.PARKED if state.paused is not None else None


@dataclass(frozen=True)
class Scenario:
    steps: tuple[Spec, ...]
    adopts: bool = False
    clear_takes_s: float = 0.0
    unavailable: int = 0
    pause_at: float | None = None
    pause: Pause = Pause(drain_deadline=4.0, now=False)
    end_run_at: float | None = None


class _Switch:
    """Counts the loop's boundaries and kills the host at one of them."""

    def __init__(self, kill_at: int | None) -> None:
        self.ticks = 0
        self.kill_at = kill_at
        self.dead = False
        self.loop: asyncio.Task[LoopExit] | None = None

    def tick(self) -> None:
        if self.dead:
            raise asyncio.CancelledError
        if self.ticks == self.kill_at:
            self.dead = True
            if self.loop is not None:
                self.loop.cancel()
            raise asyncio.CancelledError
        self.ticks += 1


class _CrashingLog:
    def __init__(self, inner: FakeStepLog[Model, Input], switch: _Switch) -> None:
        self._inner = inner
        self._switch = switch

    @property
    def state(self) -> Model:
        return self._inner.state

    async def _around(self, write: Callable[[], Awaitable[None]]) -> None:
        self._switch.tick()
        await write()
        self._switch.tick()

    def latest_run(self, tag: core.RequestId) -> Run | None:
        return self._inner.latest_run(tag)

    async def step_started(self, run: Run) -> None:
        await self._around(lambda: self._inner.step_started(run))

    async def step_finished(self, tag: core.RequestId, result: StepResult) -> None:
        await self._around(lambda: self._inner.step_finished(tag, result))

    async def attempt_interrupted(
        self, tag: core.RequestId, attempt: int, why: Interruption
    ) -> None:
        await self._around(lambda: self._inner.attempt_interrupted(tag, attempt, why))

    async def run_ended(self, end: End) -> None:
        await self._around(lambda: self._inner.run_ended(end))

    async def next_input(self) -> None:
        await self._inner.next_input()


class _CrashingRunner:
    def __init__(self, inner: FakeStepRunner, switch: _Switch) -> None:
        self._inner = inner
        self._switch = switch

    async def run(
        self, step: core.Request, tag: core.RequestId, attempt: int, cancel: CancelSignal
    ) -> StepResult:
        self._switch.tick()
        result = await self._inner.run(step, tag, attempt, cancel)
        self._switch.tick()
        return result

    async def clear(self, tag: core.RequestId) -> Adopted | Cleared:
        self._switch.tick()
        outcome = await self._inner.clear(tag)
        self._switch.tick()
        return outcome


@dataclass
class Outcome:
    entries: list[LogEntry | Input]
    calls: tuple[RunnerCall, ...]
    runner: FakeStepRunner
    boundaries: int
    epochs: int
    exits: list[LoopExit] = field(default_factory=list)


def run_world(scenario: Scenario, kill_at: int | None = None) -> Outcome:
    """Run the scenario to the end of the run, killing the host once at boundary ``kill_at``."""
    clock = VirtualClock()
    scripts = {s.tag: s.behaviors for s in scenario.steps}

    def behave(_step: core.Request, tag: core.RequestId, attempt: int) -> Behavior:
        behaviors = scripts[tag]
        return behaviors[min(attempt, len(behaviors)) - 1]

    runner = FakeStepRunner(
        clock, adopts=scenario.adopts, clear_takes_s=scenario.clear_takes_s, behave=behave
    )
    runner.clears_unavailable(scenario.unavailable)
    seed = Model(tuple(StepState(s) for s in scenario.steps))
    return run_virtual(clock, _epochs(scenario, clock, runner, seed, kill_at))


async def _epochs(
    scenario: Scenario,
    clock: VirtualClock,
    runner: FakeStepRunner,
    seed: Model,
    kill_at: int | None,
) -> Outcome:
    entries: list[LogEntry | Input] = []
    inputs = _timeline(scenario)
    outcome = Outcome(entries, (), runner, 0, 0)
    while True:
        log = FakeStepLog(seed, fold, entries)
        log.push(NewEpoch())
        switch = _Switch(kill_at if outcome.epochs == 0 else None)
        loop = asyncio.ensure_future(
            run_step_loop(
                _CrashingLog(log, switch),
                ModelRule(),
                _CrashingRunner(runner, switch),
                clock,
                TIMING,
            )
        )
        switch.loop = loop
        driver = asyncio.ensure_future(_deliver(inputs, log, clock))
        (result,) = await asyncio.gather(loop, return_exceptions=True)
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)
        outcome.boundaries += switch.ticks
        outcome.epochs += 1
        entries = log.entries
        if isinstance(result, asyncio.CancelledError):
            continue
        if isinstance(result, BaseException):
            raise result
        outcome.exits.append(result)
        if result is LoopExit.ENDED:
            break
        entries.append(Resume())
    outcome.entries = entries
    outcome.calls = runner.calls
    return outcome


def _timeline(scenario: Scenario) -> list[tuple[float, Input, list[bool]]]:
    """The operator's inputs and when they arrive; the flag records that one was delivered."""
    inputs: list[tuple[float, Input, list[bool]]] = []
    if scenario.pause_at is not None:
        inputs.append((scenario.pause_at, scenario.pause, [False]))
    if scenario.end_run_at is not None:
        inputs.append((scenario.end_run_at, EndRun(), [False]))
    return sorted(inputs, key=lambda item: item[0])


async def _deliver(
    inputs: list[tuple[float, Input, list[bool]]],
    log: FakeStepLog[Model, Input],
    clock: VirtualClock,
) -> None:
    for at, entry, delivered in inputs:
        if delivered[0]:
            continue
        await clock.sleep(max(0.0, at - clock.now()))
        delivered[0] = True
        log.push(entry)


FINISHES = AttemptReturns(after_s=1.0)
RAISES = AttemptRaises(OSError("the host lost the process"))
HANGS = AttemptHangs()
DEAF = AttemptHangs(ignoring_signal=True)


def step(name: str, *behaviors: Behavior, deadline_at: float = 30.0, attempts: int = 3) -> Spec:
    return Spec(name, deadline_at, attempts, behaviors)


def entries_of[T](outcome: Outcome, kind: type[T]) -> list[T]:
    return [e for e in outcome.entries if isinstance(e, kind)]


def check_invariants(outcome: Outcome, scenario: Scenario) -> None:
    ended = entries_of(outcome, LoggedEnded)
    assert len(ended) == 1, "the run did not end exactly once"
    finished = [e.tag for e in entries_of(outcome, LoggedFinished)]
    assert sorted(t.root for t in finished) == sorted(s.name for s in scenario.steps)
    for spec in scenario.steps:
        check_attempts_follow_interruptions(outcome, spec)
    check_clear_precedes_every_rerun(outcome)
    assert outcome.runner.running() == (), "work was left running"
    assert outcome.runner.violations == ()


def concerns(entry: LogEntry | Input, tag: core.RequestId) -> bool:
    match entry:
        case LoggedStarted(run):
            return run.tag == tag
        case LoggedInterrupted(other, _, _) | LoggedFinished(other, _):
            return other == tag
        case NewEpoch():
            return True
        case _:
            return False


def check_attempts_follow_interruptions(outcome: Outcome, spec: Spec) -> None:
    attempt = 0
    ended_since_start = True
    finished = False
    for entry in (e for e in outcome.entries if concerns(e, spec.tag)):
        assert not finished or isinstance(entry, NewEpoch), (
            f"{spec.name} has entries after its result"
        )
        match entry:
            case LoggedStarted(run):
                assert run.attempt == attempt + 1, f"{spec.name} skipped an attempt"
                assert ended_since_start, f"{spec.name} started attempt {run.attempt} uninterrupted"
                attempt, ended_since_start = run.attempt, False
            case LoggedInterrupted(_, number, _):
                assert number == attempt
                ended_since_start = True
            case LoggedFinished():
                finished = True
            case NewEpoch():
                ended_since_start = True


def check_clear_precedes_every_rerun(outcome: Outcome) -> None:
    cleared_since_last_run: dict[str, bool] = {}
    for call in outcome.calls:
        name = call.tag.root
        if call.kind is RunnerMethod.CLEAR:
            cleared_since_last_run[name] = True
        elif call.kind is RunnerMethod.RUN:
            assert call.attempt is not None
            if call.attempt > 1:
                assert cleared_since_last_run.get(name), (
                    f"{name} attempt {call.attempt} had no clear"
                )
            cleared_since_last_run[name] = False


def attempts_started(outcome: Outcome, name: str) -> list[int]:
    return [e.run.attempt for e in entries_of(outcome, LoggedStarted) if e.run.tag.root == name]


def result_of(outcome: Outcome, name: str) -> object:
    (found,) = [e.result for e in entries_of(outcome, LoggedFinished) if e.tag.root == name]
    return found


def test_steps_that_finish_run_once_with_no_clear() -> None:
    scenario = Scenario((step("a", FINISHES), step("b", FINISHES)))
    outcome = run_world(scenario)
    check_invariants(outcome, scenario)
    assert attempts_started(outcome, "a") == [1]
    assert all(isinstance(result_of(outcome, n), Finished) for n in "ab")
    assert not [c for c in outcome.calls if c.kind is RunnerMethod.CLEAR]


def test_a_runner_that_raises_is_cleared_and_run_again_as_the_next_attempt() -> None:
    scenario = Scenario((step("a", RAISES, FINISHES),))
    outcome = run_world(scenario)
    check_invariants(outcome, scenario)
    assert attempts_started(outcome, "a") == [1, 2]
    (interrupted,) = entries_of(outcome, LoggedInterrupted)
    assert interrupted.why is Interruption.RAISED
    assert isinstance(result_of(outcome, "a"), Finished)


def test_a_step_whose_attempts_are_spent_ends_as_failed() -> None:
    scenario = Scenario((step("a", RAISES, attempts=2),))
    outcome = run_world(scenario)
    check_invariants(outcome, scenario)
    assert attempts_started(outcome, "a") == [1, 2]
    assert isinstance(result_of(outcome, "a"), Failed)


def test_a_step_past_its_deadline_ends_cancelled_for_that_reason() -> None:
    scenario = Scenario((step("a", HANGS, deadline_at=5.0),))
    outcome = run_world(scenario)
    check_invariants(outcome, scenario)
    assert result_of(outcome, "a") == Cancelled(reason=CancelReason.DEADLINE)


def test_a_runner_that_ignores_its_cancel_signal_is_cancelled_cleared_and_ended() -> None:
    for adopts in (False, True):
        scenario = Scenario((step("a", DEAF, deadline_at=5.0),), adopts=adopts)
        outcome = run_world(scenario)
        check_invariants(outcome, scenario)
        assert result_of(outcome, "a") == Cancelled(reason=CancelReason.DEADLINE)


def test_a_pause_drains_then_parks_and_resume_runs_the_interrupted_step_again() -> None:
    scenario = Scenario(
        (step("a", HANGS, FINISHES), step("b", FINISHES)), pause_at=0.5, end_run_at=None
    )
    outcome = run_world(scenario)
    check_invariants(outcome, scenario)
    assert outcome.exits == [LoopExit.PARKED, LoopExit.ENDED]
    assert attempts_started(outcome, "a") == [1, 2]
    (interrupted,) = entries_of(outcome, LoggedInterrupted)
    assert interrupted.why is Interruption.PAUSE
    assert isinstance(result_of(outcome, "a"), Finished)


def test_a_cancelled_attempt_and_a_late_return_leave_one_result() -> None:
    scenario = Scenario((step("a", DEAF, FINISHES, deadline_at=50.0),), pause_at=1.0)
    outcome = run_world(scenario)
    check_invariants(outcome, scenario)
    assert len(entries_of(outcome, LoggedFinished)) == 1


def test_an_unreachable_backend_is_retried_and_the_step_then_runs_again() -> None:
    scenario = Scenario((step("a", RAISES, FINISHES),), unavailable=3)
    outcome = run_world(scenario)
    check_invariants(outcome, scenario)
    assert [c.kind for c in outcome.calls].count(RunnerMethod.CLEAR) >= 4
    assert isinstance(result_of(outcome, "a"), Finished)


def test_a_pause_does_not_wait_for_an_unreachable_backend() -> None:
    scenario = Scenario((step("a", RAISES, FINISHES),), unavailable=50, pause_at=0.5)
    outcome = run_world(scenario)
    assert outcome.exits[0] is LoopExit.PARKED


def test_the_run_ends_even_when_the_backend_stays_unreachable() -> None:
    scenario = Scenario((step("a", RAISES, FINISHES),), unavailable=1000, end_run_at=20.0)
    outcome = run_world(scenario)
    assert entries_of(outcome, LoggedEnded)
    assert result_of(outcome, "a") == Cancelled(reason=CancelReason.RUN_ENDING)
    assert outcome.runner.running() != ()


def test_a_kill_at_any_boundary_of_a_one_step_run_still_ends_it_once() -> None:
    scenario = Scenario((step("a", RAISES, FINISHES),))
    for kill_at in range(run_world(scenario).boundaries):
        check_invariants(run_world(scenario, kill_at), scenario)


def test_a_pause_at_any_moment_parks_and_resumes_to_the_same_end() -> None:
    steps = (step("a", RAISES, FINISHES), step("b", HANGS, FINISHES), step("c", FINISHES))
    for pause_at in (moment / 2 for moment in range(20)):
        for now in (False, True):
            scenario = Scenario(steps, pause_at=pause_at, pause=Pause(pause_at + 1.0, now=now))
            check_invariants(run_world(scenario), scenario)


def test_every_boundary_of_a_rich_scenario_survives_a_kill() -> None:
    scenario = Scenario(
        (
            step("a", RAISES, FINISHES),
            step("b", HANGS, FINISHES),
            step("c", DEAF, FINISHES, deadline_at=9.0),
        ),
        adopts=True,
        unavailable=1,
        pause_at=2.0,
    )
    boundaries = run_world(scenario).boundaries
    for kill_at in swept(boundaries, sample=6):
        check_invariants(run_world(scenario, kill_at), scenario)


behaviors = st.one_of(
    st.builds(AttemptReturns, after_s=st.sampled_from([0.0, 1.0, 4.0])),
    st.just(RAISES),
    st.just(HANGS),
    st.just(DEAF),
)


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    count = draw(st.integers(1, 3))
    steps = tuple(
        Spec(
            f"s{i}",
            draw(st.sampled_from([6.0, 20.0, 60.0])),
            draw(st.integers(1, 3)),
            tuple(draw(st.lists(behaviors, min_size=1, max_size=3))),
        )
        for i in range(count)
    )
    ending = draw(st.sampled_from([None, 3.0, 8.0]))
    pause_at = draw(st.sampled_from([None, 0.0, 0.5, 1.0, 2.0, 3.5, 5.0, 8.0]))
    drain = draw(st.sampled_from([0.0, 1.0, 5.0]))
    return Scenario(
        steps,
        adopts=draw(st.booleans()),
        clear_takes_s=draw(st.sampled_from([0.0, 0.5])),
        unavailable=0 if ending is not None else draw(st.sampled_from([0, 0, 1, 2])),
        pause_at=pause_at,
        pause=Pause(drain_deadline=(pause_at or 0.0) + drain, now=draw(st.booleans())),
        end_run_at=ending,
    )


@settings(max_examples=examples(pr=10, nightly=300))
@given(scenario=scenarios(), data=st.data())
def test_any_fault_schedule_with_a_kill_at_any_boundary_ends_with_every_step_finished_once(
    scenario: Scenario, data: st.DataObject
) -> None:
    clean = run_world(scenario)
    check_invariants(clean, scenario)
    kill_at = data.draw(st.integers(0, max(0, clean.boundaries - 1)))
    check_invariants(run_world(scenario, kill_at), scenario)
