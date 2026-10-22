"""One live writer serializes executor completions and owns the epoch's lease."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_runtime._replay_runtime import (
    ReplayAbsent,
    ReplayPending,
    ReplayRecoveryRequiredError,
    ReplayRetirement,
    ReplayUnknown,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_proto.api import Span
    from vs_runtime._replay_runtime import (
        ReplayEnding,
        ReplayExecutor,
        ReplayLaunch,
        ReplayResult,
        ReplayRuntime,
    )
    from vs_runtime._writer_reconciliation import ReplayWriterBarrier
    from vs_sim.api import SleepingClock


@dataclass(frozen=True)
class ReplayRunHost:
    """The already-composed runtime, its owning executor and injected host clock."""

    runtime: ReplayRuntime
    executor: ReplayExecutor
    clock: SleepingClock
    ending: Callable[[object], ReplayEnding]
    """Pure policy mapping from the root value to a resumable epoch ending."""
    writer_barrier: ReplayWriterBarrier | None = None


@dataclass(frozen=True)
class ReplayCompleted:
    """The root completed this epoch. A later ordinary Resume still runs it again."""

    value: object


@dataclass(frozen=True)
class ReplayParked:
    """The epoch retained an unused-results notice before dispatching its frontier."""


@dataclass(frozen=True)
class _Completion:
    span_id: str
    value: ReplayResult | BaseException


def _at(now: float) -> Timestamp:
    seconds = int(now)
    return Timestamp(seconds=seconds, nanos=int((now - seconds) * 1_000_000_000))


class _LiveEpoch:
    def __init__(self, host: ReplayRunHost, launch: ReplayLaunch) -> None:
        self.host, self.launch = host, launch
        self.completions: asyncio.Queue[_Completion] = asyncio.Queue()
        self.running: dict[str, asyncio.Task[ReplayResult]] = {}
        self.wall_origin = host.clock.now()
        self.flow_origin = host.runtime.archive.clock
        self.renew_at = self.wall_origin + launch.lease_duration / 3

    def flow_clock(self) -> float:
        return self.flow_origin + max(0, self.host.clock.now() - self.wall_origin)

    async def execute(self, span: Span) -> ReplayResult:
        return await self.host.runtime.dispatch(
            span.id, self.host.executor, now=self.host.clock.now()
        )

    def completed(self, task: asyncio.Task[ReplayResult]) -> None:
        if task.cancelled():
            value: ReplayResult | BaseException = asyncio.CancelledError()
        else:
            error = task.exception()
            value = error if error is not None else task.result()
        self.completions.put_nowait(_Completion(task.get_name(), value))

    def start_frontier(self) -> None:
        shell = self.host.runtime
        # Already-running siblings still report usage when an edited branch leaves
        # results unused. Only new dispatch is held at that boundary.
        if shell.frontier.unused and self.running:
            return
        now = self.host.clock.now()
        for span in shell.prepare(now=now, at=_at(now)):
            task = asyncio.create_task(self.execute(span), name=span.id)
            self.running[span.id] = task
            task.add_done_callback(self.completed)

    def next_wake(self) -> float:
        wake = self.renew_at
        clocks = self.host.runtime.clock_requests()
        if clocks:
            wake = min(wake, self.wall_origin + max(0, clocks[0] - self.flow_origin))
        return wake

    async def wait(self) -> _Completion | None:
        if not self.completions.empty():
            return self.completions.get_nowait()
        completion = asyncio.create_task(self.completions.get())
        sleeper = asyncio.create_task(
            self.host.clock.sleep(max(0, self.next_wake() - self.host.clock.now()))
        )
        try:
            await asyncio.wait((completion, sleeper), return_when=asyncio.FIRST_COMPLETED)
            return completion.result() if completion.done() else None
        finally:
            completion.cancel()
            sleeper.cancel()
            await asyncio.gather(completion, sleeper, return_exceptions=True)

    def tick(self) -> None:
        shell, clock = self.host.runtime, self.host.clock
        now = clock.now()
        if now >= self.renew_at:
            shell.renew(now=now, duration=self.launch.lease_duration)
            self.renew_at = now + self.launch.lease_duration / 3
        deadlines = shell.clock_requests()
        if deadlines and deadlines[0] <= self.flow_clock():
            shell.advance_clock(self.flow_clock(), now=now, at=_at(now))

    def release(self, completed: _Completion) -> None:
        del self.running[completed.span_id]
        if isinstance(completed.value, BaseException):
            raise completed.value
        now = self.host.clock.now()
        self.host.runtime.observe(
            completed.span_id, completed.value, now=now, at=_at(now), clock=self.flow_clock()
        )
        self.host.runtime.accept(completed.span_id, now=now, at=_at(now))

    async def _inspect(
        self, span: Span
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        shell, clock = self.host.runtime, self.host.clock
        inspection = asyncio.create_task(
            shell.inspect(span.id, self.host.executor, now=clock.now())
        )
        try:
            while not inspection.done():
                sleeper = asyncio.create_task(clock.sleep(max(0, self.renew_at - clock.now())))
                try:
                    await asyncio.wait((inspection, sleeper), return_when=asyncio.FIRST_COMPLETED)
                    if not inspection.done():
                        shell.renew(now=clock.now(), duration=self.launch.lease_duration)
                        self.renew_at = clock.now() + self.launch.lease_duration / 3
                finally:
                    sleeper.cancel()
                    await asyncio.gather(sleeper, return_exceptions=True)
            return inspection.result()
        finally:
            inspection.cancel()
            await asyncio.gather(inspection, return_exceptions=True)

    async def _retire(self, identities: list[str]) -> None:
        shell, clock = self.host.runtime, self.host.clock
        executor, barrier = self.host.executor, self.host.writer_barrier
        if not isinstance(executor, ReplayRetirement) or barrier is None:
            raise ReplayRecoveryRequiredError(
                shell.recovery, ("execution owner cannot settle withdrawn children",)
            )
        for identity in identities:
            await shell.retire(
                identity, executor, barrier.registry, now=clock.now(), at=_at(clock.now())
            )

    async def _recover_inspections(self, spans: tuple[Span, ...]) -> tuple[list[str], list[str]]:
        shell, clock = self.host.runtime, self.host.clock
        reasons = []
        barrier = self.host.writer_barrier
        withdrawn = []
        for span in spans:
            result = await self._inspect(span)
            if isinstance(result, (ReplayPending, ReplayUnknown)):
                if span.replay_physical is not None and barrier is not None:
                    shell.withdraw(span.id, now=clock.now(), at=_at(clock.now()))
                    withdrawn.append(span.id)
                else:
                    detail = (
                        "invocation is still pending"
                        if isinstance(result, ReplayPending)
                        else result.reason
                    )
                    reasons.append(f"{span.id}: {detail}")
            else:
                shell.recover(span.id, result, now=clock.now(), at=_at(clock.now()))
                if span.replay_physical is not None and span.replay_physical.has_field(
                    "withdrawn_sequence"
                ):
                    withdrawn.append(span.id)
        return withdrawn, reasons

    async def recover(self) -> None:
        shell, clock = self.host.runtime, self.host.clock
        barrier = self.host.writer_barrier
        manifest = (
            None if barrier is None else barrier.begin(shell, now=clock.now(), at=_at(clock.now()))
        )
        unfinished = shell.recovery
        withdrawn, reasons = await self._recover_inspections(
            tuple(span for span in unfinished if span.replay_physical is not None)
        )
        if barrier is not None and manifest is not None:
            await barrier.finish(shell, manifest, now=clock.now(), at=_at(clock.now()))
            if withdrawn:
                await self._retire(withdrawn)
        # Inspecting an old root acquisition may resume its owning effect. It
        # must follow proof and restoration, just like a fresh acquisition.
        _, remaining = await self._recover_inspections(
            tuple(span for span in unfinished if span.replay_physical is None)
        )
        reasons.extend(remaining)
        if shell.recovery:
            raise ReplayRecoveryRequiredError(shell.recovery, tuple(reasons))
        shell.finish_recovery()
        self.wall_origin, self.flow_origin = clock.now(), shell.archive.clock

    async def run(self) -> ReplayCompleted | ReplayParked:
        while True:
            self.start_frontier()
            shell = self.host.runtime
            if not self.running:
                if shell.paused:
                    return ReplayParked()
                if shell.admission_limited:
                    now = self.host.clock.now()
                    shell.park_admission_limit(now=now, at=_at(now))
                    return ReplayParked()
                if shell.completed:
                    now = self.host.clock.now()
                    shell.finish(self.host.ending(shell.result), now=now, at=_at(now))
                    return ReplayCompleted(shell.result)
            completed = await self.wait()
            # Renewal precedes persistence even when a completion and heartbeat
            # become ready together. No clock observation can split observe/accept.
            if completed is not None and isinstance(completed.value, BaseException):
                raise completed.value
            self.tick()
            if completed is not None:
                self.release(completed)

    async def close(self) -> None:
        tasks = tuple(self.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def drive_replay(host: ReplayRunHost, launch: ReplayLaunch) -> ReplayCompleted | ReplayParked:
    """Start and close one epoch; every completion passes durable observe/accept.

    External I/O may run concurrently, but only this coroutine writes records or
    delivers outcomes. Heartbeats use host time; flow time excludes reconstruction
    and time between epochs. Any failure cancels owned calls and releases the lease;
    cancellation never proves that an external invocation was absent.
    """
    epoch: _LiveEpoch | None = None
    try:
        now = host.clock.now()
        host.runtime.start(launch, now=now, at=_at(now))
        epoch = _LiveEpoch(host, launch)
        await epoch.recover()
        return await epoch.run()
    finally:
        try:
            if epoch is not None:
                await epoch.close()
        finally:
            host.runtime.close(now=host.clock.now())
