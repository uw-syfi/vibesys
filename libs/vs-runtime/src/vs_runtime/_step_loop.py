"""The runner loop: executes core's actions through a ``StepRunner`` and records what happened.

``decide`` owns every decision, including that a deadline passed. The loop owns tasks, bounds
and the log: a step's ``StepStarted`` is durable before any of its I/O, ``clear`` runs before
every attempt after the first, a late return from an attempt the loop gave up on is dropped,
and no await on a runner is unbounded. The loop never looks at what kind of step it runs.

Each running attempt is a flight, one task that owns the attempt from ``clear`` to the record
of how it ended. The main task only reads state, hands actions to flights and waits.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, assert_never

from vs_core.api.recovery import (
    Cancel,
    Cancelled,
    CancelReason,
    End,
    Interruption,
    OpenStep,
    Run,
    RunRest,
    Settle,
    Wait,
    at_rest,
    decide,
    interruption_of,
)
from vs_runtime._step_runner import Adopted, CancelSignal, Cleared, ClearUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Coroutine

    from vs_core.api import CoreState, Request, RequestId
    from vs_core.api.recovery import Action, StepResult
    from vs_runtime._step_runner import StepRunner
    from vs_sim.api import SleepingClock


@dataclass(frozen=True)
class Recorded:
    """The result of a step is in the log."""


@dataclass(frozen=True)
class Interrupted:
    """The run was parked when the result reached the log, so it was not recorded as one.

    A running attempt is recorded as ``AttemptInterrupted(PAUSE)`` instead and its step runs
    again after the resume: core drops the result of a step in a parked epoch. The log decides
    this under the lock that orders its appends, so a pause that lands after the loop last
    read the state is still judged.
    """


type StepRecord = Recorded | Interrupted


class StepLog(Protocol):
    """The run's log as the loop uses it: each method is one atomic durable append.

    ``state`` is the fold of everything appended, by the loop or by an input from elsewhere,
    and is current as soon as an append returns.
    """

    @property
    def state(self) -> CoreState:
        """The state to decide on."""
        ...

    def open_step(self, tag: RequestId) -> OpenStep | None:
        """The step ``tag`` as the fold has it while it has no result; ``None`` otherwise."""
        ...

    async def step_started(self, run: Run) -> StepRecord:
        """Record that an attempt is about to start, before any I/O of it.

        ``Interrupted`` when the run no longer lets the step start (a pause, an ending or the
        step's limit came while the loop waited on a ``clear``): nothing is recorded and the
        attempt must not run.
        """
        ...

    async def step_finished(self, tag: RequestId, attempt: int, result: StepResult) -> StepRecord:
        """Record the one result of a step and the attempt that produced it (0 if none ran).

        ``Interrupted`` when the run is parked: see there.
        """
        ...

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        """Record that an attempt ended and its step did not.

        ``why`` says whether the attempt is charged to the step's retry bound
        (``Interruption.charged``).
        """
        ...

    async def run_ended(self, end: End) -> None:
        """Record the end of the run."""
        ...

    async def next_input(self) -> None:
        """Return once an input has been appended since this last returned."""
        ...

    def stalled(self) -> str | None:
        """Why nothing but a runner can move the run, or None while something still can.

        The loop asks when no flight runs and no timer is set. Anything that can still append
        an input (the operator, a wake-up of the search, a clock the log ticks) makes the run
        not stalled. A run that waits on nothing is reported, never left silent.
        """
        ...


class StepLoopStalledError(RuntimeError):
    """The run waits on nothing: no runner is in flight, no timer is set and no input can come."""

    def __init__(self, why: str) -> None:
        """Say what the run waits on."""
        super().__init__(why)


class StepLoopTimingError(ValueError):
    """A bound of the loop is not a positive finite number of seconds."""

    def __init__(self, name: str, value: float) -> None:
        """Name the rejected bound."""
        super().__init__(f"{name} must be a positive finite number of seconds, got {value!r}")


@dataclass(frozen=True)
class StepLoopTiming:
    """Bounds on the loop's waits for a runner, in seconds on the loop's clock."""

    cancel_grace_s: float
    """How long a cancelled attempt may take to return before its task is cancelled."""
    clear_timeout_s: float
    """How long one ``clear`` may take before it counts as unreachable."""
    clear_backoff_s: float
    """The wait after the first unreachable ``clear``; it doubles."""
    clear_backoff_max_s: float
    """The longest wait between two ``clear`` calls."""

    def __post_init__(self) -> None:
        """Reject a bound that is not a positive finite number, naming it."""
        for name, value in vars(self).items():
            if not (math.isfinite(value) and value > 0):
                raise StepLoopTimingError(name, value)


class _Flight:
    """One attempt, or one conclusion of a step with no attempt running."""

    def __init__(
        self, tag: RequestId, step: Request, deadline_at: float | None, *, ending: bool = False
    ) -> None:
        self.tag = tag
        self.step = step
        self.deadline_at = deadline_at
        self.signal = CancelSignal()
        self.ending = ending
        self.started = False
        self.parked = False
        self.changed = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def abandons_clear(self) -> bool:
        """Whether an unreachable backend ends the wait for ``clear``."""
        reason = self.signal.reason
        interrupts = reason is not None and interruption_of(reason) is not None
        return self.ending or self.parked or interrupts


async def run_step_loop(
    log: StepLog, runner: StepRunner, clock: SleepingClock, timing: StepLoopTiming
) -> RunRest:
    """Execute the actions ``vs_core.api.recovery.decide`` asks for until the run ends or parks.

    ``clock`` is the run clock, the one ``decide`` reads and the log's records carry: it
    excludes the time the run was not running. Cancelling this call, or an exception out of
    it, is a crash: running attempts are abandoned with no record, and the next loop over
    the same log runs them again.
    """
    return await _StepLoop(log, runner, clock, timing).run()


class _StepLoop:
    def __init__(
        self,
        log: StepLog,
        runner: StepRunner,
        clock: SleepingClock,
        timing: StepLoopTiming,
    ) -> None:
        self._log = log
        self._runner = runner
        self._clock = clock
        self._timing = timing
        self._flights: dict[RequestId, _Flight] = {}
        self._appending = asyncio.Lock()
        self._input: asyncio.Task[None] | None = None

    async def run(self) -> RunRest:
        try:
            while True:
                exit_ = await self._turn()
                if exit_ is not None:
                    return exit_
        finally:
            await self._abandon_flights()

    async def _turn(self) -> RunRest | None:
        rest = at_rest(self._log.state)
        if rest is RunRest.ENDED:
            return RunRest.ENDED
        actions = decide(self._log.state, self._clock.now())
        if rest is RunRest.PARKED:
            return await self._park(actions)
        end = next((a for a in actions if isinstance(a, End)), None)
        if end is not None and not self._flights:
            await self._record_ended(end)
            return RunRest.ENDED
        await self._wake(self._dispatch(actions))
        return None

    async def _park(self, actions: tuple[Action, ...]) -> RunRest | None:
        """A parked run starts nothing and takes no result: it only stops what still runs.

        Core drops a result in a parked epoch, so a ``Settle`` it still decides (a step
        past its limit) waits for the resume instead of being dispatched over and over.
        """
        stops = tuple(a for a in actions if isinstance(a, Cancel) and a.tag in self._flights)
        self._dispatch(stops)
        self._release_unstarted()
        if not self._flights:
            return RunRest.PARKED
        await self._wake(None)
        return None

    def _dispatch(self, actions: tuple[Action, ...]) -> float | None:
        """Hand each action to its flight; return the time ``Wait`` asked to be woken at."""
        wake_at: float | None = None
        for action in actions:
            match action:
                case Run():
                    self._start(action)
                case Cancel():
                    self._cancel(action)
                case Settle():
                    self._settle(action)
                case Wait(until=until):
                    wake_at = until
                case End():
                    pass
                case _:
                    assert_never(action)
        return wake_at

    def _release_unstarted(self) -> None:
        """A parked run starts nothing, so flights still waiting on ``clear`` stop waiting."""
        for flight in self._flights.values():
            if not flight.started and not flight.parked:
                flight.parked = True
                flight.changed.set()

    def _start(self, run: Run) -> None:
        if run.tag not in self._flights:
            flight = _Flight(run.tag, run.step, run.deadline_at)
            self._launch(flight, self._attempt(run, flight))

    def _cancel(self, action: Cancel) -> None:
        flight = self._flights.get(action.tag)
        ending = action.reason is CancelReason.RUN_ENDING
        if flight is None:
            if interruption_of(action.reason) is None:
                self._conclude(action.tag, Cancelled(reason=action.reason), ending=ending)
            return
        flight.signal.set(action.reason)
        flight.ending = flight.ending or ending
        flight.changed.set()

    def _settle(self, action: Settle) -> None:
        if action.tag not in self._flights:
            self._conclude(action.tag, action.result, ending=False)

    def _conclude(self, tag: RequestId, result: StepResult, *, ending: bool) -> None:
        """End a step that has no attempt in flight (the host restarted, or it never began)."""
        open_step = self._log.open_step(tag)
        if open_step is not None:
            flight = _Flight(tag, open_step.step, None, ending=ending)
            self._launch(flight, self._clear_then_finish(flight, result))

    def _launch(self, flight: _Flight, work: Coroutine[object, object, None]) -> None:
        self._flights[flight.tag] = flight
        flight.task = asyncio.ensure_future(work)

    async def _wake(self, wake_at: float | None) -> None:
        if self._input is None or self._input.done():
            self._input = asyncio.ensure_future(self._log.next_input())
        waits: set[asyncio.Future[None]] = {self._input}
        waits.update(f.task for f in self._flights.values() if f.task is not None)
        now = self._clock.now()
        deadlines = [f.deadline_at for f in self._flights.values() if f.deadline_at is not None]
        pending = [t for t in (*deadlines, wake_at) if t is not None and t > now]
        if not self._flights and not pending and (why := self._log.stalled()) is not None:
            raise StepLoopStalledError(why)
        timer = None
        if pending:
            timer = asyncio.ensure_future(self._clock.sleep(min(pending) - now))
            waits.add(timer)
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if timer is not None:
                timer.cancel()
        self._reap()

    def _reap(self) -> None:
        for tag, flight in list(self._flights.items()):
            if flight.task is not None and flight.task.done():
                del self._flights[tag]
                flight.task.result()

    async def _abandon_flights(self) -> None:
        tasks = [f.task for f in self._flights.values() if f.task is not None]
        if self._input is not None:
            tasks.append(self._input)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _record_started(self, run: Run) -> StepRecord:
        async with self._appending:
            return await self._log.step_started(run)

    async def _record_finished(
        self, tag: RequestId, attempt: int, result: StepResult
    ) -> StepRecord:
        async with self._appending:
            return await self._log.step_finished(tag, attempt, result)

    def _last_attempt(self, tag: RequestId) -> int:
        open_step = self._log.open_step(tag)
        return 0 if open_step is None else open_step.started

    async def _record_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        async with self._appending:
            await self._log.attempt_interrupted(tag, attempt, why)

    async def _record_ended(self, end: End) -> None:
        async with self._appending:
            await self._log.run_ended(end)

    async def _attempt(self, run: Run, flight: _Flight) -> None:
        cleared = await self._clear(flight) if run.attempt > 1 else Cleared()
        if cleared is None or (flight.signal.is_set() and isinstance(cleared, Cleared)):
            await self._finish_unstarted(flight)
            return
        if isinstance(await self._record_started(run), Interrupted):
            return
        flight.started = True
        outcome = await self._run_bounded(run, flight.signal)
        await self._finish_attempt(run, flight, outcome)

    async def _finish_unstarted(self, flight: _Flight) -> None:
        """The wait for ``clear`` ended by a cancel: no attempt of this try ever started."""
        reason = flight.signal.reason
        if reason is not None and interruption_of(reason) is None:
            await self._record_finished(
                flight.tag, self._last_attempt(flight.tag), Cancelled(reason=reason)
            )

    async def _finish_attempt(self, run: Run, flight: _Flight, outcome: StepResult | None) -> None:
        reason = flight.signal.reason
        pause = None if reason is None else interruption_of(reason)
        if outcome is None:
            await self._finish_without_result(run, flight)
        elif pause is not None and isinstance(outcome, Cancelled):
            await self._record_interrupted(run.tag, run.attempt, pause)
        else:
            await self._record_outcome(run, outcome)

    async def _finish_without_result(self, run: Run, flight: _Flight) -> None:
        """The attempt raised or was given up on: clear what it left, then say how it ended."""
        cleared = await self._clear(flight)
        reason = flight.signal.reason
        pause = None if reason is None else interruption_of(reason)
        if isinstance(cleared, Adopted) and cleared.result is not None:
            await self._record_outcome(run, cleared.result)
        elif reason is None or pause is not None:
            await self._record_interrupted(run.tag, run.attempt, pause or Interruption.RAISED)
        elif isinstance(cleared, Adopted):
            outcome = await self._run_bounded(run, flight.signal)
            if outcome is None:
                await self._record_outcome(run, Cancelled(reason=reason))
            else:
                await self._finish_attempt(run, flight, outcome)
        else:
            await self._record_outcome(run, Cancelled(reason=reason))

    async def _clear_then_finish(self, flight: _Flight, result: StepResult) -> None:
        cleared = await self._clear(flight)
        if (cleared is None and not flight.ending) or self._parked():
            return
        if isinstance(cleared, Adopted) and cleared.result is None:
            await self._end_adopted(flight)
        await self._record_finished(flight.tag, self._last_attempt(flight.tag), result)

    def _parked(self) -> bool:
        """Whether the run is paused: a parked epoch takes no result into its state."""
        return at_rest(self._log.state) is RunRest.PARKED

    async def _record_outcome(self, run: Run, result: StepResult) -> None:
        """Record how a running attempt ended; the log turns it into an interruption if parked."""
        await self._record_finished(run.tag, run.attempt, result)

    async def _end_adopted(self, flight: _Flight) -> None:
        """Attach to work ``clear`` adopted and still going, with the signal set, so it ends."""
        open_step = self._log.open_step(flight.tag)
        if open_step is not None:
            flight.signal.set(CancelReason.WITHDRAWN)
            run = Run(flight.step, flight.tag, open_step.started + 1, open_step.deadline_at)
            await self._run_bounded(run, flight.signal)

    async def _clear(self, flight: _Flight) -> Adopted | Cleared | None:
        """``clear`` the tag, retrying while its backend is unreachable.

        Returns ``None`` when the wait was given up (see ``_Flight.abandons_clear``).
        """
        delay = self._timing.clear_backoff_s
        while True:
            try:
                return await self._bounded(
                    self._runner.clear(flight.step, flight.tag), self._timing.clear_timeout_s
                )
            except (ClearUnavailableError, TimeoutError):
                if flight.abandons_clear():
                    return None
            flight.changed.clear()
            await self._first_of(self._clock.sleep(delay), flight.changed.wait())
            delay = min(delay * 2, self._timing.clear_backoff_max_s)

    async def _run_bounded(self, run: Run, signal: CancelSignal) -> StepResult | None:
        """Run the attempt; give a cancel its grace, then cancel the task, then let go of it."""
        task = asyncio.ensure_future(
            self._runner.run(run.step, run.tag, run.attempt, run.deadline_at, signal)
        )
        try:
            await self._first_of(_await(task), signal.wait())
            if not task.done():
                await self._first_of(_await(task), self._clock.sleep(self._timing.cancel_grace_s))
            if not task.done():
                task.cancel()
                await self._first_of(_await(task), self._clock.sleep(self._timing.cancel_grace_s))
        finally:
            if not task.done():
                task.cancel()
            task.add_done_callback(_drop)
        if task.done() and not task.cancelled() and task.exception() is None:
            return task.result()
        return None

    async def _bounded[T](self, call: Coroutine[object, object, T], seconds: float) -> T:
        task = asyncio.ensure_future(call)
        try:
            await self._first_of(_await(task), self._clock.sleep(seconds))
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if not task.done() or task.cancelled():
            raise TimeoutError
        return task.result()

    @staticmethod
    async def _first_of(*waits: Awaitable[object]) -> None:
        tasks = [asyncio.ensure_future(wait) for wait in waits]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _await[T](task: asyncio.Future[T]) -> None:
    """Wait for ``task`` to end without raising what it raised or being cancelled by it."""
    await asyncio.wait({task})


def _drop[T](task: asyncio.Future[T]) -> None:
    """Consume the result of an attempt the loop gave up on, so a late return is dropped."""
    if not task.cancelled():
        task.exception()


def running_attempt(state: CoreState, tag: RequestId, attempt: int) -> bool:
    """Whether attempt ``attempt`` of step ``tag`` is the one running in ``state``."""
    attempts = state.steps.of(tag)
    return attempts is not None and attempts.started == attempt and not attempts.ended


__all__ = [
    "Interrupted",
    "Recorded",
    "StepLog",
    "StepLoopStalledError",
    "StepLoopTiming",
    "StepLoopTimingError",
    "StepRecord",
    "run_step_loop",
]
