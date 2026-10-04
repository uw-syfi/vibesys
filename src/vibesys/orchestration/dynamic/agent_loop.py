"""Async shell of the dynamic loop: executes ``HostCore`` effects and feeds results back.

The shell is the only scheduling code that awaits. It starts worker tasks,
stops withdrawn ones, runs the driver's turns, and turns task completions into
events. Every exit (the search ending, an exception, a cancellation) leaves
through one ``_WorkerTasks`` scope, which releases in-flight workers exactly
once and settles every withdrawn worker exactly once.
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
    Refusal,
    Refused,
    SearchEnd,
    SettleWithdrawn,
    StartWorker,
    StopReason,
    StopRequested,
    StopWorker,
    Submit,
    TurnFaulted,
    Withdraw,
    WorkerFinished,
    WorkerOutcome,
    WorkItem,
)
from vibesys.orchestration.dynamic.models import DurableStateCommitError
from vibesys.orchestration.dynamic.transitions import AlreadySettledError
from vs_runtime.api import RunCleanupError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from types import TracebackType

    from vibesys.orchestration.dynamic.control import (
        Effect,
        HostAction,
        HostEvent,
        Withdrawal,
    )


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

    def can_withdraw(self, worker_id: str) -> bool:
        """Whether durable settlement permits withdrawing this worker."""
        ...

    async def withdraw(self, plan: P, withdrawal: Withdrawal) -> None:
        """Learn, before its task is cancelled, that ``plan``'s attempt is withdrawn.

        The attempt keeps its work (a work-in-progress revision) as it exits.
        """
        ...

    async def settle(self, plan: P, withdrawal: Withdrawal) -> None:
        """Durably park or cancel ``plan`` and release its cluster jobs; called once."""
        ...


class DriverActionRefusedError(RuntimeError):
    """The core refused a driver's action, which the driver should have prevented."""


@dataclass(slots=True)
class _WorkerTasks[P]:
    """The one owner of worker tasks; releasing them is its exit, on every path.

    A withdrawn worker is settled exactly once: by the core's
    ``SettleWithdrawn`` once its task ended, or, if the loop leaves first,
    by this scope's exit.
    """

    workers: Workers[P]
    tasks: dict[asyncio.Task[None], WorkItem[P]] = field(default_factory=dict)
    # Process-local task references for already durable withdrawal intents.
    # Recovery authority belongs to Workers, never this cleanup projection.
    pending_settlements: dict[str, tuple[WorkItem[P], Withdrawal]] = field(default_factory=dict)

    async def __aenter__(self) -> _WorkerTasks[P]:
        return self

    async def stop(self, worker_id: str, withdrawal: Withdrawal) -> None:
        """Cancel the running task of ``worker_id``, telling its worker first."""
        found = [(task, item) for task, item in self.tasks.items() if item.worker_id == worker_id]
        if len(found) != 1:
            message = f"the core stopped {worker_id!r}, which has no running task"
            raise RuntimeError(message)
        task, item = found[0]
        self.pending_settlements[worker_id] = (item, withdrawal)
        task.cancel()

    def withdrawn(self, worker_id: str) -> bool:
        """Whether ``worker_id`` was stopped and is not settled yet."""
        return worker_id in self.pending_settlements

    async def settle(self, item: WorkItem[P], withdrawal: Withdrawal) -> None:
        """Settle one withdrawn worker; a queued one was never stopped.

        It stays pending until its settle returns, so a settle a cancellation
        interrupts runs again at the scope's exit; settling is idempotent.
        """
        self.pending_settlements.setdefault(item.worker_id, (item, withdrawal))
        await self.workers.settle(item.plan, withdrawal)
        del self.pending_settlements[item.worker_id]

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # A cancellation must not outlive its workers. Any other exit, an
        # operator stop (``RunStopped``, a BaseException) included, lets them
        # finish so none of their agent work is lost: each persists its own
        # phases, resume settles any that failed, and after a stop the run
        # host bounds this wait.
        if exc_type is not None and issubclass(
            exc_type, (asyncio.CancelledError, DurableStateCommitError)
        ):
            for task in self.tasks:
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        # A worker stopped before the loop left settles here: its task has
        # ended, and its jobs are released and its phase recorded exactly once.
        pending = tuple(self.pending_settlements.values())
        self.pending_settlements.clear()
        results = await asyncio.gather(
            *(self.workers.settle(item.plan, withdrawal) for item, withdrawal in pending),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors and exc_type is None:
            message = "settling withdrawn workers failed"
            raise RunCleanupError(message, tuple(errors))


def _withdraw_item[P](effects: tuple[Effect[P], ...], tasks: _WorkerTasks[P]) -> WorkItem[P]:
    for effect in effects:
        if isinstance(effect, SettleWithdrawn):
            return effect.item
        if isinstance(effect, StopWorker):
            return next(item for item in tasks.tasks.values() if item.worker_id == effect.worker_id)
    message = "accepted withdrawal did not request a worker transition"
    raise RuntimeError(message)


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
    _tasks: _WorkerTasks[P] | None = None
    _withdraw_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # An infrastructure failure aborts this shell; durable recovery authority
    # remains the envelope. The latch only wakes its owning asyncio task.
    _fatal: DurableStateCommitError | None = None
    _run_task: asyncio.Task[object] | None = None

    async def run(self, recovered: tuple[WorkItem[P], ...]) -> SearchEnd:
        """Drive the search and drain tasks before propagating a storage failure."""
        self._run_task = asyncio.current_task()
        try:
            return await self._run(recovered)
        except asyncio.CancelledError:
            if self._fatal is not None:
                raise self._fatal from None
            raise
        finally:
            self._run_task = None

    async def _run(self, recovered: tuple[WorkItem[P], ...]) -> SearchEnd:
        """Recover ``recovered`` work, then schedule until the core ends the search."""
        async with _WorkerTasks[P](self.workers) as tasks:
            self._tasks = tasks
            try:
                if await self._checkpoint(tasks):
                    await self._act(Recover(recovered, self.clock()), tasks)
                while not self.core.ended:
                    await self._step(tasks)
                async with self._withdraw_lock:
                    pass  # Every accepted external action settles before loop teardown.
            finally:
                self._tasks = None
        if self._errors:
            raise self._errors[0]
        if self._end is None:
            message = "the search loop left without an end"
            raise RuntimeError(message)
        return self._end

    async def stop(self, error: Exception) -> None:
        """Stop admission, drain started workers, then propagate the stop reason."""
        tasks = self._tasks
        if tasks is None:
            message = "a search can be stopped only while it runs"
            raise RuntimeError(message)
        await self._stop(StopReason.REQUESTED, error, tasks)

    async def withdraw(self, worker_id: str, withdrawal: Withdrawal) -> Accepted | Refused:
        """Persist withdrawal before cancellation; a storage fault aborts the owning shell."""
        try:
            return await self._withdraw(worker_id, withdrawal)
        except DurableStateCommitError as error:
            self._abort(error)
            raise

    def _abort(self, error: DurableStateCommitError) -> None:
        if self._fatal is None:
            self._fatal = error
        owner = self._run_task
        if owner is not None and owner is not asyncio.current_task():
            owner.cancel()

    async def _withdraw(self, worker_id: str, withdrawal: Withdrawal) -> Accepted | Refused:
        """Park or cancel one running or queued worker; a driver calls this during a turn.

        A refusal (the worker is not in flight, is already withdrawing, or the
        run stopped) changes nothing and is returned for the driver to report.
        """
        tasks = self._tasks
        if tasks is None:
            message = "a worker can be withdrawn only while the search runs"
            raise RuntimeError(message)
        async with self._withdraw_lock:
            action = Withdraw(worker_id, withdrawal, self.clock())
            result, effects = self.core.preview_withdraw(action)
            if isinstance(result, Refused):
                return result
            if not self.workers.can_withdraw(worker_id):
                return Refused(Refusal.ALREADY_SETTLED, worker_id)
            item = _withdraw_item(effects, tasks)
            try:
                await self.workers.withdraw(item.plan, withdrawal)
            except AlreadySettledError:
                return Refused(Refusal.ALREADY_SETTLED, worker_id)
            result, effects = self.core.on_action(Withdraw(worker_id, withdrawal, self.clock()))
            if isinstance(result, Refused):
                # Completion or stop may have freed the slot while preparation
                # awaited storage. The durable request still owns settlement.
                running = [
                    task for task, work in tasks.tasks.items() if work.worker_id == worker_id
                ]
                if running:
                    await tasks.stop(worker_id, withdrawal)
                    await asyncio.gather(*running, return_exceptions=True)
                    for task in running:
                        tasks.tasks.pop(task, None)
                await tasks.settle(item, withdrawal)
                return Accepted()
            await self._execute(effects, tasks)
            return result

    async def _step(self, tasks: _WorkerTasks[P]) -> None:
        if self._fatal is not None:
            raise self._fatal
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
            if task.cancelled():
                if not tasks.withdrawn(item.worker_id):
                    # Independent provider cancellation must propagate unchanged.
                    task.result()
                # The core settles an explicitly withdrawn worker.
                await self._feed(
                    WorkerFinished(item.worker_id, WorkerOutcome.COMPLETED, self.clock()), tasks
                )
                continue
            error = task.exception()
            if isinstance(error, DurableStateCommitError):
                raise error
            outcome = await self.workers.classify(item.plan, error)
            if outcome is WorkerOutcome.FATAL and error is not None:
                self._errors.append(error)
            if (
                outcome is WorkerOutcome.RETRYABLE
                and self.core.stopped is None
                and not tasks.withdrawn(item.worker_id)
            ):
                # A retry starts agent work: land a pending stop first, so the
                # core settles the worker instead of restarting it.
                await self._checkpoint(tasks)
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
        if self._fatal is not None:
            raise self._fatal
        for effect in effects:
            match effect:
                case StartWorker(item=item):
                    tasks.tasks[asyncio.create_task(self.workers.run_worker(item.plan))] = item
                case RecordGiveUp(item=item):
                    await self.workers.give_up(item.plan)
                case StopWorker(worker_id=worker_id, withdrawal=withdrawal):
                    await tasks.stop(worker_id, withdrawal)
                case SettleWithdrawn(item=item, withdrawal=withdrawal):
                    await tasks.settle(item, withdrawal)
                case EndSearch(end=end):
                    self._end = end
