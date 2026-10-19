"""Bound a cooperative stop: stop new work at once, cancel the run after a grace period."""

from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from vs_async_ops.api import drain
from vs_runtime._run_control import RunStopped

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine

    from vs_runtime._run_control import RunControlChannel
    from vs_runtime.contracts import (
        AccuracyEvaluation,
        AccuracyReceipt,
        BenchmarkEvaluation,
        BenchmarkObjective,
        BuildEvaluation,
        Evaluation,
        LocalValidationEvaluation,
        Workspace,
    )
    from vs_sim.api import OutputSink

type StopTimer = Callable[[float], Awaitable[None]]
"""Wait the given number of seconds; ``asyncio.sleep`` in production."""


class StopGraceError(ValueError):
    """A stop grace period is not a positive finite number of seconds."""

    def __init__(self, grace_s: float) -> None:
        """Name the rejected value."""
        super().__init__(f"stop grace must be a positive finite number of seconds, got {grace_s!r}")


class _StopSupervisor:
    """Act on stop requests for the task running one ``bounded_stop`` block.

    Two jobs, two owners. The grace wait belongs to the block: it ends with the
    block. A stop's cleanup (``on_stop``) belongs to the stop: it runs in its
    own task and is always seen through, even if the block ends first.
    """

    def __init__(
        self,
        channel: RunControlChannel,
        grace_s: float,
        on_stop: Callable[[], Awaitable[None]],
        timer: StopTimer,
    ) -> None:
        task = asyncio.current_task()
        if task is None:
            message = "bounded_stop must run inside an asyncio task"
            raise RuntimeError(message)
        self._task = task
        self._channel = channel
        self._grace_s = grace_s
        self._on_stop = on_stop
        self._timer = timer
        self._requested = asyncio.Event()
        self._grace_waits: list[asyncio.Task[None]] = []
        self._cleanups: list[asyncio.Task[None]] = []
        self.forced = False
        self.failures: list[Exception] = []

    def notify(self) -> None:
        """Wake the supervisor; called on the event loop."""
        self._requested.set()

    def start(self) -> None:
        """Begin watching for stop requests."""
        self._grace_waits.append(asyncio.create_task(self._watch()))

    async def end(self) -> None:
        """Abandon the grace wait and see every stop cleanup through to its end."""
        for wait in self._grace_waits:
            wait.cancel()
        await drain(*self._grace_waits, *self._cleanups)

    async def _watch(self) -> None:
        """Stop new work at each request; cancel the block if the stop outlives the grace."""
        while True:
            await self._requested.wait()
            self._requested.clear()
            cleanup = asyncio.create_task(self._stop_new_work())
            self._cleanups.append(cleanup)
            # ``wait`` leaves *cleanup* running if this watcher is cancelled.
            await asyncio.wait({cleanup})
            await self._timer(self._grace_s)
            # Nothing undoes a stop (a resume is refused while one is pending), so a stop
            # that outlived the grace is still in force.
            self.forced = True
            self._task.cancel()
            return

    async def _stop_new_work(self) -> None:
        try:
            await self._on_stop()
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-122301 [BLE001]; on_stop cancels independently owned external work, and any failure of it must still leave the grace bound armed; a narrower catch would let an unlisted transport error make the stop unbounded, and the error is re-raised when the block ends.
            self.failures.append(error)

    def ended(self, error: BaseException) -> BaseException:
        """Return the error the block leaves with, given the error it raised."""
        outcome = error
        if self.forced:
            remaining = self._task.uncancel()
            if isinstance(error, asyncio.CancelledError) and remaining == 0:
                outcome = RunStopped(
                    f"the stop outlived its {self._grace_s:g} s grace, so the work still "
                    "running was cancelled"
                )
        for failure in self.failures:
            outcome.add_note(f"stop cleanup failed: {failure!r}")
        return outcome


@asynccontextmanager
async def bounded_stop(
    channel: RunControlChannel,
    *,
    grace_s: float,
    on_stop: Callable[[], Awaitable[None]],
    timer: StopTimer,
) -> AsyncIterator[None]:
    """Bound how long the enclosed block outlives a stop request on *channel*.

    When a stop is requested while the block runs, ``on_stop`` runs at once
    (it rejects and cancels new external work), then *timer* waits
    ``grace_s``. If the block is still running after that wait, the task
    running the block is cancelled, the same cancellation a repeated signal
    delivers, so the block unwinds through its own teardown; the block then
    leaves as :class:`RunStopped`. Nothing disarms the cancellation: a stop only
    escalates, and the channel refuses a resume while one is pending. A failure of ``on_stop`` does not disarm it: the error
    is raised when the block ends, or noted on the error the block ends with.

    ``on_stop`` runs to completion even when the block ends first: leaving the
    block abandons the grace wait, never a cleanup already started, so the
    block does not exit until it has finished.
    """
    if not math.isfinite(grace_s) or grace_s <= 0:
        raise StopGraceError(grace_s)
    supervisor = _StopSupervisor(channel, grace_s, on_stop, timer)
    loop = asyncio.get_running_loop()
    unsubscribe = channel.on_stop_requested(lambda: loop.call_soon_threadsafe(supervisor.notify))
    if channel.stop_requested():
        supervisor.notify()
    supervisor.start()
    try:
        yield
    except BaseException as error:
        unsubscribe()
        await supervisor.end()
        outcome = supervisor.ended(error)
        if outcome is error:
            raise
        raise outcome from error
    unsubscribe()
    await supervisor.end()
    if supervisor.failures:
        raise supervisor.failures[0]


class _StopGatedEvaluation:
    """Refuse new evaluations after a stop and cancel running ones at the stop."""

    def __init__(self, inner: Evaluation, channel: RunControlChannel) -> None:
        self._inner = inner
        self._channel = channel

    async def build(self, workspace: Workspace, *, output: OutputSink) -> BuildEvaluation:
        return await self.until_stop(self._inner.build(workspace, output=output))

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
        output: OutputSink,
    ) -> AccuracyEvaluation:
        return await self.until_stop(self._inner.accuracy(workspace, reuse=reuse, output=output))

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
        output: OutputSink,
    ) -> BenchmarkEvaluation:
        return await self.until_stop(
            self._inner.benchmark(workspace, objectives=objectives, output=output)
        )

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        return await self.until_stop(
            self._inner.validate_local(
                workspace, recipe_artifact=recipe_artifact, report_location=report_location
            )
        )

    async def until_stop[T](self, evaluation: Coroutine[object, object, T]) -> T:
        """Run *evaluation*; a stop requested meanwhile cancels it and lands."""
        try:
            self._channel.raise_if_stopped()
        except RunStopped:
            evaluation.close()
            raise
        loop = asyncio.get_running_loop()
        requested = asyncio.Event()
        unsubscribe = self._channel.on_stop_requested(
            lambda: loop.call_soon_threadsafe(requested.set)
        )
        work = asyncio.ensure_future(evaluation)
        stop = asyncio.ensure_future(requested.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            unsubscribe()
            stop.cancel()
            if not work.done():
                # Cancelling the evaluation cancels its external job through
                # the job's owner, on this path and on a caller's cancellation.
                work.cancel()
            await asyncio.gather(work, stop, return_exceptions=True)
        if not work.cancelled():
            return work.result()
        self._channel.raise_if_stopped()
        message = "an evaluation was cancelled before it finished"
        raise RunStopped(message)


def stop_gated_evaluation(inner: Evaluation, channel: RunControlChannel) -> Evaluation:
    """Return *inner* with a pending stop landed before each new evaluation.

    Starting an evaluation is a cooperative boundary, like starting an agent
    turn: once a stop is requested, ``accuracy``, ``benchmark``, and
    ``validate_local`` raise :class:`RunStopped` instead of starting work. A call
    running when the stop is requested is cancelled, not awaited, and raises
    :class:`RunStopped`.
    """
    return _StopGatedEvaluation(inner, channel)


__all__ = ["StopGraceError", "StopTimer", "bounded_stop", "stop_gated_evaluation"]
