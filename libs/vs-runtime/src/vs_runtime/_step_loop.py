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
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, assert_never

from vs_core.api.recovery import Cancel, Cancelled, CancelReason, End, Run, Settle, Wait
from vs_runtime._step_runner import Adopted, CancelSignal, Cleared, ClearUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Coroutine

    from vs_core.api import RequestId
    from vs_core.api.recovery import Action, StepResult
    from vs_runtime._step_runner import StepRunner
    from vs_sim.api import SleepingClock


class Interruption(StrEnum):
    """Why an attempt ended without ending its step."""

    PAUSE = "pause"
    """The run is pausing: not a failure and not a retry."""

    RAISED = "raised"
    """The runner raised, or gave nothing back: the same as a crash of the attempt."""


class LoopExit(StrEnum):
    """How the loop returned."""

    ENDED = "ended"
    """``End`` was recorded."""

    PARKED = "parked"
    """The run is paused and nothing is in flight."""


class StepLog[S](Protocol):
    """The run's log as the loop uses it: each method is one atomic durable append.

    ``state`` is the fold of everything appended, by the loop or by an input from elsewhere,
    and is current as soon as an append returns.
    """

    @property
    def state(self) -> S:
        """The state to decide on."""
        ...

    def latest_run(self, tag: RequestId) -> Run | None:
        """The last attempt recorded for ``tag``, with the request it ran; ``None`` if none."""
        ...

    async def step_started(self, run: Run) -> None:
        """Record that an attempt is about to start, before any I/O of it."""
        ...

    async def step_finished(self, tag: RequestId, attempt: int, result: StepResult) -> None:
        """Record the one result of a step and the attempt that produced it (0 if none ran)."""
        ...

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        """Record that an attempt ended and its step did not."""
        ...

    async def run_ended(self, end: End) -> None:
        """Record the end of the run."""
        ...

    async def next_input(self) -> None:
        """Return once an input has been appended since this last returned."""
        ...


class RecoveryRule[S](Protocol):
    """What core decides: pure functions of the folded state."""

    def decide(self, state: S, now: float) -> tuple[Action, ...]:
        """The actions the loop must take now."""
        ...

    def settled(self, state: S) -> LoopExit | None:
        """``ENDED`` if the log already holds the end of the run, ``PARKED`` if the run is paused.

        The loop returns at once for ``ENDED``, and for ``PARKED`` once nothing is in flight.
        """
        ...


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


class _Fate(StrEnum):
    INTERRUPT = "interrupt"
    """Record an interruption; leftovers wait for the next epoch's ``clear``."""
    END_NOW = "end-now"
    """Record the result at once; an unreachable backend is a named residual."""
    SETTLE = "settle"
    """Record the result after ``clear`` succeeded."""


def _fate(reason: CancelReason) -> _Fate:
    match reason:
        case CancelReason.PAUSE_NOW | CancelReason.DRAIN_EXPIRED:
            return _Fate.INTERRUPT
        case CancelReason.RUN_ENDING:
            return _Fate.END_NOW
        case CancelReason.DEADLINE | CancelReason.WITHDRAWN | CancelReason.INTERRUPTED:
            return _Fate.SETTLE
        case _:
            assert_never(reason)


class _Flight:
    """One attempt, or one conclusion of a step with no attempt running."""

    def __init__(self, tag: RequestId, deadline_at: float | None, *, ending: bool = False) -> None:
        self.tag = tag
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
        settles = reason is None or _fate(reason) is _Fate.SETTLE
        return self.ending or self.parked or not settles


async def run_step_loop[S](
    log: StepLog[S],
    rule: RecoveryRule[S],
    runner: StepRunner,
    clock: SleepingClock,
    timing: StepLoopTiming,
) -> LoopExit:
    """Execute the actions ``rule`` decides until the run ends or parks.

    Cancelling this call, or an exception out of it, is a crash: running attempts are
    abandoned with no record, and the next loop over the same log runs them again.
    """
    return await _StepLoop(log, rule, runner, clock, timing).run()


class _StepLoop[S]:
    def __init__(
        self,
        log: StepLog[S],
        rule: RecoveryRule[S],
        runner: StepRunner,
        clock: SleepingClock,
        timing: StepLoopTiming,
    ) -> None:
        self._log = log
        self._rule = rule
        self._runner = runner
        self._clock = clock
        self._timing = timing
        self._flights: dict[RequestId, _Flight] = {}
        self._appending = asyncio.Lock()
        self._input: asyncio.Task[None] | None = None

    async def run(self) -> LoopExit:
        try:
            while True:
                exit_ = await self._turn()
                if exit_ is not None:
                    return exit_
        finally:
            await self._abandon_flights()

    async def _turn(self) -> LoopExit | None:
        if self._rule.settled(self._log.state) is LoopExit.ENDED:
            return LoopExit.ENDED
        actions = self._rule.decide(self._log.state, self._clock.now())
        end = next((a for a in actions if isinstance(a, End)), None)
        if end is not None and not self._flights:
            await self._record_ended(end)
            return LoopExit.ENDED
        wake_at = self._dispatch(actions)
        if self._rule.settled(self._log.state) is LoopExit.PARKED:
            self._release_unstarted()
            if not self._flights:
                return LoopExit.PARKED
        await self._wake(wake_at)
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
            flight = _Flight(run.tag, run.deadline_at)
            self._launch(flight, self._attempt(run, flight))

    def _cancel(self, action: Cancel) -> None:
        flight = self._flights.get(action.tag)
        ending = action.reason is CancelReason.RUN_ENDING
        if flight is None:
            if _fate(action.reason) is not _Fate.INTERRUPT:
                self._conclude(action.tag, Cancelled(reason=action.reason), ending=ending)
            return
        flight.signal.set(action.reason)
        flight.ending = flight.ending or ending
        flight.changed.set()

    def _settle(self, action: Settle) -> None:
        if action.tag not in self._flights:
            self._conclude(action.tag, action.result, ending=False)

    def _conclude(self, tag: RequestId, result: StepResult, *, ending: bool) -> None:
        flight = _Flight(tag, None, ending=ending)
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

    async def _record_started(self, run: Run) -> None:
        async with self._appending:
            await self._log.step_started(run)

    async def _record_finished(self, tag: RequestId, attempt: int, result: StepResult) -> None:
        async with self._appending:
            await self._log.step_finished(tag, attempt, result)

    def _last_attempt(self, tag: RequestId) -> int:
        latest = self._log.latest_run(tag)
        return 0 if latest is None else latest.attempt

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
        await self._record_started(run)
        flight.started = True
        outcome = await self._run_bounded(run, flight.signal)
        await self._finish_attempt(run, flight, outcome)

    async def _finish_unstarted(self, flight: _Flight) -> None:
        """The wait for ``clear`` ended by a cancel: no attempt of this try ever started."""
        reason = flight.signal.reason
        if reason is not None and _fate(reason) is not _Fate.INTERRUPT:
            await self._record_finished(
                flight.tag, self._last_attempt(flight.tag), Cancelled(reason=reason)
            )

    async def _finish_attempt(self, run: Run, flight: _Flight, outcome: StepResult | None) -> None:
        reason = flight.signal.reason
        if outcome is None:
            await self._finish_without_result(run, flight)
        elif (
            isinstance(outcome, Cancelled)
            and reason is not None
            and _fate(reason) is _Fate.INTERRUPT
        ):
            await self._record_interrupted(run.tag, run.attempt, Interruption.PAUSE)
        else:
            await self._record_finished(run.tag, run.attempt, outcome)

    async def _finish_without_result(self, run: Run, flight: _Flight) -> None:
        """The attempt raised or was given up on: clear what it left, then say how it ended."""
        cleared = await self._clear(flight)
        reason = flight.signal.reason
        if isinstance(cleared, Adopted) and cleared.result is not None:
            await self._record_finished(run.tag, run.attempt, cleared.result)
        elif reason is None or _fate(reason) is _Fate.INTERRUPT:
            why = Interruption.RAISED if reason is None else Interruption.PAUSE
            await self._record_interrupted(run.tag, run.attempt, why)
        elif isinstance(cleared, Adopted):
            outcome = await self._run_bounded(run, flight.signal)
            if outcome is None:
                await self._record_finished(run.tag, run.attempt, Cancelled(reason=reason))
            else:
                await self._finish_attempt(run, flight, outcome)
        else:
            await self._record_finished(run.tag, run.attempt, Cancelled(reason=reason))

    async def _clear_then_finish(self, flight: _Flight, result: StepResult) -> None:
        cleared = await self._clear(flight)
        if cleared is None and not flight.ending:
            return
        if isinstance(cleared, Adopted) and cleared.result is None:
            await self._end_adopted(flight)
        await self._record_finished(flight.tag, self._last_attempt(flight.tag), result)

    async def _end_adopted(self, flight: _Flight) -> None:
        """Attach to work ``clear`` adopted and still going, with the signal set, so it ends.

        Only a started attempt leaves anything to adopt, so its request is in the log.
        """
        latest = self._log.latest_run(flight.tag)
        if latest is not None:
            flight.signal.set(CancelReason.WITHDRAWN)
            await self._run_bounded(replace(latest, attempt=latest.attempt + 1), flight.signal)

    async def _clear(self, flight: _Flight) -> Adopted | Cleared | None:
        """``clear`` the tag, retrying while its backend is unreachable.

        Returns ``None`` when the wait was given up (see ``_Flight.abandons_clear``).
        """
        delay = self._timing.clear_backoff_s
        while True:
            try:
                return await self._bounded(
                    self._runner.clear(flight.tag), self._timing.clear_timeout_s
                )
            except (ClearUnavailableError, TimeoutError):
                if flight.abandons_clear():
                    return None
            flight.changed.clear()
            await self._first_of(self._clock.sleep(delay), flight.changed.wait())
            delay = min(delay * 2, self._timing.clear_backoff_max_s)

    async def _run_bounded(self, run: Run, signal: CancelSignal) -> StepResult | None:
        """Run the attempt; give a cancel its grace, then cancel the task, then let go of it."""
        task = asyncio.ensure_future(self._runner.run(run.step, run.tag, run.attempt, signal))
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


__all__ = [
    "Interruption",
    "LoopExit",
    "RecoveryRule",
    "StepLog",
    "StepLoopTiming",
    "StepLoopTimingError",
    "run_step_loop",
]
