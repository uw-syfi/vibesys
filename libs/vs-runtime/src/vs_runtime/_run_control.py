"""Thread-safe cooperative run control."""

from __future__ import annotations

import threading
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable


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
        """Queue steering for the next invocation boundary."""
        ...

    def request_pause(self) -> None:
        """Request a pause at the next cooperative boundary."""
        ...

    def resume(self) -> None:
        """Cancel a pause or stop request and release a parked run."""
        ...

    def request_stop(self) -> None:
        """Request an unwind at the next cooperative boundary."""
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

    def raise_if_stopped(self) -> None:
        """Raise :class:`RunStopped` when a stop is pending."""
        ...

    def stop_requested(self) -> bool:
        """Return whether a stop is pending, without landing it."""
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

    def __init__(self, events: RunControlEventSink) -> None:
        """Bind the channel to a synchronous semantic event sink."""
        self._events = events
        self._lock = threading.Condition(threading.Lock())
        self._pending_steer: list[str] = []
        self._paused = False
        self._stop_requested = False
        self._stop_listeners: list[Callable[[], object]] = []

    def queue_steer(self, text: str) -> None:
        """Queue free-text steering for the next invocation boundary."""
        with self._lock:
            self._pending_steer.append(text)
        self._emit(RunControlTransitionKind.STEER_QUEUED, text=text)

    def request_pause(self) -> None:
        """Request that the run park at its next cooperative boundary."""
        with self._lock:
            self._paused = True
        self._emit(RunControlTransitionKind.PAUSE_REQUESTED)

    def resume(self) -> None:
        """Cancel a pending pause or stop and release a parked run."""
        with self._lock:
            self._paused = False
            self._stop_requested = False
            self._lock.notify_all()
        self._emit(RunControlTransitionKind.RESUMED)

    def request_stop(self) -> None:
        """Request that the run unwind at its next cooperative boundary."""
        with self._lock:
            self._stop_requested = True
            self._lock.notify_all()
            listeners = tuple(self._stop_listeners)
        self._emit(RunControlTransitionKind.STOP_REQUESTED)
        for listener in listeners:
            listener()

    def stop_requested(self) -> bool:
        """Return whether a stop is pending, without landing it."""
        with self._lock:
            return self._stop_requested

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
