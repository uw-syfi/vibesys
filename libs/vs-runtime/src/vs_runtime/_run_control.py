"""Thread-safe cooperative run control."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from vs_runtime._operator_control import SteerMode

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import StopMode
    from vs_runtime._operator_control import SteerTarget
    from vs_sim.api import Threads


class RunControlTransitionKind(StrEnum):
    """Closed set of cooperative run-control observations."""

    STEER_QUEUED = "steer_queued"
    GUIDANCE_QUEUED = "guidance_queued"
    """A standing steer was queued: guidance in force until withdrawn, not one turn's."""
    PAUSE_REQUESTED = "pause_requested"
    RESUMED = "resumed"
    STOP_REQUESTED = "stop_requested"
    STEER_CONSUMED = "steer_consumed"
    STEER_DROPPED = "steer_dropped"
    WITHDRAW_QUEUED = "withdraw_queued"
    GUIDANCE_ENDED = "guidance_ended"
    """Standing guidance reaches no turn any more: withdrawn, or its target or the run ended."""
    PAUSED = "paused"
    STOPPED = "stopped"


class RunControlTransition(BaseModel):
    """One immutable run-control transition emitted synchronously."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RunControlTransitionKind
    text: str = ""
    agent_kind: str | None = None
    round_label: str | None = None
    execution_id: str | None = None


class PauseWake(StrEnum):
    """Why one turn's bounded wait on the run's pause ended."""

    RESUMED = "resumed"
    """Something other than the run's timed resume (the operator) resumed the run."""
    ELAPSED = "elapsed"
    """The run's timed resume came due: the channel resumed the run itself."""
    DETACHED = "detached"
    """The waiting turn left (it was cancelled); the run's pause state is unchanged."""


class CapacityWake(StrEnum):
    """Why a wait for provider capacity, which the operator asked for, ended."""

    ELAPSED = "elapsed"
    """Capacity should be back: the channel resumed the run itself."""
    RESUMED = "resumed"
    """The operator resumed the run now, ending the wait."""
    CANCELLED = "cancelled"
    """The operator paused the run again: it waits for the operator, not for capacity."""
    REARMED = "rearmed"
    """The operator asked again: the newer request replaces the wait."""
    DETACHED = "detached"
    """The waiting turn left (it was cancelled); the run and the request are unchanged."""


@dataclass(frozen=True, slots=True)
class CapacityWaitEnd:
    """How one wait for capacity ended, and the request now in force after a ``REARMED``."""

    wake: CapacityWake
    request: int


def _never() -> bool:
    """The *ended* condition of a wait only the channel's own state ends."""
    return False


class RunControlEventSink(Protocol):
    """Receive one run-control transition before its operation returns."""

    def __call__(self, transition: RunControlTransition) -> object:
        """Record or project one semantic transition."""
        ...


@dataclass(frozen=True)
class QueuedSteer:
    """One queued steer: its text, the id of the control that sent it, whom it is for and how.

    A steer with no target is for the whole run.
    """

    text: str
    control_id: str | None = None
    target: SteerTarget | None = None
    mode: SteerMode = SteerMode.NEXT_TURN


@dataclass(frozen=True)
class QueuedWithdraw:
    """One queued withdrawal: the standing steer it ends and the id of the control that sent it."""

    withdraws: str
    control_id: str


class RunControlChannel(Protocol):
    """Cooperative control mailbox shared by callers and the run thread."""

    def queue_steer(
        self,
        text: str,
        control_id: str | None = None,
        target: SteerTarget | None = None,
        mode: SteerMode = SteerMode.NEXT_TURN,
    ) -> None:
        """Queue steering for the turns of ``target`` (the run when None), as ``mode`` says.

        ``control_id`` names the control that sent it. The queue is the single source of
        truth. The run loop drains it (:meth:`take_pending_steer`) and turns each message into
        a durable session input, which the next matching turn receives exactly once, or every
        matching turn while it stands; one that reaches no turn is reported
        (:meth:`notify_steer_dropped`).
        """
        ...

    def queue_withdraw(self, withdraws: str, control_id: str) -> None:
        """Queue the end of the standing steer sent under ``withdraws``.

        The run loop drains withdrawals (:meth:`take_pending_withdrawals`) after the steers
        queued with them, so a steer and its withdrawal taken in one poll apply in that order.
        """
        ...

    def request_pause(self, control_id: str | None = None) -> None:
        """Request a pause at the next cooperative boundary; ``control_id`` names its control."""
        ...

    def resume(self, control_id: str | None = None) -> None:
        """Cancel a pause or stop request and release a parked run."""
        ...

    def requested_by(self, flag: Literal["pause", "stop"]) -> str | None:
        """The id of the control that last set ``flag``, or None when it had none.

        ``pause`` names the latest pause or resume (the run resuming itself has none), and
        ``stop`` the pending stop, so the run records each under the id its sender gave.
        """
        ...

    def land_pause(self) -> None:
        """Report that a pause has landed: the run is held paused and nothing of it runs.

        Emits ``PAUSED`` when a pause is in force and no stop is pending; otherwise nothing,
        so a resume or a stop that came first is never reported as a pause. The core run loop
        calls it, as a parked turn reports its own pause.
        """
        ...

    def resume_with_fallback(self, control_id: str | None = None) -> None:
        """Resume like :meth:`resume`, asking a run parked on a provider capacity limit to switch.

        The request stays pending until a parked turn takes it with
        :meth:`consume_fallback_request`. A run with no fallback configured ignores it.
        """
        ...

    def consume_fallback_request(self) -> bool:
        """Take the pending fallback request: True once per request."""
        ...

    def request_stop(self, mode: StopMode, control_id: str | None = None) -> None:
        """Request an unwind at the next cooperative boundary, draining or cancelling work."""
        ...

    def take_pending_steer(self) -> list[QueuedSteer]:
        """Drain queued steering exactly once."""
        ...

    def take_pending_withdrawals(self) -> list[QueuedWithdraw]:
        """Drain queued withdrawals exactly once."""
        ...

    def notify_steer_consumed(
        self,
        *,
        agent_kind: str,
        round_label: str,
        execution_id: str | None,
    ) -> None:
        """Record which invocation consumed queued steering."""
        ...

    def notify_steer_dropped(self, reason: str) -> None:
        """Record that an accepted steer reached no turn; ``reason`` names the steer and why.

        Its target ended or became unknown before the run took it, or before a turn of it ran.
        """
        ...

    def notify_guidance_ended(self, reason: str) -> None:
        """Record that standing guidance reaches no turn any more; ``reason`` names it and why.

        It was withdrawn, its target or the run ended, or its attempt was reopened.
        """
        ...

    def wait_while_paused(self) -> None:
        """Park while paused and land a stop that releases the wait.

        The run's timed resume (see :meth:`wait_resumed`) ends the wait when it comes due.
        """
        ...

    def wait_resumed(self, timeout: float, *, ended: Callable[[], bool] = _never) -> PauseWake:
        """Arm the run's timed resume *timeout* seconds from now, and park until the run resumes.

        The timed resume belongs to the channel, not to this wait: the earliest one armed
        wins, and any waiter or a :meth:`pause_requested` poll resumes the run when it comes
        due (``ELAPSED``), atomically, so a stop request that arrives at the same moment is
        never cleared. ``RESUMED`` says something else resumed the run. *ended* (rechecked
        when :meth:`interrupt_waits` wakes the wait) ends only this wait, as ``DETACHED``:
        the pause, its timed resume and every other waiter stay as they were. A stop request
        ends the wait by raising :class:`RunStopped`.
        """
        ...

    def raise_if_stopped(self) -> None:
        """Raise :class:`RunStopped` when a stop is pending."""
        ...

    def resume_when_capacity_returns(self) -> bool:
        """Ask a run parked on a provider capacity limit to resume itself once capacity returns.

        False, and nothing changes, when no turn is parked on a capacity limit. Asking again
        replaces the earlier request; ``resume`` ends the wait at once and ``request_pause``
        cancels it, leaving the run paused for the operator.
        """
        ...

    def park_for_capacity(
        self, *, waiting: bool = False, ended: Callable[[], bool] = _never
    ) -> int | None:
        """Park a turn stopped on a capacity limit until the operator resumes it or asks to wait.

        Returns the operator's capacity request (``resume_when_capacity_returns``) when one
        ended the park, and ``None`` when the run was resumed or *ended* became true (then
        the run stays paused for the operator). With ``waiting`` (the turn already waited
        for capacity at the operator's request) it does not park, and returns a fresh
        request. A stop request ends the park by raising :class:`RunStopped`.
        """
        ...

    def wait_for_capacity(
        self, seconds: float, request: int, *, ended: Callable[[], bool] = _never
    ) -> CapacityWaitEnd:
        """Wait at most ``seconds`` for capacity under ``request``; see :class:`CapacityWake`.

        On ``ELAPSED`` the channel has resumed the run itself, atomically, so a stop arriving
        at that moment is never cleared. *ended* becoming true ends only this wait, as
        ``DETACHED``, changing nothing else. A stop request ends the wait by raising
        :class:`RunStopped`.
        """
        ...

    def interrupt_waits(self) -> None:
        """Wake every parked wait, so each rechecks its *ended* condition.

        Nothing else changes: a wait whose condition still holds parks again.
        """
        ...

    def enter_capacity(self) -> None:
        """A turn stopped on a capacity limit: the operator may ask the run to wait for capacity.

        Pairs with one :meth:`leave_capacity`; several turns may be in at once.
        """
        ...

    def leave_capacity(self) -> None:
        """One turn stopped waiting on its capacity limit.

        When it was the last such turn, further requests are refused and the pending one
        is dropped; while others remain, both stay as they are.
        """
        ...

    def stop_requested(self) -> bool:
        """Return whether a stop is pending, without landing it."""
        ...

    def stop_mode(self) -> StopMode | None:
        """How the pending stop treats work in flight, or ``None`` when no stop is pending."""
        ...

    def pause_requested(self) -> bool:
        """Return whether a pause is pending, without parking on it.

        The run loop polls it, so it resumes the run first when the run's timed resume
        (see :meth:`wait_resumed`) is due, even when no turn waits on it any more.
        """
        ...

    def on_stop_requested(self, listener: Callable[[], object]) -> Callable[[], None]:
        """Call *listener* after each stop request; return its unsubscribe callable.

        The listener runs on the requesting thread, after the request is
        recorded and published, and must not block.
        """
        ...

    def set_run_deadline(self, at: float) -> None:
        """Record when the run must end, in epoch seconds on the run clock's timeline.

        The run loop records it as it starts, so work the run does on other threads (a
        turn waiting out a provider outage) can bound its own waits by it.
        """
        ...

    def run_deadline(self) -> float | None:
        """When the run must end, or ``None`` before the run loop recorded it."""
        ...


class RunStopped(BaseException):
    """A requested stop landed at a cooperative run boundary."""


class RuntimeRunControlChannel:
    """Thread-safe mailbox between control callers and a running policy."""

    def __init__(self, events: RunControlEventSink, threads: Threads) -> None:
        """Bind the channel to a synchronous semantic event sink; ``threads`` makes its lock."""
        self._events = events
        self._lock = threads.condition(threads.lock())
        self._pending_steer: list[QueuedSteer] = []
        self._pending_withdrawals: list[QueuedWithdraw] = []
        self._paused = False
        self._stop_requested = False
        self._stop_mode: StopMode | None = None
        self._requested_by: dict[Literal["pause", "stop"], str | None] = {}
        self._fallback_requested = False
        self._stop_listeners: list[Callable[[], object]] = []
        self._now = threads.now
        self._capacity_turns = 0
        self._capacity_request: int | None = None
        self._capacity_requests = 0
        self._run_deadline: float | None = None
        # The run's timed resume, on the ``threads.now`` timeline; ``None`` unless paused.
        self._resume_at: float | None = None
        self._resumed_by = PauseWake.RESUMED

    def queue_steer(
        self,
        text: str,
        control_id: str | None = None,
        target: SteerTarget | None = None,
        mode: SteerMode = SteerMode.NEXT_TURN,
    ) -> None:
        """Queue free-text steering for the run loop to take at its next control poll."""
        with self._lock:
            self._pending_steer.append(QueuedSteer(text, control_id, target, mode))
        self._emit(
            RunControlTransitionKind.GUIDANCE_QUEUED
            if mode is SteerMode.STANDING
            else RunControlTransitionKind.STEER_QUEUED,
            text=text,
        )

    def queue_withdraw(self, withdraws: str, control_id: str) -> None:
        """Queue a withdrawal for the run loop to take at its next control poll."""
        with self._lock:
            self._pending_withdrawals.append(QueuedWithdraw(withdraws, control_id))
        self._emit(RunControlTransitionKind.WITHDRAW_QUEUED, text=withdraws)

    def request_pause(self, control_id: str | None = None) -> None:
        """Request that the run park at its next cooperative boundary.

        The request is published before a boundary can see it, so a run that
        parks concurrently never reports ``PAUSED`` ahead of the request.
        """
        self._emit(RunControlTransitionKind.PAUSE_REQUESTED)
        with self._lock:
            self._paused = True
            self._requested_by["pause"] = control_id
            self._capacity_request = None
            self._lock.notify_all()

    def resume(self, control_id: str | None = None) -> None:
        """Cancel a pending pause or stop and release a parked run."""
        self._release(fallback=False, control_id=control_id)

    def requested_by(self, flag: Literal["pause", "stop"]) -> str | None:
        """The id of the control that last set ``flag``, or None when it had none."""
        with self._lock:
            return self._requested_by.get(flag)

    def land_pause(self) -> None:
        """Emit ``PAUSED`` while a pause is in force and no stop is pending."""
        with self._lock:
            landed = self._paused and not self._stop_requested
        if landed:
            self._emit(RunControlTransitionKind.PAUSED)

    def _release(self, *, fallback: bool, control_id: str | None) -> None:
        with self._lock:
            self._unpause(PauseWake.RESUMED)
            self._requested_by = {"pause": control_id}
            self._stop_requested = False
            self._stop_mode = None
            self._fallback_requested = fallback
        self._emit(RunControlTransitionKind.RESUMED)

    def _unpause(self, cause: PauseWake) -> None:
        """Resume the run and wake every waiter; the caller holds the lock and emits RESUMED."""
        self._paused = False
        self._resumed_by = cause
        self._resume_at = None
        self._capacity_request = None
        # The run resuming itself has no control id; an operator resume records its own after.
        self._requested_by["pause"] = None
        self._lock.notify_all()

    def _fire_due(self) -> bool:
        """Resume the run if its timed resume is due; the caller holds the lock and emits."""
        due = (
            self._paused
            and not self._stop_requested
            and self._resume_at is not None
            and self._now() >= self._resume_at
        )
        if due:
            self._unpause(PauseWake.ELAPSED)
        return due

    def _wait_until_due(self, until: float | None = None) -> None:
        """Park on the condition until notified, the timed resume, or *until*; lock held."""
        deadlines = [at for at in (self._resume_at, until) if at is not None]
        if not deadlines:
            self._lock.wait()
            return
        self._lock.wait(max(min(deadlines) - self._now(), 0.0))

    def resume_with_fallback(self, control_id: str | None = None) -> None:
        """Resume the run and leave a fallback request for the parked turn to take."""
        self._release(fallback=True, control_id=control_id)

    def consume_fallback_request(self) -> bool:
        """Take the pending fallback request: True once per request."""
        with self._lock:
            requested, self._fallback_requested = self._fallback_requested, False
        return requested

    def request_stop(self, mode: StopMode, control_id: str | None = None) -> None:
        """Request that the run unwind at its next cooperative boundary, as ``mode`` says.

        The request is published before a boundary can see it, so a run that
        reaches a boundary concurrently never reports ``STOPPED`` ahead of the
        request: a consumer folding the events would otherwise see a stop land
        on a run that was never stopping.
        """
        self._emit(RunControlTransitionKind.STOP_REQUESTED)
        with self._lock:
            self._stop_requested = True
            self._stop_mode = mode
            self._requested_by["stop"] = control_id
            self._lock.notify_all()
            listeners = tuple(self._stop_listeners)
        for listener in listeners:
            listener()

    def stop_requested(self) -> bool:
        """Return whether a stop is pending, without landing it."""
        with self._lock:
            return self._stop_requested

    def stop_mode(self) -> StopMode | None:
        """How the pending stop treats work in flight, or ``None`` when none is pending."""
        with self._lock:
            return self._stop_mode if self._stop_requested else None

    def pause_requested(self) -> bool:
        """Return whether a pause is pending, without parking on it.

        This is the run loop's poll, so it also fires the run's timed resume once due.
        """
        with self._lock:
            fired = self._fire_due()
            paused = self._paused
        if fired:
            self._emit(RunControlTransitionKind.RESUMED)
        return paused

    def on_stop_requested(self, listener: Callable[[], object]) -> Callable[[], None]:
        """Call *listener* after each stop request; return its unsubscribe callable."""
        with self._lock:
            self._stop_listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._stop_listeners:
                    self._stop_listeners.remove(listener)

        return unsubscribe

    def set_run_deadline(self, at: float) -> None:
        """Record when the run must end."""
        with self._lock:
            self._run_deadline = at

    def run_deadline(self) -> float | None:
        """When the run must end, or ``None`` before it was recorded."""
        with self._lock:
            return self._run_deadline

    def take_pending_steer(self) -> list[QueuedSteer]:
        """Drain steering queued since the previous invocation boundary."""
        with self._lock:
            pending, self._pending_steer = self._pending_steer, []
        return pending

    def take_pending_withdrawals(self) -> list[QueuedWithdraw]:
        """Drain withdrawals queued since the previous control poll."""
        with self._lock:
            pending, self._pending_withdrawals = self._pending_withdrawals, []
        return pending

    def notify_steer_consumed(
        self,
        *,
        agent_kind: str,
        round_label: str,
        execution_id: str | None,
    ) -> None:
        """Record the invocation identity that consumed queued steering."""
        self._events(
            RunControlTransition(
                kind=RunControlTransitionKind.STEER_CONSUMED,
                agent_kind=agent_kind,
                round_label=round_label,
                execution_id=execution_id,
            )
        )

    def notify_steer_dropped(self, reason: str) -> None:
        """Record why a queued steer reached no one."""
        self._events(RunControlTransition(kind=RunControlTransitionKind.STEER_DROPPED, text=reason))

    def notify_guidance_ended(self, reason: str) -> None:
        """Record that standing guidance reaches no turn any more, and why."""
        self._events(
            RunControlTransition(kind=RunControlTransitionKind.GUIDANCE_ENDED, text=reason)
        )

    def wait_while_paused(self) -> None:
        """Park while paused, then land any stop that released the wait."""
        with self._lock:
            should_emit_paused = self._paused and not self._stop_requested
        if should_emit_paused:
            self._emit(RunControlTransitionKind.PAUSED)
        fired = False
        with self._lock:
            while self._paused and not self._stop_requested:
                if self._fire_due():
                    fired = True
                    break
                self._wait_until_due()
        if fired:
            self._emit(RunControlTransitionKind.RESUMED)
        self.raise_if_stopped()

    def wait_resumed(self, timeout: float, *, ended: Callable[[], bool] = _never) -> PauseWake:
        """Arm the run's timed resume and park until the run resumes or *ended* (``DETACHED``)."""
        with self._lock:
            should_emit_paused = self._paused and not self._stop_requested
        if should_emit_paused:
            self._emit(RunControlTransitionKind.PAUSED)
        fired = False
        with self._lock:
            if ended():
                wake = PauseWake.DETACHED
            else:
                if self._paused and not self._stop_requested:
                    at = self._now() + timeout
                    self._resume_at = at if self._resume_at is None else min(self._resume_at, at)
                while True:
                    if self._stop_requested:
                        wake = PauseWake.RESUMED
                        break
                    if self._fire_due():
                        fired, wake = True, PauseWake.ELAPSED
                        break
                    if not self._paused:
                        wake = self._resumed_by
                        break
                    if ended():
                        wake = PauseWake.DETACHED
                        break
                    self._wait_until_due()
        if fired:
            self._emit(RunControlTransitionKind.RESUMED)
        self.raise_if_stopped()
        return wake

    def resume_when_capacity_returns(self) -> bool:
        """Arm a resume for when capacity returns; False when no turn waits on a capacity limit."""
        with self._lock:
            if not (self._capacity_turns and self._paused and not self._stop_requested):
                return False
            self._capacity_request = self._next_capacity_request()
            self._lock.notify_all()
        return True

    def park_for_capacity(
        self, *, waiting: bool = False, ended: Callable[[], bool] = _never
    ) -> int | None:
        """Park until resumed or *ended* (``None``) or asked to wait for capacity (the request)."""
        with self._lock:
            should_emit_paused = self._paused and not self._stop_requested
        if should_emit_paused:
            self._emit(RunControlTransitionKind.PAUSED)
        fired = False
        with self._lock:
            if waiting and self._paused and not self._stop_requested:
                self._capacity_request = self._next_capacity_request()
            while (
                self._paused
                and not self._stop_requested
                and self._capacity_request is None
                and not ended()
            ):
                if self._fire_due():
                    fired = True
                    break
                self._wait_until_due()
            request = self._capacity_request if self._paused and not ended() else None
        if fired:
            self._emit(RunControlTransitionKind.RESUMED)
        self.raise_if_stopped()
        return request

    def wait_for_capacity(
        self, seconds: float, request: int, *, ended: Callable[[], bool] = _never
    ) -> CapacityWaitEnd:
        """Wait for capacity under ``request``, ending as :class:`CapacityWake` says."""
        fired = False
        with self._lock:
            until = self._now() + seconds
            while True:
                fired = self._fire_due() or fired
                if self._stop_requested or not self._paused or self._capacity_request != request:
                    end = self._capacity_end(request)
                    break
                if ended():
                    end = CapacityWaitEnd(CapacityWake.DETACHED, request)
                    break
                if self._now() >= until:
                    self._unpause(PauseWake.ELAPSED)
                    fired, end = True, CapacityWaitEnd(CapacityWake.ELAPSED, request)
                    break
                self._wait_until_due(until)
        if fired:
            self._emit(RunControlTransitionKind.RESUMED)
        self.raise_if_stopped()
        return end

    def interrupt_waits(self) -> None:
        """Wake every parked wait to recheck its *ended* condition."""
        with self._lock:
            self._lock.notify_all()

    def enter_capacity(self) -> None:
        """Count one more turn stopped on a capacity limit."""
        with self._lock:
            self._capacity_turns += 1

    def leave_capacity(self) -> None:
        """Count one turn out; the last one out refuses further requests and drops the pending one."""
        with self._lock:
            self._capacity_turns = max(self._capacity_turns - 1, 0)
            if not self._capacity_turns:
                self._capacity_request = None

    def _capacity_end(self, request: int) -> CapacityWaitEnd:
        """How the wait for ``request`` stands; the caller holds the lock."""
        if self._stop_requested:
            return CapacityWaitEnd(CapacityWake.CANCELLED, request)
        if not self._paused:
            timed = self._resumed_by is PauseWake.ELAPSED
            return CapacityWaitEnd(CapacityWake.ELAPSED if timed else CapacityWake.RESUMED, request)
        if self._capacity_request is None:
            return CapacityWaitEnd(CapacityWake.CANCELLED, request)
        return CapacityWaitEnd(CapacityWake.REARMED, self._capacity_request)

    def _next_capacity_request(self) -> int:
        self._capacity_requests += 1
        return self._capacity_requests

    def raise_if_stopped(self) -> None:
        """Land and raise a requested stop at the current boundary."""
        with self._lock:
            stopped = self._stop_requested
        if not stopped:
            return
        self._emit(RunControlTransitionKind.STOPPED)
        raise RunStopped

    def _emit(self, kind: RunControlTransitionKind, *, text: str = "") -> None:
        self._events(RunControlTransition(kind=kind, text=text))


__all__ = [
    "CapacityWaitEnd",
    "CapacityWake",
    "PauseWake",
    "QueuedSteer",
    "QueuedWithdraw",
    "RunControlChannel",
    "RunControlEventSink",
    "RunControlTransition",
    "RunControlTransitionKind",
    "RunStopped",
    "RuntimeRunControlChannel",
]
