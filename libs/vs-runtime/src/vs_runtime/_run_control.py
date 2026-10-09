"""Thread-safe cooperative run control."""

from __future__ import annotations

import threading
from enum import StrEnum
from functools import partial
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
    STEER_DELIVERED = "steer_delivered"
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


class SteerTarget(Protocol):
    """An agent turn in flight that may take an operator message before it ends."""

    @property
    def agent_kind(self) -> str:
        """Name the agent running the turn."""
        ...

    @property
    def round_label(self) -> str:
        """Name the round the turn belongs to."""
        ...

    @property
    def execution_id(self) -> str | None:
        """Name the execution the turn runs as."""
        ...

    def offer_steer(self, text: str, on_rejected: Callable[[], None]) -> bool:
        """Offer *text* to the turn now; ``True`` when the provider took it.

        When the provider takes it and refuses it later, *on_rejected* is
        called once so the channel can queue the text for the next boundary.
        """
        ...


class RunControlChannel(Protocol):
    """Cooperative control mailbox shared by callers and the run thread."""

    def queue_steer(self, text: str) -> None:
        """Queue steering; deliver it now to a turn in flight that can take it.

        The queue is the single source of truth. A message is drained at the
        next invocation boundary (:meth:`take_pending_steer`) unless an attached
        target takes it first, in which case it leaves the queue and the
        channel records ``STEER_DELIVERED``.
        """
        ...

    def attach_steer_target(self, target: SteerTarget) -> Callable[[], None]:
        """Let *target* take queued steering until the returned detach is called.

        With several targets attached the one whose turn began first is offered
        the message; a message a target declines stays queued.
        """
        ...

    def requeue_steer(self, text: str) -> None:
        """Put a message a provider refused back at the head of the queue."""
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

    def __init__(self, events: RunControlEventSink) -> None:
        """Bind the channel to a synchronous semantic event sink."""
        self._events = events
        self._lock = threading.Condition(threading.Lock())
        self._pending_steer: list[str] = []
        self._steer_targets: list[SteerTarget] = []
        self._paused = False
        self._stop_requested = False
        self._stop_listeners: list[Callable[[], object]] = []

    def queue_steer(self, text: str) -> None:
        """Queue free-text steering, delivering it now when a running turn can take it."""
        with self._lock:
            self._pending_steer.append(text)
        self._emit(RunControlTransitionKind.STEER_QUEUED, text=text)
        self._offer_pending_steer()

    def attach_steer_target(self, target: SteerTarget) -> Callable[[], None]:
        """Let *target* take queued steering until the returned detach is called."""
        with self._lock:
            self._steer_targets.append(target)

        def detach() -> None:
            with self._lock:
                if target in self._steer_targets:
                    self._steer_targets.remove(target)

        return detach

    def requeue_steer(self, text: str) -> None:
        """Queue a refused message again, ahead of newer ones, for the next boundary."""
        with self._lock:
            self._pending_steer.insert(0, text)
        self._emit(RunControlTransitionKind.STEER_QUEUED, text=text)

    def _offer_pending_steer(self) -> None:
        """Hand queued messages, oldest first, to the oldest target until one is declined."""
        while True:
            with self._lock:
                if not self._steer_targets or not self._pending_steer:
                    return
                target = self._steer_targets[0]
                text = self._pending_steer.pop(0)
            if not target.offer_steer(text, partial(self.requeue_steer, text)):
                with self._lock:
                    self._pending_steer.insert(0, text)
                return
            self._events(
                RunControlTransition(
                    kind=RunControlTransitionKind.STEER_DELIVERED,
                    text=text,
                    agent_kind=target.agent_kind,
                    round_label=target.round_label,
                    execution_id=target.execution_id,
                )
            )

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
    "SteerTarget",
]
