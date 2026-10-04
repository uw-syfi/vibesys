"""Bound a cooperative stop: stop new work at once, cancel the run after a grace period."""

from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from vs_runtime._run_control import RunStopped

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine

    from vs_runtime._run_control import RunControlChannel
    from vs_runtime.contracts import (
        AccuracyEvaluation,
        AccuracyReceipt,
        AgentEvaluation,
        BenchmarkEvaluation,
        BenchmarkObjective,
        CandidateProfile,
        Evaluation,
        LocalValidationEvaluation,
        ReleasedJobs,
        Workspace,
    )

type StopTimer = Callable[[float], Awaitable[None]]
"""Wait the given number of seconds; ``asyncio.sleep`` in production."""


class StopGraceError(ValueError):
    """A stop grace period is not a positive finite number of seconds."""

    def __init__(self, grace_s: float) -> None:
        """Name the rejected value."""
        super().__init__(f"stop grace must be a positive finite number of seconds, got {grace_s!r}")


class _StopSupervisor:
    """Act on stop requests for the task running one ``bounded_stop`` block."""

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
        self.forced = False
        self.failures: list[Exception] = []

    def notify(self) -> None:
        """Wake the supervisor; called on the event loop."""
        self._requested.set()

    async def supervise(self) -> None:
        """Stop new work at each request; cancel the block if the stop outlives the grace."""
        while True:
            await self._requested.wait()
            self._requested.clear()
            try:
                await self._on_stop()
            except Exception as error:  # noqa: BLE001  # lint-waiver: LW-122301 [BLE001]; on_stop cancels independently owned external work, and any failure of it must still leave the grace bound armed; a narrower catch would let an unlisted transport error make the stop unbounded, and the error is re-raised when the block ends.
                self.failures.append(error)
            await self._timer(self._grace_s)
            if self._channel.stop_requested():
                self.forced = True
                self._task.cancel()
                return

    def ended(self, error: BaseException) -> BaseException:
        """Return the error the block leaves with, given the error it raised."""
        outcome = error
        if self.forced:
            remaining = self._task.uncancel()
            if isinstance(error, asyncio.CancelledError) and remaining == 0:
                outcome = RunStopped()
        for failure in self.failures:
            outcome.add_note(f"stop cleanup failed: {failure!r}")
        return outcome


@asynccontextmanager
async def bounded_stop(
    channel: RunControlChannel,
    *,
    grace_s: float,
    on_stop: Callable[[], Awaitable[None]],
    timer: StopTimer = asyncio.sleep,
) -> AsyncIterator[None]:
    """Bound how long the enclosed block outlives a stop request on *channel*.

    When a stop is requested while the block runs, ``on_stop`` runs at once
    (it rejects and cancels new external work), then *timer* waits
    ``grace_s``. If the stop is still pending after that wait, the task
    running the block is cancelled, the same cancellation a repeated signal
    delivers, so the block unwinds through its own teardown; the block then
    leaves as :class:`RunStopped`. A resume within the grace period disarms
    the cancellation. A failure of ``on_stop`` does not disarm it: the error
    is raised when the block ends, or noted on the error the block ends with.
    """
    if not math.isfinite(grace_s) or grace_s <= 0:
        raise StopGraceError(grace_s)
    supervisor = _StopSupervisor(channel, grace_s, on_stop, timer)
    loop = asyncio.get_running_loop()
    unsubscribe = channel.on_stop_requested(lambda: loop.call_soon_threadsafe(supervisor.notify))
    if channel.stop_requested():
        supervisor.notify()
    watcher = asyncio.create_task(supervisor.supervise())
    try:
        yield
    except BaseException as error:
        outcome = supervisor.ended(error)
        if outcome is error:
            raise
        raise outcome from error
    finally:
        unsubscribe()
        if not watcher.done():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    if supervisor.failures:
        raise supervisor.failures[0]


class _StopGatedEvaluation:
    """Refuse new evaluations after a stop and cancel running ones at the stop."""

    def __init__(self, inner: Evaluation, channel: RunControlChannel) -> None:
        self._inner = inner
        self._channel = channel

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
    ) -> AccuracyEvaluation:
        return await self._until_stop(self._inner.accuracy(workspace, reuse=reuse))

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
    ) -> BenchmarkEvaluation:
        return await self._until_stop(self._inner.benchmark(workspace, objectives=objectives))

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        return await self._until_stop(
            self._inner.validate_local(
                workspace, recipe_artifact=recipe_artifact, report_location=report_location
            )
        )

    async def agent_evaluations(self, workspace: Workspace) -> tuple[AgentEvaluation, ...]:
        return await self._inner.agent_evaluations(workspace)

    async def can_profile(self) -> bool:
        return await self._inner.can_profile()

    async def profile(self, revision: str, request: str, *, member_id: str) -> CandidateProfile:
        # A profile runs a profiler agent turn, which gets the grace period
        # like any agent turn, so only its start is gated.
        self._channel.raise_if_stopped()
        return await self._inner.profile(revision, request, member_id=member_id)

    async def reopen_jobs(self, member_id: str) -> None:
        """Reconcile a completed release and open a fresh generation for resumed work."""
        self._channel.raise_if_stopped()
        await self._inner.reopen_jobs(member_id)

    async def jobs_released(self, member_id: str) -> bool:
        """Project whether the member's durable scope refuses ordinary admission.

        Closing and completed releases both fence new work. Recovery can
        reconcile cleanup before opening a fresh scope generation.
        """
        return await self._inner.jobs_released(member_id)

    async def release_jobs(self, member_id: str) -> ReleasedJobs:
        # Release is cleanup: it cancels jobs, so it must work after a stop.
        return await self._inner.release_jobs(member_id)

    async def _until_stop[T](self, evaluation: Coroutine[object, object, T]) -> T:
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
        raise RunStopped


def stop_gated_evaluation(inner: Evaluation, channel: RunControlChannel) -> Evaluation:
    """Return *inner* with a pending stop landed before each new evaluation.

    Starting an evaluation is a cooperative boundary, like starting an agent
    turn: once a stop is requested, ``accuracy``, ``benchmark``,
    ``validate_local``, and ``profile`` raise :class:`RunStopped` instead of
    starting work. An ``accuracy``, ``benchmark``, or ``validate_local`` call
    running when the stop is requested is cancelled, not awaited, and raises
    :class:`RunStopped`. Reading recorded evaluations stays available.
    """
    return _StopGatedEvaluation(inner, channel)


__all__ = ["StopGraceError", "StopTimer", "bounded_stop", "stop_gated_evaluation"]
