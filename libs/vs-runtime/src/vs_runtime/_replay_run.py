"""One launch lifetime owns the epoch's lease; its one live writer serializes completions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import applied_control
from vs_runtime._replay_controls import apply_controls
from vs_runtime._replay_runtime import (
    ReplayPending,
    ReplayRecoveryRequiredError,
    ReplayResult,
    ReplayRetirement,
    ReplayUnknown,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

    from vs_project.api import ControlInbox
    from vs_proto.api import Span
    from vs_runtime._replay_runtime import (
        ReplayEnding,
        ReplayExecutor,
        ReplayLaunch,
        ReplayRuntime,
    )
    from vs_runtime._writer_reconciliation import ReplayWriterBarrier
    from vs_sim.api import SleepingClock


@dataclass(frozen=True)
class ReplayRunHost:
    """The already-composed runtime, its owning executor, injected host clock and inbox."""

    runtime: ReplayRuntime
    executor: ReplayExecutor
    clock: SleepingClock
    ending: Callable[[object], ReplayEnding]
    """Pure policy mapping from the root value to a resumable epoch ending."""
    writer_barrier: ReplayWriterBarrier | None = None
    inbox: ControlInbox | None = None
    """The operator's controls, applied at every wake and at least every poll interval."""
    control_poll_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not 0 < self.control_poll_seconds < float("inf"):
            problem = "control_poll_seconds must be finite and positive"
            raise ValueError(problem)


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


_OPENED = object()


class ReplayLifetime:
    """One launch: host exclusion, its committed successor epoch, inbox, clock and services.

    Only ``replay_lifetime`` or ``open_replay`` construct one, after the epoch is
    durable; leaving their scope releases the lease. ``drive_replay`` drives a
    lifetime once and cannot open another epoch.
    """

    def __init__(self, host: ReplayRunHost, launch: ReplayLaunch, opened: object) -> None:
        if opened is not _OPENED:
            problem = "open a replay lifetime with replay_lifetime or open_replay"
            raise TypeError(problem)
        self.host, self.launch = host, launch
        self._driven = False

    def claim(self) -> None:
        """Take the lifetime's one drive; a second drive raises."""
        if self._driven:
            problem = "a replay lifetime drives its epoch once; Resume opens a new lifetime"
            raise RuntimeError(problem)
        self._driven = True


@asynccontextmanager
async def started_epoch(
    runtime: ReplayRuntime, launch: ReplayLaunch, clock: SleepingClock
) -> AsyncIterator[None]:
    """Acquire exclusion and commit the successor epoch; release the lease on exit."""
    now = clock.now()
    runtime.start(launch, now=now, at=_at(now))
    try:
        yield
    finally:
        runtime.close(now=clock.now())


def opened_lifetime(host: ReplayRunHost, launch: ReplayLaunch) -> ReplayLifetime:
    """Serve the inbox for a composed host whose epoch ``started_epoch`` committed."""
    if host.inbox is not None:
        host.inbox.serve()
    return ReplayLifetime(host, launch, _OPENED)


@asynccontextmanager
async def replay_lifetime(
    host: ReplayRunHost, launch: ReplayLaunch
) -> AsyncIterator[ReplayLifetime]:
    """Open one lifetime for an already-composed host with no environment to open."""
    async with started_epoch(host.runtime, launch, host.clock):
        yield opened_lifetime(host, launch)


class _LiveEpoch:
    def __init__(self, host: ReplayRunHost, launch: ReplayLaunch) -> None:
        self.host, self.launch = host, launch
        self.completions: asyncio.Queue[_Completion] = asyncio.Queue()
        self.running: dict[str, asyncio.Task[ReplayResult]] = {}
        self.withdrawn: set[str] = set()
        self.wall_origin = host.clock.now()
        self.flow_origin = host.runtime.archive.clock
        self.renew_at = self.wall_origin + launch.lease_duration / 3
        self.poll_at = self.wall_origin

    def flow_clock(self) -> float:
        return self.flow_origin + max(0, self.host.clock.now() - self.wall_origin)

    async def execute(self, span: Span) -> ReplayResult:
        return await self._owned(
            self.host.runtime.dispatch(span.id, self.host.executor, now=self.host.clock.now())
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

    def serve_controls(self) -> None:
        """Dispose of pending controls, then cancel the running children pause-now withdrew."""
        inbox = self.host.inbox
        if inbox is None:
            return
        now = self.host.clock.now()
        self.poll_at = now + self.host.control_poll_seconds
        if self.host.runtime.paused:
            # A parked epoch applies nothing more; the next epoch classifies the rest.
            return
        for disposition in apply_controls(self.host.runtime, inbox, now=now, at=_at(now)):
            applied = applied_control(disposition)
            for identity in () if applied is None else applied.withdrawn:
                self.withdrawn.add(identity)
                task = self.running.get(identity)
                if task is not None:
                    task.cancel()

    def next_wake(self) -> float:
        wake = self.renew_at if self.host.inbox is None else min(self.renew_at, self.poll_at)
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
        if completed.span_id in self.withdrawn:
            # Cancellation proves nothing, and pause-now parked the epoch: the next
            # launch inspects the unended child and retains whatever it produced.
            return
        if isinstance(completed.value, BaseException):
            raise completed.value
        if self.host.runtime.paused:
            # Work admitted before pause-now finishes with its owner, uncancelled.
            # The parked epoch records nothing more; the next launch inspects it.
            return
        now = self.host.clock.now()
        self.host.runtime.observe(
            completed.span_id, completed.value, now=now, at=_at(now), clock=self.flow_clock()
        )
        self.host.runtime.accept(completed.span_id, now=now, at=_at(now))

    async def _owned[T](self, operation: Coroutine[object, object, T]) -> T:
        """Keep host exclusion across every awaited execution-owner operation."""
        shell, clock = self.host.runtime, self.host.clock
        work = asyncio.create_task(operation)
        try:
            while not work.done():
                sleeper = asyncio.create_task(clock.sleep(max(0, self.renew_at - clock.now())))
                try:
                    await asyncio.wait((work, sleeper), return_when=asyncio.FIRST_COMPLETED)
                    if clock.now() >= self.renew_at:
                        shell.renew(now=clock.now(), duration=self.launch.lease_duration)
                        self.renew_at = clock.now() + self.launch.lease_duration / 3
                finally:
                    sleeper.cancel()
                    await asyncio.gather(sleeper, return_exceptions=True)
            return work.result()
        finally:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)

    async def _retire(self, identities: list[str]) -> None:
        shell, clock = self.host.runtime, self.host.clock
        executor, barrier = self.host.executor, self.host.writer_barrier
        if not isinstance(executor, ReplayRetirement) or barrier is None:
            raise ReplayRecoveryRequiredError(
                shell.recovery, ("execution owner cannot settle withdrawn children",)
            )
        for identity in identities:
            await self._owned(shell.retire(identity, executor, barrier.registry, clock=clock))

    async def _recover_inspections(self, spans: tuple[Span, ...]) -> tuple[list[str], list[str]]:
        shell, clock = self.host.runtime, self.host.clock
        reasons = []
        barrier = self.host.writer_barrier
        withdrawn = []
        for span in spans:
            result = await self._owned(shell.inspect(span.id, self.host.executor, now=clock.now()))
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
            await self._owned(barrier.finish(shell, manifest, clock=clock))
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
            self.serve_controls()
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
            if (
                completed is not None
                and isinstance(completed.value, BaseException)
                and completed.span_id not in self.withdrawn
            ):
                raise completed.value
            self.tick()
            if completed is not None:
                self.release(completed)

    async def close(self) -> None:
        tasks = tuple(self.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def drive_replay(lifetime: ReplayLifetime) -> ReplayCompleted | ReplayParked:
    """Recover and drive the lifetime's epoch once; every completion passes observe/accept.

    External I/O may run concurrently, but only this coroutine writes records or
    delivers outcomes. Heartbeats use host time; flow time excludes reconstruction
    and time between epochs. Any failure cancels owned calls; the lifetime then
    releases the lease. Cancellation never proves that an external invocation was absent.
    """
    lifetime.claim()
    epoch = _LiveEpoch(lifetime.host, lifetime.launch)
    try:
        await epoch.recover()
        return await epoch.run()
    finally:
        await epoch.close()


async def run_replay(host: ReplayRunHost, launch: ReplayLaunch) -> ReplayCompleted | ReplayParked:
    """Open one lifetime for a composed host and drive it once."""
    async with replay_lifetime(host, launch) as lifetime:
        return await drive_replay(lifetime)
