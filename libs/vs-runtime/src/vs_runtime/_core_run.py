"""The production run loop over ``CoreRuntime``.

The loop owns time and nothing else. Core is pure: it never sleeps and learns time only
through ``ClockAdvanced``. This loop reads an injected ``SleepingClock``, delivers the time to
core, drains the shell, and sleeps when a drain made no progress.

Pacing. A job that is running but not finished is polled by a request that core issues
each time an observation arrives. Without pacing that cycle runs as fast as the executor
answers (the Fake cluster answers instantly, a real cluster costs a Slurm call each). The
design has three parts, so that a busy loop on a running job cannot happen:

* Core decides when the next observation is due and exposes it as a core time
  (``CoreView.next_observe_at``, the default ``next_wake``). Core emits the next observe
  only once ``ClockAdvanced`` reaches that time.
* The loop never calls the executor on its own. Between settles it either made progress
  (state changed) or it waits until the earliest of: the next time core asks for, the
  next lease renewal, the run deadline, and the next control poll. While requests run it
  also wakes when one finishes or the operator stops. Every no-progress iteration
  waits at least ``min_sleep`` seconds, so no iteration can spin.
* A dispatch cap fails a cycle that never goes idle, naming the request kinds seen.

Time. The clock is a timeline shared across processes (seconds since the epoch in
production), because lease expiry in the state store is compared across hosts. The loop
makes it non-decreasing and never below the last time core committed.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    AttemptPhase,
    ClockAdvanced,
    ContinuationPhase,
    ControlId,
    ControlInput,
    CoreState,
    RecoveryPhase,
    RequestPhase,
    RunControlEvent,
    RunResultProposal,
    RunStatus,
    SessionPhase,
    StopMode,
    orphan_waits,
    project,
    unreleased_records,
)
from vs_runtime._core_loop import LeaseUnavailableError
from vs_runtime._steer_delivery import SteerConsumption, steer_input, unrecorded_steers

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_core.api import CoreEvent
    from vs_runtime._core_loop import CoreRuntime, PublicationDelivery
    from vs_runtime._core_requests import ExecutorRefusal
    from vs_runtime._run_control import RunControlChannel
    from vs_sim.api import SleepingClock, Threads


type ControlAction = Literal["pause", "resume", "stop", "steer"]


#: Name of the task that renews the lease while a drain is in flight; fakes recognize it.
HEARTBEAT_TASK = "lease-heartbeat"

#: Name of the task that sleeps while requests run; the sleep ends early when one finishes.
WAIT_TASK = "loop-wait"


# How long a run's state-store lease stays valid without renewal. The loop renews it every
# third of this; a restart after a crash waits at most this long (see
# ``start_core_awaiting_lease``).
PRODUCTION_LEASE_SECONDS = 60.0


@dataclass(frozen=True)
class RunTiming:
    """The time sources and lease length one run is composed with.

    ``clock`` is the run's epoch timeline for the core loop and its timestamps;
    ``threads`` is the blocking time (monotonic reads, sleeps, locks) that code on
    worker threads, such as an agent turn, waits on. A production entrypoint builds
    both from the real implementations; a simulated run builds them from one simulation.
    """

    clock: SleepingClock
    lease_seconds: float
    threads: Threads

    def __post_init__(self) -> None:
        """Reject a lease that could never be held."""
        if self.lease_seconds <= 0:
            message = "lease_seconds must be positive"
            raise ValueError(message)


class NextWake(Protocol):
    """The earliest core time at which a ``ClockAdvanced`` would make core emit work.

    ``None`` means core is waiting for nothing time-driven. The default reads core's
    pacing (``CoreView.next_observe_at``).
    """

    def __call__(self, core: CoreState) -> float | None:
        """The due time, or ``None``."""
        ...


def core_next_wake(core: CoreState) -> float | None:
    """The default: core's own schedule, the due time of its next paced job poll."""
    return project(core).next_observe_at


class SteerArtifacts(Protocol):
    """Where the text of a steer control lives; the control carries only its digest."""

    def put(self, ref: ArtifactRef, text: str) -> None:
        """Store the text under the reference before the control is submitted."""
        ...


class StallNotes(Protocol):
    """What the host can add to a stall report: causes core's state does not show."""

    def __call__(self) -> Sequence[str]:
        """Short clauses, each naming one reason nothing can proceed; empty when none."""
        ...


def no_stall_notes() -> Sequence[str]:
    """The default: the host knows nothing beyond core's state."""
    return ()


class RunStalledError(RuntimeError):
    """The run is not terminal, not paused, made no progress and core waits for no time."""


@dataclass(frozen=True)
class RunLoopConfig:
    """Loop policy. Seconds are on the clock's timeline."""

    host_id: str
    lease_duration: float
    control_poll_interval: float = 1.0
    min_sleep: float = 0.05
    recovery_poll_interval: float = 1.0
    max_dispatches: int = 100_000
    max_concurrent: int | None = None
    """Requests the shell executes at once; None takes the run's ``Limits.max_parallel``."""

    def __post_init__(self) -> None:
        """Reject intervals that would let the loop spin or lose its lease."""
        for name in ("lease_duration", "control_poll_interval", "min_sleep"):
            if getattr(self, name) <= 0:
                message = f"{name} must be positive"
                raise ValueError(message)
        if (
            self.recovery_poll_interval <= 0
            or self.max_dispatches <= 0
            or (self.max_concurrent is not None and self.max_concurrent <= 0)
        ):
            message = "recovery_poll_interval, max_dispatches and max_concurrent must be positive"
            raise ValueError(message)


@dataclass(frozen=True)
class RunOutcome:
    """How the loop returned: the committed status, its result, or an executor refusal."""

    status: RunStatus
    result: RunResultProposal | None
    refusal: ExecutorRefusal | None = None


class RunControlBridge:
    """Turns the cooperative ``RunControlChannel`` mailbox into core control events.

    The channel is level-triggered (a pause flag, a stop flag, a queue of steer texts).
    Each ``poll`` emits one event per change since the last poll, with stable
    identities ``control-<n>`` that continue the count of controls core already holds,
    so a resumed run never reuses an identity. A stop carries no result: the search
    adopts its last choice and ends the run itself.
    """

    def __init__(
        self,
        channel: RunControlChannel,
        steer: SteerArtifacts,
    ) -> None:
        """Bind the mailbox and the steer text store."""
        self._channel = channel
        self._steer = steer
        self._count = 0
        self._consumption = SteerConsumption()
        self._started = False
        self._paused = False
        self._stopped = False

    def watch_stop(self, listener: Callable[[], object]) -> Callable[[], None]:
        """Call ``listener`` (on the requesting thread) at each stop request; returns unsubscribe.

        Only wakes the loop so it reads the channel now; the stop reaches core through
        ``poll`` like every control.
        """
        return self._channel.on_stop_requested(listener)

    def poll(self, core: CoreState, now_at: float) -> tuple[CoreEvent, ...]:
        """Controls requested since the last poll, in submission order.

        Each steer control is followed by the session input that delivers it to the next
        turn (``_steer_delivery``), and a steer control whose input core never recorded
        gets that input too.
        """
        self._consumption.newly_delivered(core, self._report_consumed)
        if not self._started:
            self._started = True
            self._count = len(core.run.controls)
            self._paused = core.run.status == RunStatus.PAUSED
            self._stopped = any(c.action == "stop" for c in core.run.controls)
        events: list[CoreEvent] = list(unrecorded_steers(core, now_at))
        for text in self._channel.take_pending_steer():
            control = self._event("steer", now_at, text=text)
            events += (control, steer_input(core, control.control, now_at))
        if self._stopped:
            return tuple(events)
        if self._channel.stop_requested():
            self._stopped = True
            events.append(self._event("stop", now_at, mode=self._channel.stop_mode()))
        elif self._channel.pause_requested() != self._paused:
            self._paused = not self._paused
            events.append(self._event("pause" if self._paused else "resume", now_at))
        return tuple(events)

    def _report_consumed(self, role: str, round_label: str, execution_id: str) -> None:
        self._channel.notify_steer_consumed(
            agent_kind=role, round_label=round_label, execution_id=execution_id
        )

    def _event(
        self,
        action: ControlAction,
        now_at: float,
        *,
        text: str | None = None,
        mode: StopMode | None = None,
    ) -> RunControlEvent:
        self._count += 1
        identity = ControlId(root=f"control-{self._count}")
        artifact = None
        if text is not None:
            artifact = ArtifactRef(
                artifact_id=ArtifactId(root=f"steer-{identity.root}"),
                digest=hashlib.sha256(text.encode()).hexdigest(),
            )
            self._steer.put(artifact, text)
        control = ControlInput(control_id=identity, action=action, mode=mode, artifact=artifact)
        return RunControlEvent(control=control, now_at=now_at)


@dataclass(frozen=True)
class CoreRunHost:
    """The process-local pieces one run loop drives."""

    shell: CoreRuntime
    delivery: PublicationDelivery
    clock: SleepingClock
    controls: RunControlBridge | None = None


def start_core(host: CoreRunHost, config: RunLoopConfig) -> None:
    """Take the lease and commit the new-epoch recovery, at the clock's current time."""
    host.shell.start(config.host_id, now_at=host.clock.now(), lease_duration=config.lease_duration)


async def start_core_awaiting_lease(host: CoreRunHost, config: RunLoopConfig) -> None:
    """Start the shell, waiting out a lease a crashed host left behind.

    A host that died keeps its lease until it expires, and a restart cannot tell a dead
    holder from a live one, so it waits on the run clock, at most one lease duration
    (a live holder renews, so it would still hold the lease after that). The wait is
    in clock time: a simulated clock makes it instant. Raises ``LeaseUnavailableError``
    naming the bound when the lease is still held after it.
    """
    give_up_at = host.clock.now() + config.lease_duration
    while True:
        try:
            start_core(host, config)
        except LeaseUnavailableError as error:
            remaining = give_up_at - host.clock.now()
            if remaining <= 0:
                message = (
                    f"the run's lease is still held {config.lease_duration:g} s after this "
                    "host started waiting for it: another host is running this run, or the "
                    "clock is behind the run's last recorded time"
                )
                raise LeaseUnavailableError(message) from error
            await host.clock.sleep(min(config.recovery_poll_interval, remaining))
        else:
            return


async def drive_core(
    host: CoreRunHost,
    config: RunLoopConfig,
    *,
    next_wake: NextWake = core_next_wake,
    stall_notes: StallNotes = no_stall_notes,
) -> RunOutcome:
    """Run a started shell until its run is terminal, an executor is refused, or it fails.

    Each iteration: renew the lease when a third of it has passed, submit controls and
    the deadline stop, deliver the clock, settle the shell, ask the strategy once recovery
    is READY, settle again. Requests (agent turns, submissions) keep running across
    iterations, so the strategy decides, the clock ticks and controls are read whenever
    any request finishes, not only when all have. An iteration that changed nothing waits
    (see the module docstring); one that cannot be woken by time raises ``RunStalledError``.

    A stop is a control like the others: core commits it, cancels the running turns, and
    cancels the run's owned jobs and closes its attempts once the requests it cannot cancel
    (a submission, a job poll) have returned their observations; the run then ends
    terminal. The loop waits for those requests. Only the host's grace bound ends the wait
    early, with ``RunStopped`` and the record open; the abandoned requests stay dispatched,
    and a resume inspects them and finishes the drain.

    ``stall_notes`` adds what only the host knows to the ``RunStalledError`` message, such
    as a charged submission whose handle never reached its agent.
    """
    return await _Loop(host, config, next_wake, stall_notes).run()


class _Loop:
    def __init__(
        self,
        host: CoreRunHost,
        config: RunLoopConfig,
        next_wake: NextWake,
        stall_notes: StallNotes,
    ) -> None:
        self._shell = host.shell
        self._delivery = host.delivery
        self._clock = host.clock
        self._controls = host.controls
        self._config = config
        self._next_wake = next_wake
        self._stall_notes = stall_notes
        self._time = self._core().run.now_at
        self._renewed_at = self._read()
        self._deadline_stop = any(
            c.control_id.root == "deadline" for c in self._core().run.controls
        )
        self._first_dispatch = self._shell.dispatched
        self._lease: asyncio.Task[None] | None = None
        # Set when the operator asks to stop, so a wait that has requests running ends now.
        self._woken = asyncio.Event()

    def _core(self) -> CoreState:
        return self._shell.record.envelope.core

    def _read(self) -> float:
        """Non-decreasing time, never below what core already committed."""
        self._time = max(self._time, self._clock.now(), self._core().run.now_at)
        return self._time

    def _outcome(self, refusal: ExecutorRefusal | None = None) -> RunOutcome:
        run = self._core().run
        return RunOutcome(status=run.status, result=run.result, refusal=refusal)

    async def run(self) -> RunOutcome:
        """The loop body, with the stop wake-up subscribed and running requests released."""
        loop = asyncio.get_running_loop()
        unsubscribe = (
            self._controls.watch_stop(lambda: loop.call_soon_threadsafe(self._woken.set))
            if self._controls is not None
            else _noop
        )
        # Requests outlast the lease, so it is renewed from beside the loop, which may be
        # waiting for them or publishing. A renewal that fails ends the loop.
        heartbeat = asyncio.create_task(self._heartbeat(), name=HEARTBEAT_TASK)
        self._lease = heartbeat
        try:
            return await self._iterate()
        finally:
            unsubscribe()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if self._shell.in_flight:
                await self._shell.abandon()
            if not heartbeat.cancelled() and (failure := heartbeat.exception()) is not None:
                raise failure

    async def _iterate(self) -> RunOutcome:
        while self._core().run.status != RunStatus.TERMINAL:
            self._raise_if_lease_lost()
            now = self._read()
            self._renew(now)
            self._shell.record_renewal()
            self._woken.clear()
            for event in self._due_controls(now):
                self._shell.submit(event, now_at=now)
            self._shell.submit(ClockAdvanced(now_at=now), now_at=now)
            before = self._signature()
            refusal = await self._settle(now)
            if refusal is None and self._core().records.recovery.phase == RecoveryPhase.READY:
                self._shell.flush(now_at=now)
                refusal = await self._settle(now)
            if refusal is not None:
                # Requests already running finish and commit before the refusal ends the run.
                await self._shell.finish_in_flight(self._delivery, now_at=now, refusal=refusal)
                return self._outcome(refusal)
            if self._core().run.status == RunStatus.TERMINAL:
                break
            if self._signature() == before:
                await self._wait(now)
        return self._outcome()

    def _slots(self) -> int:
        return self._config.max_concurrent or self._core().run.limits.max_parallel

    async def _settle(self, now: float) -> ExecutorRefusal | None:
        total = self._shell.dispatched - self._first_dispatch
        remaining = max(self._config.max_dispatches - total, 0)
        try:
            return await self._shell.settle(
                self._delivery,
                now_at=now,
                max_dispatches=remaining,
                max_concurrent=self._slots(),
            )
        finally:
            self._raise_if_lease_lost()

    def _raise_if_lease_lost(self) -> None:
        """Surface a failed renewal as soon as the loop looks, not when the run ends."""
        lease = self._lease
        if (
            lease is not None
            and lease.done()
            and not lease.cancelled()
            and (failure := lease.exception()) is not None
        ):
            raise failure

    async def _heartbeat(self) -> None:
        """Renew the lease a third of its duration after the last renewal, and tell core.

        Core learns of a renewal at the next commit, which may come in the middle of a loop
        iteration that publishes or waits for requests. So the proof of life core holds is at
        most one renewal interval old, plus the longest stretch without a commit (one
        publication, or a wait for requests). Queuing the renewal only at the top of the
        next iteration added every publication of the iteration to that age.
        """
        while True:
            # Sleep until the next renewal is due, not for a fixed period: the loop also
            # renews, and a period out of phase with it wakes just short of due, skips,
            # and leaves up to two intervals between renewals.
            due = self._renewal_due()
            await self._clock.sleep(max(due - self._read(), 0.0))
            if self._renewal_due() == due:
                # Renew even if float rounding woke this a hair before ``due``; checking
                # ``now >= due`` again could skip it and sleep for nothing, forever.
                self._renew_now(self._read())
            self._shell.record_renewal()

    def _renewal_due(self) -> float:
        return self._renewed_at + self._config.lease_duration / 3

    def _renew(self, now: float) -> None:
        if now >= self._renewal_due():
            self._renew_now(now)

    def _renew_now(self, now: float) -> None:
        self._shell.renew(now_at=now, lease_duration=self._config.lease_duration)
        self._renewed_at = now

    def _due_controls(self, now: float) -> tuple[CoreEvent, ...]:
        core = self._core()
        events: list[CoreEvent] = list(self._controls.poll(core, now) if self._controls else ())
        live = core.run.status in (RunStatus.RUNNING, RunStatus.PAUSED)
        if (
            now >= core.run.deadline_at
            and not self._deadline_stop
            and live
            and core.run.result is None
        ):
            self._deadline_stop = True
            events.append(
                RunControlEvent(
                    control=ControlInput(
                        control_id=ControlId(root="deadline"), action="stop", mode=StopMode.DRAIN
                    ),
                    now_at=now,
                )
            )
        if self._recovery_cannot_open(core) and live:
            events.append(
                RunControlEvent(
                    control=ControlInput(
                        control_id=ControlId(root=_RECOVERY_BLOCKED),
                        action="stop",
                        mode=StopMode.CANCEL,
                    ),
                    result=RunResultProposal(
                        outcome="blocked", reason="recovery blocked: outcome unknown"
                    ),
                    now_at=now,
                )
            )
        return tuple(events)

    def _recovery_cannot_open(self, core: CoreState) -> bool:
        """Whether recovery is blocked with nothing left that could unblock it.

        A blocked check reopens only on an observation, and with nothing in flight and no
        timer due none will come: the run would sit running until its deadline. So the loop
        stops it with a ``blocked`` result that says why, as it stops a run at its deadline.
        """
        return (
            core.records.recovery.phase == RecoveryPhase.BLOCKED
            and core.run.result is None
            and not self._shell.in_flight
            and self._next_wake(core) is None
            and self._shell.next_wake_at is None
            and not any(c.control_id.root == _RECOVERY_BLOCKED for c in core.run.controls)
        )

    def _signature(self) -> object:
        """Everything that changes when work happened; time, liveness and the revision do not."""
        record = self._shell.record
        core = record.envelope.core
        quiet = core.model_copy(
            update={
                "revision": 0,
                "run": core.run.model_copy(update={"now_at": 0.0}),
                "records": core.records.model_copy(update={"hosts": ()}),
            }
        )
        return (quiet, record.envelope.strategy, record.delivery_cursor, self._shell.dispatched)

    async def _wait(self, now: float) -> None:
        core = self._core()
        due = self._next_wake(core)
        asked = self._shell.next_wake_at
        if asked is not None and asked > now:
            due = asked if due is None else min(due, asked)
        if self._shell.in_flight:
            await self._wait_while_running(due)
            return
        paused = core.run.status == RunStatus.PAUSED
        # Recovery that the loop already stopped as blocked reopens on nothing: polling it
        # would only wait out the deadline before the same stall.
        recovering = core.records.recovery.phase != RecoveryPhase.READY and not (
            core.records.recovery.phase == RecoveryPhase.BLOCKED
            and any(c.control_id.root == _RECOVERY_BLOCKED for c in core.run.controls)
        )
        past_deadline = now >= core.run.deadline_at
        if due is None and not paused and (not recovering or past_deadline):
            raise RunStalledError(_describe(core, self._stall_notes()))
        wake = [self._renewal_due()]
        if due is not None:
            wake.append(due)
        if recovering and due is None:
            wake.append(now + self._config.recovery_poll_interval)
        if self._controls is not None:
            wake.append(now + self._config.control_poll_interval)
        if not self._deadline_stop:
            wake.append(core.run.deadline_at)
        await self._clock.sleep(max(min(wake) - now, self._config.min_sleep))

    async def _wait_while_running(self, due: float | None) -> None:
        """Wait while requests run, until something needs core: the next iteration.

        That is a request finishing, an operator stop, the time core asked for next (a
        job poll, or a wake the strategy requested), the next control poll (pause and
        steer are read there), the run deadline, or the next lease renewal, which the next
        iteration records in core. A turn can run for many minutes, so these are timers of
        the wait itself; the heartbeat renews the lease.
        """
        now = self._read()
        wake = [self._renewal_due()]
        if due is not None and due > now:
            wake.append(due)
        if self._controls is not None:
            wake.append(now + self._config.control_poll_interval)
        if not self._deadline_stop:
            wake.append(self._shell.record.envelope.core.run.deadline_at)
        await self._race(max(min(wake) - now, 0.0))

    async def _race(self, seconds: float) -> None:
        """Wait for a request to finish, the operator to stop, or ``seconds`` to pass."""
        waiting: list[asyncio.Future[Any]] = [
            asyncio.ensure_future(self._shell.wait_for_flight()),
            asyncio.ensure_future(self._woken.wait()),
            asyncio.create_task(
                self._clock.sleep(max(seconds, self._config.min_sleep)), name=WAIT_TASK
            ),
        ]
        if self._lease is not None:
            waiting.append(self._lease)
        try:
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiting:
                if task is not self._lease:
                    task.cancel()
            await asyncio.gather(
                *waiting[: len(waiting) - (self._lease is not None)], return_exceptions=True
            )


_RECOVERY_BLOCKED = "recovery-blocked"


def _noop() -> None:
    return None


def _describe(core: CoreState, notes: Sequence[str]) -> str:
    """Why nothing can proceed: the open records, then every wait nothing will end.

    A stalled run always names a cause. When core holds no open record and no orphaned wait,
    the strategy still has unfinished work core is not waiting on, so the report lists the
    attempts, sessions and continuations that have not ended.
    """
    open_records = [
        f"{record.request.kind}:{record.phase.value}"
        for record in core.records.records
        if record.phase.value != "completed"
    ]
    text = (
        f"run {core.run.run_id.root} stalled at core revision {core.revision} "
        f"with status {core.run.status.value}; open records {open_records}"
    )
    # An attempt that cannot finish closing is the usual reason nothing else is left to do.
    waiting = [
        f"{attempt.attempt_id.root} {attempt.phase.value} on "
        f"{[f'{edge.kind}:{edge.identity.root}' for edge in attempt.release_dependencies]}"
        for attempt in core.attempts.attempts
        if attempt.release_dependencies
    ]
    if waiting:
        text += f"; attempts waiting for a release {waiting}"
    # A completed owned job or turn whose release is unproven holds closure, though no
    # record is open: name it, or the report blames the strategy for a release core lacks.
    unreleased = [
        f"{record.request.kind}:{record.request_id.root}"
        for record in unreleased_records(core)
        if record.phase == RequestPhase.COMPLETED
    ]
    if unreleased:
        text += f"; completed records whose release is unproven {unreleased}"
    orphans = [f"{wait.waiter.value} {wait.subject}" for wait in orphan_waits(core)]
    if orphans:
        text += f"; waits nothing will end {orphans}"
    if not open_records and not waiting and not unreleased and not orphans:
        text += f"; {_unfinished(core)}"
    if notes:
        text += f"; {'; '.join(notes)}"
    return text


def _unfinished(core: CoreState) -> str:
    """What has not ended when core waits on nothing: the work left to the strategy."""
    attempts = [
        f"{row.attempt_id.root} {row.phase.value}"
        for row in core.attempts.attempts
        if row.phase != AttemptPhase.TERMINAL
    ]
    sessions = [
        f"{row.spec.session_id.root} {row.phase.value}"
        for row in core.sessions.sessions
        if row.phase not in (SessionPhase.TERMINAL, SessionPhase.IDLE)
    ]
    continuations = [
        f"{row.continuation_id.root} {row.phase.value}"
        for row in core.evaluation.continuations
        if row.phase in (ContinuationPhase.WAITING, ContinuationPhase.AUTHORIZED)
    ]
    return (
        "core waits on nothing, so the strategy is the one that has stopped proposing; "
        f"unfinished attempts {attempts}, sessions {sessions}, continuations {continuations}"
    )
