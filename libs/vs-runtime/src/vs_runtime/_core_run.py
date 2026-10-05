"""The production run loop over ``CoreRuntime``.

The loop owns time and nothing else. Core is pure: it never sleeps and learns time only
through ``ClockAdvanced``. This loop reads an injected ``RunClock``, delivers the time to
core, drains the shell, and sleeps when a drain made no progress.

Pacing. A job that is running but not finished is polled by a request that core issues
each time an observation arrives. Without pacing that cycle runs as fast as the executor
answers (the Fake cluster answers instantly, a real cluster costs a Slurm call each). The
design has three parts, so that a busy loop on a running job cannot happen:

* Core decides when the next observation is due and exposes it as a core time
  (``RunView.next_observe_at``, the default ``next_wake``). Core emits the next observe
  only once ``ClockAdvanced`` reaches that time.
* The loop never calls the executor on its own. Between drains it either made progress
  (state changed) or it sleeps until the earliest of: the next time core asks for, the
  next lease renewal, the run deadline, and the next control poll. Every no-progress
  iteration sleeps at least ``min_sleep`` seconds, so no iteration can spin.
* A dispatch cap fails a cycle that never goes idle, naming the request kinds seen.

Time. The clock is a timeline shared across processes (seconds since the epoch in
production), because lease expiry in the state store is compared across hosts. The loop
makes it non-decreasing and never below the last time core committed.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ClockAdvanced,
    ControlId,
    ControlInput,
    CoreState,
    RecoveryPhase,
    RunControlEvent,
    RunResultProposal,
    RunStatus,
    project,
)

if TYPE_CHECKING:
    from vs_core.api import CoreEvent
    from vs_runtime._core_loop import CoreRuntime, PublicationDelivery
    from vs_runtime._core_requests import ExecutorRefusal
    from vs_runtime._run_control import RunControlChannel


type ControlAction = Literal["pause", "resume", "stop", "steer"]


class RunClock(Protocol):
    """Time source and waiting, injected so tests run without real sleeps."""

    def now(self) -> float:
        """Seconds on a timeline all hosts share."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait about this long (the loop re-reads ``now`` afterwards)."""
        ...


class WallRunClock:
    """Production clock: seconds since the epoch, real sleeping."""

    def now(self) -> float:
        """Seconds since the epoch."""
        return time.time()

    async def sleep(self, seconds: float) -> None:
        """Sleep on the event loop."""
        await asyncio.sleep(seconds)


class NextWake(Protocol):
    """The earliest core time at which a ``ClockAdvanced`` would make core emit work.

    ``None`` means core is waiting for nothing time-driven. The default reads core's
    pacing (``RunView.next_observe_at``).
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
    stop_result: RunResultProposal = field(
        default_factory=lambda: RunResultProposal(
            outcome="cancelled", reason="stop requested by the operator"
        )
    )
    deadline_result: RunResultProposal = field(
        default_factory=lambda: RunResultProposal(
            outcome="cancelled", reason="run deadline reached"
        )
    )

    def __post_init__(self) -> None:
        """Reject intervals that would let the loop spin or lose its lease."""
        for name in ("lease_duration", "control_poll_interval", "min_sleep"):
            if getattr(self, name) <= 0:
                message = f"{name} must be positive"
                raise ValueError(message)
        if self.recovery_poll_interval <= 0 or self.max_dispatches <= 0:
            message = "recovery_poll_interval and max_dispatches must be positive"
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
    so a resumed run never reuses an identity. Stop carries the configured result
    proposal; core never invents one.
    """

    def __init__(
        self,
        channel: RunControlChannel,
        steer: SteerArtifacts,
        *,
        stop_result: RunResultProposal,
    ) -> None:
        """Bind the mailbox, the steer text store and the stop proposal."""
        self._channel = channel
        self._steer = steer
        self._stop_result = stop_result
        self._count = 0
        self._started = False
        self._paused = False
        self._stopped = False

    def poll(self, core: CoreState, now_at: float) -> tuple[RunControlEvent, ...]:
        """Controls requested since the last poll, in submission order."""
        if not self._started:
            self._started = True
            self._count = len(core.run.controls)
            self._paused = core.run.status == RunStatus.PAUSED
            self._stopped = any(c.action == "stop" for c in core.run.controls)
        events: list[RunControlEvent] = [
            self._event("steer", now_at, text=text) for text in self._channel.take_pending_steer()
        ]
        if self._stopped:
            return tuple(events)
        if self._channel.stop_requested():
            self._stopped = True
            events.append(self._event("stop", now_at, result=self._stop_result))
        elif self._channel.pause_requested() != self._paused:
            self._paused = not self._paused
            events.append(self._event("pause" if self._paused else "resume", now_at))
        return tuple(events)

    def _event(
        self,
        action: ControlAction,
        now_at: float,
        *,
        text: str | None = None,
        result: RunResultProposal | None = None,
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
        control = ControlInput(control_id=identity, action=action, artifact=artifact)
        return RunControlEvent(control=control, now_at=now_at, result=result)


@dataclass(frozen=True)
class CoreRunHost:
    """The process-local pieces one run loop drives."""

    shell: CoreRuntime
    delivery: PublicationDelivery
    clock: RunClock
    controls: RunControlBridge | None = None


def start_core(host: CoreRunHost, config: RunLoopConfig) -> None:
    """Take the lease and commit the new-epoch recovery, at the clock's current time."""
    host.shell.start(config.host_id, now_at=host.clock.now(), lease_duration=config.lease_duration)


async def drive_core(
    host: CoreRunHost, config: RunLoopConfig, *, next_wake: NextWake = core_next_wake
) -> RunOutcome:
    """Run a started shell until its run is terminal, an executor is refused, or it fails.

    Each iteration: renew the lease when a third of it has passed, submit controls and
    the deadline stop, deliver the clock, drain the shell, ask the strategy once recovery
    is READY, drain again. An iteration that changed nothing sleeps (see the module
    docstring); one that cannot be woken by time raises ``RunStalledError``.
    """
    return await _Loop(host, config, next_wake).run()


class _Loop:
    def __init__(self, host: CoreRunHost, config: RunLoopConfig, next_wake: NextWake) -> None:
        self._shell = host.shell
        self._delivery = host.delivery
        self._clock = host.clock
        self._controls = host.controls
        self._config = config
        self._next_wake = next_wake
        self._time = self._core().run.now_at
        self._renewed_at = self._read()
        self._deadline_stop = any(
            c.control_id.root == "deadline" for c in self._core().run.controls
        )
        self._first_dispatch = self._shell.dispatched

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
        """The loop body."""
        while self._core().run.status != RunStatus.TERMINAL:
            now = self._read()
            self._renew(now)
            for event in self._due_controls(now):
                self._shell.submit(event, now_at=now)
            self._shell.submit(ClockAdvanced(now_at=now), now_at=now)
            before = self._signature()
            refusal = await self._drain(now)
            if refusal is None and self._core().intents.recovery.phase == RecoveryPhase.READY:
                self._shell.decide(now_at=now)
                refusal = await self._drain(now)
            if refusal is not None:
                return self._outcome(refusal)
            if self._core().run.status == RunStatus.TERMINAL:
                break
            if self._signature() == before:
                await self._wait(now)
        return self._outcome()

    async def _drain(self, now: float) -> ExecutorRefusal | None:
        total = self._shell.dispatched - self._first_dispatch
        remaining = max(self._config.max_dispatches - total, 0)
        return await self._shell.run_until_idle(
            self._delivery, now_at=now, max_dispatches=remaining
        )

    def _renew(self, now: float) -> None:
        if now - self._renewed_at >= self._config.lease_duration / 3:
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
                    control=ControlInput(control_id=ControlId(root="deadline"), action="stop"),
                    now_at=now,
                    result=self._config.deadline_result,
                )
            )
        return tuple(events)

    def _signature(self) -> object:
        """Everything that changes when work happened; time and the revision counter do not."""
        record = self._shell.record
        core = record.envelope.core
        quiet = core.model_copy(
            update={"revision": 0, "run": core.run.model_copy(update={"now_at": 0.0})}
        )
        return (quiet, record.envelope.strategy, record.delivery_cursor, self._shell.dispatched)

    async def _wait(self, now: float) -> None:
        core = self._core()
        due = self._next_wake(core)
        paused = core.run.status == RunStatus.PAUSED
        recovering = core.intents.recovery.phase != RecoveryPhase.READY
        past_deadline = now >= core.run.deadline_at
        if due is None and not paused and (not recovering or past_deadline):
            raise RunStalledError(_describe(core))
        wake = [self._renewed_at + self._config.lease_duration / 3]
        if due is not None:
            wake.append(due)
        if recovering and due is None:
            wake.append(now + self._config.recovery_poll_interval)
        if self._controls is not None:
            wake.append(now + self._config.control_poll_interval)
        if not self._deadline_stop:
            wake.append(core.run.deadline_at)
        await self._clock.sleep(max(min(wake) - now, self._config.min_sleep))


def _describe(core: CoreState) -> str:
    open_intents = [
        f"{intent.request.kind}:{intent.phase.value}"
        for intent in core.intents.intents
        if intent.phase.value != "completed"
    ]
    return (
        f"run {core.run.run_id.root} stalled at core revision {core.revision} "
        f"with status {core.run.status.value}; open intents {open_intents}"
    )
