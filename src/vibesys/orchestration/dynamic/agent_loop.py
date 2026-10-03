"""Async shell of the dynamic loop: executes ``HostCore`` effects and feeds results back.

The shell is the only scheduling code that awaits. It starts worker tasks,
runs the driver's turns, and turns task completions into events. Every exit
(the search ending, an exception, a cancellation) leaves through one
``_WorkerTasks`` scope, which releases in-flight workers exactly once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from vibesys.orchestration.dynamic.control import (
    Accepted,
    EndSearch,
    FinishSearch,
    HostCore,
    RecordGiveUp,
    Recover,
    SearchEnd,
    StartWorker,
    StopReason,
    StopRequested,
    Submit,
    TurnFaulted,
    WorkerFinished,
    WorkerOutcome,
    WorkItem,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from types import TracebackType

    from vibesys.orchestration.dynamic.control import Effect, HostAction, HostEvent


class DriverStep(StrEnum):
    """What a driver wants next, decided from the core's state."""

    TURN = "turn"  # Run one driver turn now and submit its plans.
    WAIT = "wait"  # Wait for the next worker to finish.
    FINISH = "finish"  # Submit nothing more; end once started work settles.


class Driver[P](Protocol):
    """Decides what to start; planner mode and agent mode are drivers."""

    async def checkpoint(self) -> None:
        """Land a pending operator stop (by raising) before a turn starts work."""
        ...

    def observe(self, event: HostEvent) -> None:
        """Learn of one event the core applied."""
        ...

    def next_step(self, core: HostCore[P]) -> DriverStep:
        """Choose the next step from the core's current state."""
        ...

    async def turn(self, core: HostCore[P]) -> tuple[WorkItem[P], ...]:
        """Plan new work for the core's free capacity; any exception is a turn fault."""
        ...


class Workers[P](Protocol):
    """Runs and classifies workers; owns their durable records."""

    def run_worker(self, plan: P) -> Coroutine[object, object, None]:
        """Return the coroutine that executes one attempt of ``plan``."""
        ...

    async def classify(self, plan: P, error: BaseException | None) -> WorkerOutcome:
        """Classify a finished attempt from its error and durable state."""
        ...

    async def give_up(self, plan: P) -> None:
        """Durably mark ``plan`` failed with its retry budget spent."""
        ...


class DriverActionRefusedError(RuntimeError):
    """The core refused a driver's action, which the driver should have prevented."""


@dataclass(slots=True)
class _WorkerTasks[P]:
    """The one owner of worker tasks; releasing them is its exit, on every path."""

    tasks: dict[asyncio.Task[None], WorkItem[P]] = field(default_factory=dict)

    async def __aenter__(self) -> _WorkerTasks[P]:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # A cancellation or interrupt must not outlive its workers. Any other
        # exit lets them finish so none of their agent work is lost: each
        # persists its own phases, and resume settles any that failed.
        if exc_type is not None and not issubclass(exc_type, Exception):
            for task in self.tasks:
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()


@dataclass(slots=True)
class AgentLoop[P]:
    """Run one search: drive ``core`` with ``driver`` until it ends.

    ``clock`` returns run-elapsed seconds; it is the only time source the core
    sees. ``run`` returns how the search ended, or raises the first error that
    stopped it after every started worker has settled.
    """

    core: HostCore[P]
    driver: Driver[P]
    workers: Workers[P]
    clock: Callable[[], float]
    _errors: list[BaseException] = field(default_factory=list)
    _end: SearchEnd | None = None

    async def run(self, recovered: tuple[WorkItem[P], ...]) -> SearchEnd:
        """Recover ``recovered`` work, then schedule until the core ends the search."""
        async with _WorkerTasks[P]() as tasks:
            if await self._checkpoint(tasks):
                await self._act(Recover(recovered, self.clock()), tasks)
            while not self.core.ended:
                await self._step(tasks)
        if self._errors:
            raise self._errors[0]
        if self._end is None:
            message = "the search loop left without an end"
            raise RuntimeError(message)
        return self._end

    async def _step(self, tasks: _WorkerTasks[P]) -> None:
        match self.driver.next_step(self.core):
            case DriverStep.TURN:
                if not await self._checkpoint(tasks):
                    return
                try:
                    items = await self.driver.turn(self.core)
                except Exception as error:  # noqa: BLE001  # lint-waiver: LW-261003 [BLE001]; a turn crosses an agent boundary whose faults (crash, timeout, a reply still invalid after correction) have no common type, and each one goes to the core's bounded turn-fault policy; listing types would end the run on an unlisted fault without that policy, and running the turn as a task to read task.exception() only hides the same catch-all.
                    await self._turn_faulted(error, tasks)
                    return
                await self._act(Submit(items, self.clock()), tasks)
            case DriverStep.FINISH:
                await self._act(FinishSearch(self.clock()), tasks)
            case DriverStep.WAIT:
                await self._wait(tasks)

    async def _checkpoint(self, tasks: _WorkerTasks[P]) -> bool:
        """Land a pending stop; return whether work may start."""
        try:
            await self.driver.checkpoint()
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-261004 [BLE001]; the runtime control raises an untyped stop or pause failure, which the shell records, drains started workers for, and re-raises unchanged; listing types would leak an unlisted one past the drain, and a task wrapper only hides the same catch-all.
            await self._stop(StopReason.REQUESTED, error, tasks)
            return False
        return True

    async def _wait(self, tasks: _WorkerTasks[P]) -> None:
        if not tasks.tasks:
            message = "the driver waits, but no worker is running"
            raise RuntimeError(message)
        done, _ = await asyncio.wait(tasks.tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            item = tasks.tasks.pop(task)
            error = task.exception()
            outcome = await self.workers.classify(item.plan, error)
            if outcome is WorkerOutcome.FATAL and error is not None:
                self._errors.append(error)
            await self._feed(WorkerFinished(item.worker_id, outcome, self.clock()), tasks)

    async def _turn_faulted(self, error: BaseException, tasks: _WorkerTasks[P]) -> None:
        """Hand a faulted turn to the core; raise its error once the bound is spent."""
        await self._feed(TurnFaulted(self.clock()), tasks)
        if self.core.stopped is StopReason.TURN_FAULTS_EXHAUSTED:
            self._errors.append(error)

    async def _stop(self, reason: StopReason, error: BaseException, tasks: _WorkerTasks[P]) -> None:
        self._errors.append(error)
        await self._feed(StopRequested(reason, self.clock()), tasks)

    async def _feed(self, event: HostEvent, tasks: _WorkerTasks[P]) -> None:
        self.driver.observe(event)
        await self._execute(self.core.on_event(event), tasks)

    async def _act(self, action: HostAction[P], tasks: _WorkerTasks[P]) -> None:
        result, effects = self.core.on_action(action)
        if not isinstance(result, Accepted):
            message = f"the core refused {type(action).__name__}: {result.code}"
            raise DriverActionRefusedError(message)
        await self._execute(effects, tasks)

    async def _execute(self, effects: tuple[Effect[P], ...], tasks: _WorkerTasks[P]) -> None:
        for effect in effects:
            match effect:
                case StartWorker(item=item):
                    tasks.tasks[asyncio.create_task(self.workers.run_worker(item.plan))] = item
                case RecordGiveUp(item=item):
                    await self.workers.give_up(item.plan)
                case EndSearch(end=end):
                    self._end = end
