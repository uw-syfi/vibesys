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
import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar, Literal

import pytest
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
    decide,
    finished_message,
    interrupted_message,
    started_message,
)
from vs_core.api.recovery import (
    fold as core_fold,
)
from vs_proto.api import (
    AttemptInterrupted,
    InterruptionReason,
    StepFinished,
    StepStarted,
    Timestamp,
)
from vs_runlog.api import Appended, ConfirmedLog
from vs_runtime.api.step_runner import (
    Interruption,
    LoopExit,
    OpenStep,
    StepLog,
    StepLoopTiming,
    StepLoopTimingError,
    run_step_loop,
)
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
from vs_sim.api.testing import Changes, VirtualClock, examples, run_virtual, swept, wait_for_state

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_proto.api import Record
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
        case LoggedFinished(tag, _attempt, result):
            return state.with_step(tag, running=False, result=result)
        case LoggedInterrupted(tag, _attempt, why):
            debit = 1 if why is Interruption.RAISED else 0
            return state.with_step(tag, running=False, debits=state.step(tag).debits + debit)
        case LoggedEnded():
            return replace(state, ended=True)
    message = f"unhandled entry {entry!r}"
    raise AssertionError(message)


def model_open_step(model: Model, tag: core.RequestId) -> OpenStep | None:
    step = model.step(tag)
    if step.result is not None:
        return None
    return OpenStep(a_step(tag), step.spec.deadline_at, step.attempts)


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


class _CrashingLog[S]:
    def __init__(self, inner: StepLog[S], switch: _Switch) -> None:
        self._inner = inner
        self._switch = switch

    @property
    def state(self) -> S:
        return self._inner.state

    async def _around(self, write: Callable[[], Awaitable[None]]) -> None:
        self._switch.tick()
        await write()
        self._switch.tick()

    def open_step(self, tag: core.RequestId) -> OpenStep | None:
        return self._inner.open_step(tag)

    async def step_started(self, run: Run) -> None:
        await self._around(lambda: self._inner.step_started(run))

    async def step_finished(self, tag: core.RequestId, attempt: int, result: StepResult) -> None:
        await self._around(lambda: self._inner.step_finished(tag, attempt, result))

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
        self,
        step: core.Request,
        tag: core.RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        self._switch.tick()
        result = await self._inner.run(step, tag, attempt, deadline_at, cancel)
        self._switch.tick()
        return result

    async def clear(self, step: core.Request, tag: core.RequestId) -> Adopted | Cleared:
        self._switch.tick()
        outcome = await self._inner.clear(step, tag)
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
        log = FakeStepLog(seed, fold, model_open_step, entries)
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
    check_runs_carry_the_step_deadline(outcome, scenario)
    assert outcome.runner.running() == (), "work was left running"
    assert outcome.runner.violations == ()


def concerns(entry: LogEntry | Input, tag: core.RequestId) -> bool:
    match entry:
        case LoggedStarted(run):
            return run.tag == tag
        case LoggedInterrupted(other, _, _) | LoggedFinished(other, _, _):
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
            case LoggedFinished(_, named, _):
                assert named == attempt, (
                    f"{spec.name} finished naming attempt {named}, not {attempt}"
                )
                finished = True
            case NewEpoch():
                ended_since_start = True


def check_runs_carry_the_step_deadline(outcome: Outcome, scenario: Scenario) -> None:
    deadlines = {spec.name: spec.deadline_at for spec in scenario.steps}
    for call in outcome.calls:
        if call.kind is RunnerMethod.RUN:
            assert call.deadline_at == deadlines[call.tag.root]


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


# --- The loop over core's own rule: real fold, real decide, real records -----------------------


class QueryOutcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class Query(core.OperationRequest):
    kind: Literal["test.query"] = "test.query"
    lifecycle: Literal[core.LifecycleClass.QUERY] = core.LifecycleClass.QUERY
    outcome_model: ClassVar[type[QueryOutcome]] = QueryOutcome
    value: int


def query_registry() -> core.OperationRegistry:
    descriptor = core.OperationDescriptor(
        kind="test.query",
        lifecycle=core.LifecycleClass.QUERY,
        request_schema=core.SchemaRef(name="query", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
    )
    return core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=Query, outcome_model=QueryOutcome
            ),
        )
    )


def state_with_open_steps(count: int, registry: core.OperationRegistry) -> core.CoreState:
    state = core.initial_state()
    state = state.model_copy(
        update={
            "registry": registry.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=registry.descriptors)}
            ),
        }
    )
    scope = core.Scope(owner=state.run.run_id, generation=0)
    for index in range(count):
        decision = registry.validate_decision(
            core.Operation(
                decision_id=core.DecisionId(root=f"d:{index}"),
                scope=scope,
                request=Query(value=index),
            )
        )
        state = core.step(
            state,
            core.DecisionSubmitted(
                decision=core.Wait(request=decision, until=core.Within(limit=100.0)),
                expected_revision=state.revision,
            ),
        ).state
    return state


class MemoryRecords:
    """The writer surface of the confirmed log, kept in memory: what survives a host death."""

    def __init__(self) -> None:
        self.records: list[Record] = []
        self.changes = Changes()

    def append(self, record: Record) -> None:
        self.records.append(record)

    def commit(self) -> None:
        self.changes.notify()

    def read(self) -> tuple[Record, ...]:
        return tuple(self.records)

    def count(self, field: str) -> int:
        return sum(1 for r in self.records if r.body is not None and r.body.field == field)

    def attempts_started(self) -> dict[str, list[int]]:
        by_tag: dict[str, list[int]] = {}
        for record in self.records:
            if record.body is not None and record.body.field == "step_started":
                message = record.body.value
                by_tag.setdefault(message.tag, []).append(message.attempt)
        return by_tag


OPEN_PHASES = (
    core.RequestPhase.PREPARED,
    core.RequestPhase.DISPATCHED,
    core.RequestPhase.RECONCILING,
)


class CoreStepLog:
    """A ``StepLog`` over core's records: each method appends one and folds it into the state."""

    def __init__(
        self,
        records: MemoryRecords,
        seed: core.CoreState,
        registry: core.OperationRegistry,
        clock: VirtualClock,
        epoch: int,
    ) -> None:
        self._log = ConfirmedLog(records)
        self._registry = registry
        self._clock = clock
        self._epoch = epoch
        folded = core_fold(records.read(), seed, registry=registry)
        steps = folded.steps.model_copy(update={"epoch": epoch})
        self._state = folded.model_copy(update={"steps": steps})

    @property
    def state(self) -> core.CoreState:
        return self._state

    def open_step(self, tag: core.RequestId) -> OpenStep | None:
        record = self._state.records.record(tag)
        if record is None or record.phase not in OPEN_PHASES:
            return None
        attempts = self._state.steps.of(tag)
        return OpenStep(
            record.request, record.until.at, 0 if attempts is None else attempts.started
        )

    async def step_started(self, run: Run) -> None:
        self._append(started_message(run))

    async def step_finished(self, tag: core.RequestId, attempt: int, result: StepResult) -> None:
        self._append(finished_message(tag, attempt, result))

    async def attempt_interrupted(
        self, tag: core.RequestId, attempt: int, why: Interruption
    ) -> None:
        assert why is Interruption.PAUSE, "core's records have no reason for a runner that raised"
        self._append(interrupted_message(tag, attempt, InterruptionReason.PAUSE))

    async def run_ended(self, end: End) -> None:
        raise NotImplementedError

    async def next_input(self) -> None:
        await asyncio.Event().wait()

    def _append(self, message: StepStarted | StepFinished | AttemptInterrupted) -> None:
        appended = self._log.append(
            message, epoch=self._epoch, at=Timestamp(), clock=self._clock.now()
        )
        assert isinstance(appended, Appended)
        self._state = core_fold([appended.record], self._state, registry=self._registry)


class CoreRule:
    def decide(self, state: core.CoreState, now: float) -> tuple[Action, ...]:
        return decide(state, now)

    def settled(self, state: core.CoreState) -> LoopExit | None:
        match state.run.status:
            case core.RunStatus.TERMINAL:
                return LoopExit.ENDED
            case core.RunStatus.PARKED:
                return LoopExit.PARKED
            case _:
                return None


def a_report(step: core.Request, tag: core.RequestId) -> Finished:
    return Finished(
        observation=core.Observation(
            event_id=core.EventId(root=f"{tag.root}-done"),
            request_id=tag,
            scope=step.scope,
            sequence=0,
            observed_at=0.0,
            status=core.ObservationStatus.SUCCEEDED,
            terminal=True,
            accepted=True,
            released=True,
            children_complete=True,
        )
    )


@dataclass
class CoreWorld:
    records: MemoryRecords
    runner: FakeStepRunner
    boundaries: int


def run_core_world(count: int, behaviors: tuple[Behavior, ...], kill_at: int | None) -> CoreWorld:
    """Run ``count`` open steps through the loop over core's rule until each has its result.

    Core ends a run only when a strategy proposes its result, and there is none here, so the
    loop idles once every step has finished and the host stops it.
    """
    clock = VirtualClock()
    registry = query_registry()
    seed = state_with_open_steps(count, registry)
    records = MemoryRecords()

    def behave(step: core.Request, tag: core.RequestId, attempt: int) -> Behavior:
        behavior = behaviors[min(attempt, len(behaviors)) - 1]
        if isinstance(behavior, AttemptReturns) and behavior.result is None:
            return AttemptReturns(result=a_report(step, tag), after_s=behavior.after_s)
        return behavior

    runner = FakeStepRunner(clock, behave=behave)

    async def main() -> int:
        epoch = 0
        while True:
            switch = _Switch(kill_at if epoch == 0 else None)
            log = CoreStepLog(records, seed, registry, clock, epoch + 1)
            loop = asyncio.ensure_future(
                run_step_loop(
                    _CrashingLog(log, switch),
                    CoreRule(),
                    _CrashingRunner(runner, switch),
                    clock,
                    TIMING,
                )
            )
            switch.loop = loop
            done = asyncio.ensure_future(
                wait_for_state(
                    lambda: records.count("step_finished"), lambda n: n == count, records.changes
                )
            )
            await asyncio.wait({loop, done}, return_when=asyncio.FIRST_COMPLETED)
            loop.cancel()
            done.cancel()
            outcomes = await asyncio.gather(loop, done, return_exceptions=True)
            raised = [o for o in outcomes if isinstance(o, Exception)]
            if raised:
                raise raised[0]
            epoch += 1
            if records.count("step_finished") == count:
                return switch.ticks

    return CoreWorld(records, runner, run_virtual(clock, main()))


def check_core_world(world: CoreWorld, count: int) -> None:
    assert world.records.count("step_finished") == count
    started = world.records.attempts_started()
    assert len(started) == count
    for attempts in started.values():
        assert attempts == list(range(1, len(attempts) + 1))
    check_clear_precedes_every_rerun(Outcome([], world.runner.calls, world.runner, 0, 0))
    assert world.runner.running() == ()
    assert world.runner.violations == ()


def test_core_open_steps_run_to_their_results_through_the_loop() -> None:
    world = run_core_world(3, (FINISHES,), None)
    check_core_world(world, 3)
    assert world.records.count("step_started") == 3


def test_a_step_past_its_limit_ends_cancelled_through_the_loop() -> None:
    world = run_core_world(2, (HANGS,), None)
    check_core_world(world, 2)


def test_every_boundary_of_a_run_over_core_rule_survives_a_kill() -> None:
    boundaries = run_core_world(2, (FINISHES,), None).boundaries
    for kill_at in swept(boundaries, sample=8):
        check_core_world(run_core_world(2, (FINISHES,), kill_at), 2)
