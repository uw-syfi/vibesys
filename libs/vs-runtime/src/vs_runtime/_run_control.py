"""Thread-safe cooperative run control."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import StopMode
    from vs_sim.api import Threads


class RunControlTransitionKind(StrEnum):
    """Closed set of cooperative run-control observations."""

    STEER_QUEUED = "steer_queued"
    PAUSE_REQUESTED = "pause_requested"
    RESUMED = "resumed"
    STOP_REQUESTED = "stop_requested"
    STEER_CONSUMED = "steer_consumed"
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


@dataclass(frozen=True, slots=True)
class CapacityWaitEnd:
    """How one wait for capacity ended, and the request now in force after a ``REARMED``."""

    wake: CapacityWake
    request: int


class RunControlEventSink(Protocol):
    """Receive one run-control transition before its operation returns."""

    def __call__(self, transition: RunControlTransition) -> object:
        """Record or project one semantic transition."""
        ...


@dataclass(frozen=True)
class QueuedSteer:
    """One queued steer: its text and the id of the control that sent it, if it had one."""

    text: str
    control_id: str | None = None


class RunControlChannel(Protocol):
    """Cooperative control mailbox shared by callers and the run thread."""

    def queue_steer(self, text: str, control_id: str | None = None) -> None:
        """Queue steering for the next turn; ``control_id`` names the control that sent it.

        The queue is the single source of truth. The run loop drains it
        (:meth:`take_pending_steer`) and turns each message into a durable session input,
        which the next turn of the run receives exactly once.
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

    def notify_steer_consumed(
        self,
        *,
        agent_kind: str,
        round_label: str,
        execution_id: str | None,
    ) -> None:
        """Record which invocation consumed queued steering."""
        ...

    def wait_while_paused(self) -> None:
        """Park while paused and land a stop that releases the wait."""
        ...

    def wait_resumed(self, timeout: float) -> bool:
        """Park like :meth:`wait_while_paused` for at most *timeout* seconds.

        Returns True when something resumed the run, and False when the timeout
        elapsed first. On a timeout the channel resumes the run itself, atomically,
        so a stop request that arrives at the same moment is never cleared.
        A stop request ends the wait by raising :class:`RunStopped`.
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

    def park_for_capacity(self, *, waiting: bool = False) -> int | None:
        """Park a turn stopped on a capacity limit until the operator resumes it or asks to wait.

        Returns the operator's capacity request (``resume_when_capacity_returns``) when one
        ended the park, and ``None`` when the run was resumed. With ``waiting`` (the turn
        already waited for capacity at the operator's request) it does not park, and returns
        a fresh request. A stop request ends the park by raising :class:`RunStopped`.
        """
        ...

    def wait_for_capacity(self, seconds: float, request: int) -> CapacityWaitEnd:
        """Wait at most ``seconds`` for capacity under ``request``; see :class:`CapacityWake`.

        On ``ELAPSED`` the channel has resumed the run itself, atomically, so a stop arriving
        at that moment is never cleared. A stop request ends the wait by raising
        :class:`RunStopped`.
        """
        ...

    def leave_capacity(self) -> None:
        """The parked turn stopped waiting on its capacity limit: further requests are refused."""
        ...

    def stop_requested(self) -> bool:
        """Return whether a stop is pending, without landing it."""
        ...

    def stop_mode(self) -> StopMode | None:
        """How the pending stop treats work in flight, or ``None`` when no stop is pending."""
        ...

    def pause_requested(self) -> bool:
        """Return whether a pause is pending, without parking on it."""
        ...

    def on_stop_requested(self, listener: Callable[[], object]) -> Callable[[], None]:
        """Call *listener* after each stop request; return its unsubscribe callable.

        The listener runs on the requesting thread, after the request is
        recorded and published, and must not block.
        """
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
        self._paused = False
        self._stop_requested = False
        self._stop_mode: StopMode | None = None
        self._requested_by: dict[Literal["pause", "stop"], str | None] = {}
        self._fallback_requested = False
        self._stop_listeners: list[Callable[[], object]] = []
        self._capacity_listening = False
        self._capacity_request: int | None = None
        self._capacity_requests = 0

    def queue_steer(self, text: str, control_id: str | None = None) -> None:
        """Queue free-text steering for the run loop to take at its next control poll."""
        with self._lock:
            self._pending_steer.append(QueuedSteer(text, control_id))
        self._emit(RunControlTransitionKind.STEER_QUEUED, text=text)

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
            self._paused = False
            self._requested_by = {"pause": control_id}
            self._stop_requested = False
            self._stop_mode = None
            self._fallback_requested = fallback
            self._capacity_request = None
            self._lock.notify_all()
        self._emit(RunControlTransitionKind.RESUMED)

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
        """Return whether a pause is pending, without parking on it."""
        with self._lock:
            return self._paused

    def on_stop_requested(self, listener: Callable[[], object]) -> Callable[[], None]:
        """Call *listener* after each stop request; return its unsubscribe callable."""
        with self._lock:
            self._stop_listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._stop_listeners:
                    self._stop_listeners.remove(listener)

        return unsubscribe

    def take_pending_steer(self) -> list[QueuedSteer]:
        """Drain steering queued since the previous invocation boundary."""
        with self._lock:
            pending, self._pending_steer = self._pending_steer, []
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

    def wait_while_paused(self) -> None:
        """Park while paused, then land any stop that released the wait."""
        with self._lock:
            should_emit_paused = self._paused and not self._stop_requested
        if should_emit_paused:
            self._emit(RunControlTransitionKind.PAUSED)
        with self._lock:
            while self._paused and not self._stop_requested:
                self._lock.wait()
        self.raise_if_stopped()

    def wait_resumed(self, timeout: float) -> bool:
        """Park for at most *timeout* seconds; resume the run itself if the timeout elapses."""
        with self._lock:
            should_emit_paused = self._paused and not self._stop_requested
        if should_emit_paused:
            self._emit(RunControlTransitionKind.PAUSED)
        with self._lock:
            self._lock.wait_for(lambda: not self._paused or self._stop_requested, timeout)
            timed_out = self._paused and not self._stop_requested
            if timed_out:
                self._paused = False
                self._requested_by["pause"] = None
        if timed_out:
            self._emit(RunControlTransitionKind.RESUMED)
        self.raise_if_stopped()
        return not timed_out

    def resume_when_capacity_returns(self) -> bool:
        """Arm a resume for when capacity returns; False when no turn waits on a capacity limit."""
        with self._lock:
            if not (self._capacity_listening and self._paused and not self._stop_requested):
                return False
            self._capacity_request = self._next_capacity_request()
            self._lock.notify_all()
        return True

    def park_for_capacity(self, *, waiting: bool = False) -> int | None:
        """Park until resumed (``None``) or asked to wait for capacity (the request)."""
        with self._lock:
            self._capacity_listening = True
            should_emit_paused = self._paused and not self._stop_requested
        if should_emit_paused:
            self._emit(RunControlTransitionKind.PAUSED)
        with self._lock:
            if waiting and self._paused and not self._stop_requested:
                self._capacity_request = self._next_capacity_request()
            while self._paused and not self._stop_requested and self._capacity_request is None:
                self._lock.wait()
            request = self._capacity_request if self._paused else None
        self.raise_if_stopped()
        return request

    def wait_for_capacity(self, seconds: float, request: int) -> CapacityWaitEnd:
        """Wait for capacity under ``request``, ending as :class:`CapacityWake` says."""
        with self._lock:
            self._lock.wait_for(
                lambda: (
                    self._stop_requested or not self._paused or self._capacity_request != request
                ),
                seconds,
            )
            end = self._capacity_end(request)
            if end.wake is CapacityWake.ELAPSED:
                self._paused = False
                self._requested_by["pause"] = None
                self._capacity_request = None
        if end.wake is CapacityWake.ELAPSED:
            self._emit(RunControlTransitionKind.RESUMED)
        self.raise_if_stopped()
        return end

    def leave_capacity(self) -> None:
        """Refuse capacity requests until a turn parks on a capacity limit again."""
        with self._lock:
            self._capacity_listening = False
            self._capacity_request = None

    def _capacity_end(self, request: int) -> CapacityWaitEnd:
        """How the wait for ``request`` stands; the caller holds the lock."""
        if self._stop_requested:
            return CapacityWaitEnd(CapacityWake.CANCELLED, request)
        if not self._paused:
            return CapacityWaitEnd(CapacityWake.RESUMED, request)
        if self._capacity_request is None:
            return CapacityWaitEnd(CapacityWake.CANCELLED, request)
        if self._capacity_request != request:
            return CapacityWaitEnd(CapacityWake.REARMED, self._capacity_request)
        return CapacityWaitEnd(CapacityWake.ELAPSED, request)

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
    "QueuedSteer",
    "RunControlChannel",
    "RunControlEventSink",
    "RunControlTransition",
    "RunControlTransitionKind",
    "RunStopped",
    "RuntimeRunControlChannel",
]
