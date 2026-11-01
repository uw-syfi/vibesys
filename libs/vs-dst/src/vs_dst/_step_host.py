"""Hosts on the step loop: the run.log, the loop and a runner over the world, on a virtual clock.

This is the production loop of ``docs/contributing/run-recovery.md`` with only the far side
simulated. The log is ``RunLogStepLog`` over a run.log kept in memory, the loop is
``run_step_loop``, the search and the operator reach core through ``CoreInputs``, and the runner
performs each step by asking the world's executors. Nothing else decides what happens.

The world's effects are keyed by the step's tag, as the real runners' are: an effect that
finished and sealed its result is found again by the next attempt, and one that was in flight
when its host died left nothing and runs again.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vs_core.api import (
    ContractError,
    ControlId,
    ControlInput,
    ControlRefusedError,
    DispatchTurn,
    FlowCodeChanged,
    HostFence,
    HostId,
    ObservationStatus,
    ResumeSessionTurn,
    RunControlEvent,
    RunStatus,
    SubmitMeasurement,
    Until,
    control_refusal,
    requested_stop_mode,
)
from vs_core.api.recovery import Cancelled, Finished, RunRest, at_rest
from vs_core.api.testing import Journal, new_run
from vs_dst._effects import RecoverableExecutors
from vs_dst._host import FIRST_HOST_STARTS_AT, Hosted, WrongStoreError
from vs_dst._interruptions import Moment
from vs_dst._snapshots import RecordingRecords
from vs_dst._world import HoldingWorld
from vs_proto.api import EpochStarted, Timestamp
from vs_runlog.api import ConfirmedLog
from vs_runtime.api.core import ExecutionContext
from vs_runtime.api.step_router import LIVE_REQUEST_KINDS, NoRunnerError
from vs_runtime.api.step_runner import (
    Adopted,
    Cleared,
    CoreInputs,
    DispatchCapExceededError,
    RunLogStepLog,
    SearchRun,
    StallBoard,
    StepLoopStalledError,
    StepLoopTiming,
    run_step_loop,
)
from vs_runtime.api.testing import RecordingSearch, Run
from vs_sim.api.testing import VirtualClock, loop_threads, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreEvent, CoreState, Observation, Request, RequestId
    from vs_core.api.recovery import End, Interruption, OpenStep, RecordRefusal, StepResult
    from vs_core.api.recovery import Run as RunAction
    from vs_dst._effects import Effects
    from vs_dst._snapshots import OutsideWorld, Store, Watcher
    from vs_dst._world import World
    from vs_runtime.api.core import ExecutionResult
    from vs_runtime.api.step_runner import CancelSignal, StepRecord
    from vs_runtime.api.testing import HostOptions

MAX_STEPS = 2_000
"""How many steps one host starts before the run is called a cycle that never ends."""

TIMING = StepLoopTiming(
    cancel_grace_s=1.0, clear_timeout_s=5.0, clear_backoff_s=1.0, clear_backoff_max_s=8.0
)
"""The loop's bounds on a runner, in virtual seconds."""

QUOTA_POLL_S = 1.0
"""How often a turn the quota holds in its call looks whether the run is still held, in virtual seconds."""

RUN_FAILURES = (ContractError, FlowCodeChanged, RuntimeError, ValueError)
"""What a host that fails by its own contract raises: core or the fold refusing a record, a search
that cannot be restored from its log, the log refusing an append. Anything else is a defect in
the test, and propagates."""


class StepLoopHosting:
    """Hosts on the step loop over a run.log kept in memory."""

    def new_store(self) -> Store:
        """A run.log that tells its watcher about each record it takes."""
        return RecordingRecords()

    def run(
        self,
        outside: OutsideWorld,
        options: HostOptions,
        clock_at: float,
        watcher: Watcher | None = None,
        patience: float | None = None,
    ) -> Hosted:
        """A new process over the run.log, run until it ends the run, parks it or is given up on."""
        records = outside.store
        if not isinstance(records, RecordingRecords):
            raise WrongStoreError(records)
        host = _Host(outside, records, options)
        started_at = records.last_clock if records.records else clock_at
        clock = VirtualClock(started_at)
        run = run_virtual(clock, host.run(clock, watcher, patience))
        return Hosted(clock.now(), run, host.state)

    def restart_at(self, first_clock_at: float, store: Store) -> float:
        """The run clock continues from the last record: downtime is not run time."""
        if not isinstance(store, RecordingRecords):
            raise WrongStoreError(store)
        return max(first_clock_at, store.last_clock)


@dataclass
class _Counting:
    """The ``StepLog`` of the host, counting the steps it starts so a cycle is told from a run."""

    inner: CoreInputs
    started: int = 0

    @property
    def state(self) -> CoreState:
        return self.inner.state

    def open_step(self, tag: RequestId) -> OpenStep | None:
        return self.inner.open_step(tag)

    async def step_started(self, run: RunAction) -> RunAction | RecordRefusal:
        self.started += 1
        if self.started > MAX_STEPS:
            raise DispatchCapExceededError(MAX_STEPS, {run.step.kind: self.started})
        return await self.inner.step_started(run)

    async def step_finished(self, tag: RequestId, attempt: int, result: StepResult) -> StepRecord:
        return await self.inner.step_finished(tag, attempt, result)

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        await self.inner.attempt_interrupted(tag, attempt, why)

    async def clock_advanced(self, at: float) -> None:
        await self.inner.clock_advanced(at)

    async def run_ended(self, end: End) -> None:
        await self.inner.run_ended(end)

    async def next_input(self) -> None:
        await self.inner.next_input()

    def stalled(self) -> str | None:
        return self.inner.stalled()


class _Host:
    """One process over the outside world: its log, search, operator input and runner."""

    def __init__(
        self, outside: OutsideWorld, records: RecordingRecords, options: HostOptions
    ) -> None:
        self._outside = outside
        self._records = records
        self._options = options
        self._decisions: list[Any] = []
        self._journal = Journal()
        self._inputs: CoreInputs | None = None
        self._search: SearchRun[Any, Any] | None = None
        self.state: CoreState | None = None

    async def run(
        self, clock: VirtualClock, watcher: Watcher | None, patience: float | None
    ) -> Run | Exception:
        """Open the log, run the loop, and say how the process ended."""
        try:
            return await self._run(clock, watcher, patience)
        except RUN_FAILURES as failure:
            return failure

    async def _run(
        self, clock: VirtualClock, watcher: Watcher | None, patience: float | None
    ) -> Run:
        world = self._outside.world
        setup = world.setup
        resuming = self._options.resumed and setup.upgrade is not None
        launch = setup.upgrade() if resuming and setup.upgrade is not None else setup.new_launch()
        registry = setup.harness.registry
        driver = launch.search.driver(launch.config, registry)
        search = RecordingSearch(driver, self._decisions)
        handle = _HostHandle(clock, self._decisions)
        if watcher is not None:
            watcher.watch(self._records, handle)
        confirmed = ConfirmedLog(self._records, loop_threads())
        epoch = (self._records.records[-1].epoch if self._records.records else 0) + 1
        confirmed.append(EpochStarted(epoch=epoch), epoch=epoch, at=Timestamp(), clock=clock.now())
        run_search = SearchRun.restored(confirmed.records(), search, launch.config, registry)
        self._search = run_search
        log = RunLogStepLog.open(
            confirmed,
            epoch=epoch,
            seed=new_run(driver, setup.harness),
            registry=registry,
            clock=clock,
            search=run_search,
        )
        inputs = CoreInputs(log, run_search, clock)
        self._inputs = inputs
        self.state = log.state
        handle.inputs = inputs
        self._resume(log, epoch, clock)
        runner = _WorldRunner(
            _executors(self._outside, self._journal, lambda: log.state, watcher, handle),
            self._outside.effects,
            clock,
            epoch,
            holds=_holds_of(world),
        )
        loop = asyncio.ensure_future(
            run_step_loop(_Counting(inputs), runner, clock, TIMING, StallBoard())
        )
        halted: DispatchCapExceededError | StepLoopStalledError | None = None
        try:
            await self._until(loop, clock, patience)
        except (DispatchCapExceededError, StepLoopStalledError) as stopped:
            halted = stopped
        finally:
            self.state = log.state
            inputs.leave()
            await handle.close()
        return self._result(inputs, halted=halted)

    def _resume(self, log: RunLogStepLog, epoch: int, clock: VirtualClock) -> None:
        """The operator resumes a run that stood paused, or was stopped, when its last process ended.

        The first Resume is always taken: a refusal fails the host, so a run that needs a second
        Resume is an error and not a quiet retry. A stop the log holds is lifted only when core
        takes the Resume (a run whose ending has begun may not), as the control bridge of a
        launched process does.
        """
        state = log.state
        paused = at_rest(state) is RunRest.PARKED or state.run.status in (
            RunStatus.PAUSED,
            RunStatus.DRAINING,
        )
        control = ControlInput(
            control_id=ControlId(root=f"operator-resume:{epoch}"), action="resume", when="now"
        )
        stopped = (
            requested_stop_mode(state.run) is not None and control_refusal(state, control) is None
        )
        if not (self._options.resumed and (paused or stopped)):
            return
        log.input(RunControlEvent(control=control, now_at=clock.now()))

    @staticmethod
    async def _until(
        loop: asyncio.Future[RunRest], clock: VirtualClock, patience: float | None
    ) -> None:
        """Wait for the loop, or give up on it after ``patience`` virtual seconds."""
        waits: set[asyncio.Future[Any]] = {loop}
        if patience is not None:
            waits.add(asyncio.ensure_future(clock.sleep(patience)))
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for wait in waits - {loop}:
            wait.cancel()
        if not loop.done():
            loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        if not loop.cancelled() and (failure := loop.exception()) is not None:
            raise failure

    def _result(
        self,
        inputs: CoreInputs,
        *,
        halted: DispatchCapExceededError | StepLoopStalledError | None = None,
    ) -> Run:
        progress = None if self._search is None else self._search.progress
        if progress is None:
            raise SearchNeverAdvancedError
        return Run(
            core=inputs.state,
            state=progress.state,
            decisions=self._decisions,
            journal=self._journal,
            halted=halted,
        )


class SearchNeverAdvancedError(RuntimeError):
    """A host reported a run whose search never advanced: its log was opened without hearing it."""

    def __init__(self) -> None:
        """Say what happened."""
        super().__init__("the host's search has no progress, though its log is open")


class _HostHandle:
    """What an interruption needs of this host: its clock, its decisions, an input.

    The records are watched from the host's first append, before its inputs exist, so the inputs
    are bound once they do; an input before then has nowhere to go and is a defect.
    """

    def __init__(self, clock: VirtualClock, decisions: list[Any]) -> None:
        self._clock = clock
        self._decisions = decisions
        self.inputs: CoreInputs | None = None
        self._timers: list[asyncio.Future[None]] = []
        self.held_until = -math.inf
        """The virtual time until which the provider's quota holds every turn in its call."""

    def hold_turns(self, seconds: float) -> None:
        self.held_until = self._clock.now() + seconds

    def submit_after(self, seconds: float, event: Callable[[float], CoreEvent]) -> None:
        self._timers.append(asyncio.ensure_future(self._later(seconds, event)))

    async def _later(self, seconds: float, event: Callable[[float], CoreEvent]) -> None:
        await self._clock.sleep(seconds)
        try:
            self.submit(event(self._clock.now()))
        except ControlRefusedError:
            return  # the run was resumed before the timer fired: there is nothing left to lift

    async def close(self) -> None:
        """End the timers the process owned: they die with it."""
        for timer in self._timers:
            timer.cancel()
        await asyncio.gather(*self._timers, return_exceptions=True)

    @property
    def clock(self) -> VirtualClock:
        return self._clock

    @property
    def decisions(self) -> list[Any]:
        return self._decisions

    def submit(self, event: CoreEvent) -> None:
        if self.inputs is None:
            raise HostNotOpenError
        self.inputs.offer(event)


class HostNotOpenError(RuntimeError):
    """An input was offered to a host whose log is not open yet."""

    def __init__(self) -> None:
        """Say what happened."""
        super().__init__("the host has no inputs before its run.log is open")


class _WorldRunner:
    """Runs each step through the world's executors, which seal what a step's effect did.

    A step whose effect finished is found by its tag, as a keyed write or a job's ledger entry
    is: ``clear`` adopts the sealed result and ``run`` returns it, so the effect never runs
    twice. A step whose effect began and did not finish left nothing, and runs again.
    """

    def __init__(
        self,
        executors: RecoverableExecutors,
        effects: Effects,
        clock: VirtualClock,
        epoch: int,
        *,
        holds: Callable[[Request], bool],
    ) -> None:
        self._executors = executors
        self._effects = effects
        self._clock = clock
        self._holds = holds
        self._fence = HostFence(host_id=HostId(root="dst"), epoch=epoch)

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Perform the step, or return what its earlier attempt sealed.

        A kind the production router has no runner for is refused as it refuses it
        (``NoRunnerError``), so a world cannot serve a step the real loop would stall on.
        """
        del attempt
        if type(step) not in LIVE_REQUEST_KINDS:
            raise NoRunnerError(type(step).__name__)
        if (sealed := self._sealed(step, tag)) is not None:
            return sealed
        if self._holds(step):
            # The job is queued and nothing starts it: it began, ends by no one's doing, and
            # seals nothing, so only the loop's cancel ends the step.
            self._executors.begin(tag.root)
            return Cancelled(reason=await cancel.wait())
        context = ExecutionContext(
            fence=self._fence,
            now_at=self._clock.now(),
            payload_digest=tag.root,
            until=Until(at=deadline_at),
        )
        effect = asyncio.ensure_future(self._executors.execute(step, context))
        cancelled = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({effect, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancelled.cancel()
        if effect.done():
            return _result_of(step, tag, effect.result())
        effect.cancel()
        await asyncio.gather(effect, return_exceptions=True)
        return Cancelled(reason=await cancel.wait())

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """Adopt the result the step's effect sealed, or find nothing."""
        sealed = self._sealed(step, tag)
        return Cleared() if sealed is None else Adopted(sealed)

    def _sealed(self, step: Request, tag: RequestId) -> Finished | None:
        """The result the step's effect sealed; None when it sealed none that ends the step.

        An effect whose answer was not final left nothing to adopt, as the executors of the
        production runners leave nothing when they cannot conclude: the attempt raised, it is
        charged, and the step runs again until its retries are spent.
        """
        sealed = self._effects.sealed.get(tag.root)
        return None if sealed is None else _final_result(step, sealed)


class StepWithoutResultError(Exception):
    """The world's executor could not conclude the step: the attempt has no result."""

    def __init__(self, tag: RequestId, observation: Observation) -> None:
        """Name the step and what the executor observed."""
        super().__init__(
            f"attempt of {tag.root} ended without a result: {observation.status.value}"
        )


def _result_of(step: Request, tag: RequestId, result: ExecutionResult) -> Finished:
    """The step's result: what the executor observed, which must be final.

    An observation that is not final is an attempt with no result (``StepWithoutResultError``).
    """
    if (finished := _final_result(step, result)) is None:
        raise StepWithoutResultError(tag, result.observation.observation)
    return finished


def _final_result(step: Request, result: ExecutionResult) -> Finished | None:
    """What the executor's answer ends the step with; None when the answer is not final.

    A measurement submission is the one step the world answers with a job still running: the
    job runner of a step follows its job to the end, so the step ends with the job accepted and
    ended, and core polls for the evidence as it does for a job it owns. Any other observation
    that is not final is an attempt with no result, which the loop runs again.
    """
    observed = result.observation
    view = observed.observation
    if view.terminal:
        return Finished(observation=view, observed=observed, owner_events=result.owner_events)
    if (
        isinstance(step, SubmitMeasurement)
        and view.accepted
        and view.status is ObservationStatus.PENDING
    ):
        ended = view.model_copy(
            update={"status": ObservationStatus.SUCCEEDED, "terminal": True, "released": True}
        )
        return Finished(
            observation=ended, observed=observed.model_copy(update={"observation": ended})
        )
    return None


def _holds_of(world: World) -> Callable[[Request], bool]:
    """What the world holds forever: nothing, unless it says so (`HoldingWorld`)."""
    return world.holds if isinstance(world, HoldingWorld) else lambda _request: False


def _executors(
    outside: OutsideWorld,
    journal: Journal,
    core: Callable[[], CoreState],
    watcher: Watcher | None,
    handle: _HostHandle,
) -> RecoverableExecutors:
    """The world's executors for this host, hearing each effect as it begins.

    A turn the provider's quota stops waits in its call, begun and sealing nothing, until the
    quota lifts: the capacity gate of the real executors.
    """
    world = outside.world
    setup = world.setup

    class HostExecutors(RecoverableExecutors):
        def _began(self, index: int) -> None:
            if watcher is not None:
                watcher.reached(Moment.IN_FLIGHT, index)

        async def _enacted(self, request: Request, result: ExecutionResult) -> ExecutionResult:
            if isinstance(request, DispatchTurn | ResumeSessionTurn):
                while handle.clock.now() < handle.held_until:
                    await handle.clock.sleep(QUOTA_POLL_S)
            return result

    return HostExecutors(
        world, core, setup.harness.registry, setup.schemas, journal, outside.effects
    )


__all__ = ["FIRST_HOST_STARTS_AT", "MAX_STEPS", "StepLoopHosting"]
