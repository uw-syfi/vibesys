"""Thread-safe cooperative run control."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

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


class RunControlEventSink(Protocol):
    """Receive one run-control transition before its operation returns."""

    def __call__(self, transition: RunControlTransition) -> object:
        """Record or project one semantic transition."""
        ...


class RunControlChannel(Protocol):
    """Cooperative control mailbox shared by callers and the run thread."""

    def queue_steer(self, text: str) -> None:
        """Queue steering for the next turn.

        The queue is the single source of truth. The run loop drains it
        (:meth:`take_pending_steer`) and turns each message into a durable session input,
        which the next turn of the run receives exactly once.
        """
        ...

    def request_pause(self) -> None:
        """Request a pause at the next cooperative boundary."""
        ...

    def resume(self) -> None:
        """Cancel a pause or stop request and release a parked run."""
        ...

    def resume_with_fallback(self) -> None:
        """Resume like :meth:`resume`, asking a run parked on a provider capacity limit to switch.

        The request stays pending until a parked turn takes it with
        :meth:`consume_fallback_request`. A run with no fallback configured ignores it.
        """
        ...

    def consume_fallback_request(self) -> bool:
        """Take the pending fallback request: True once per request."""
        ...

    def request_stop(self, mode: StopMode) -> None:
        """Request an unwind at the next cooperative boundary, draining or cancelling work."""
        ...

    def take_pending_steer(self) -> list[str]:
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
        self._pending_steer: list[str] = []
        self._paused = False
        self._stop_requested = False
        self._stop_mode: StopMode | None = None
        self._fallback_requested = False
        self._stop_listeners: list[Callable[[], object]] = []

    def queue_steer(self, text: str) -> None:
        """Queue free-text steering for the run loop to take at its next control poll."""
        with self._lock:
            self._pending_steer.append(text)
        self._emit(RunControlTransitionKind.STEER_QUEUED, text=text)

    def request_pause(self) -> None:
        """Request that the run park at its next cooperative boundary.

        The request is published before a boundary can see it, so a run that
        parks concurrently never reports ``PAUSED`` ahead of the request.
        """
        self._emit(RunControlTransitionKind.PAUSE_REQUESTED)
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        """Cancel a pending pause or stop and release a parked run."""
        self._release(fallback=False)

    def _release(self, *, fallback: bool) -> None:
        with self._lock:
            self._paused = False
            self._stop_requested = False
            self._stop_mode = None
            self._fallback_requested = fallback
            self._lock.notify_all()
        self._emit(RunControlTransitionKind.RESUMED)

    def resume_with_fallback(self) -> None:
        """Resume the run and leave a fallback request for the parked turn to take."""
        self._release(fallback=True)

    def consume_fallback_request(self) -> bool:
        """Take the pending fallback request: True once per request."""
        with self._lock:
            requested, self._fallback_requested = self._fallback_requested, False
        return requested

    def request_stop(self, mode: StopMode) -> None:
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

    def take_pending_steer(self) -> list[str]:
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
        if timed_out:
            self._emit(RunControlTransitionKind.RESUMED)
        self.raise_if_stopped()
        return not timed_out

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
    "RunControlChannel",
    "RunControlEventSink",
    "RunControlTransition",
    "RunControlTransitionKind",
    "RunStopped",
    "RuntimeRunControlChannel",
]
