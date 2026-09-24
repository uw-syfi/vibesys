"""Core-owned run control: the channel `RunControl` writes to, the run loop reads.

`vibesys.api.RunControl.steer/pause/resume/stop` are messages to the run
loop, the sole writer of run state (see `vibesys.api.session.RunControl`).
`RunControlChannel` is the mailbox: writer-side methods (`queue_steer`,
`request_pause`, `resume`, `request_stop`) queue a request and emit its
request-time `CoreEventType`, unconditionally, from whatever thread issues
it. Reader-side methods (`raise_if_stopped`, `wait_while_paused`,
`take_pending_steer`), called by `RunContext.control` and agent turns,
consume that state and emit the matching boundary-consume-time
event only when landing it actually changes something. Emission is
synchronous (`EventJournal.emit` calls subscribers before returning), so a
server projecting these events onto its own status machine sees them in the
same order the channel applied them, and can rely on a stop already being
landed by the time `raise_if_stopped` raises.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from vibesys.events import CoreEventType

if TYPE_CHECKING:
    from vibesys.run.event_journal import EventJournal


class RunStopped(BaseException):
    """A `/stop` landed: unwind the run callable.

    Raised on the run's own thread, from `RunControlChannel.raise_if_stopped`,
    once the channel has already applied the stop and emitted
    `CoreEventType.STOPPED`. Derives from `BaseException`, like
    `KeyboardInterrupt`, so an `except Exception` inside loop code cannot
    absorb the unwind on its way out to the run's caller.
    """


class RunControlChannel:
    """Thread-safe mailbox between `RunControl` callers and the run loop.

    One instance per run, held on `LocalRunIntegration.control`. Writer methods
    run on whatever thread issues a control message (a server transport
    thread, for example) and always emit their request-time event, whether
    or not the request changes anything -- a repeated pause request still
    gets a distinct audit entry, matching what `RunController` did before
    this state moved here. Reader methods run at policy and agent turn
    boundaries.
    """

    def __init__(self, events: EventJournal) -> None:
        """Bind the channel to the run's own event journal."""
        self._events = events
        self._lock = threading.Condition(threading.Lock())
        self._pending_steer: list[str] = []
        self._paused = False
        self._stop_requested = False

    def queue_steer(self, text: str) -> None:
        """Queue free-text steering for the next invocation boundary."""
        with self._lock:
            self._pending_steer.append(text)
        self._events.emit(CoreEventType.STEER_QUEUED, text)

    def request_pause(self) -> None:
        """Request the run reach its next boundary and release the write lease."""
        with self._lock:
            self._paused = True
        self._events.emit(CoreEventType.PAUSE_REQUESTED)

    def resume(self) -> None:
        """Cancel a pending pause or stop and let the run continue."""
        with self._lock:
            self._paused = False
            self._stop_requested = False
            self._lock.notify_all()
        self._events.emit(CoreEventType.RESUMED)

    def request_stop(self) -> None:
        """Request the run terminate at its next boundary."""
        with self._lock:
            self._stop_requested = True
            self._lock.notify_all()
        self._events.emit(CoreEventType.STOP_REQUESTED)

    def take_pending_steer(self) -> list[str]:
        """Drain and return steering queued since the last boundary."""
        with self._lock:
            pending, self._pending_steer = self._pending_steer, []
        return pending

    def notify_steer_consumed(
        self, *, agent_kind: str, round_label: str, execution_id: str | None
    ) -> None:
        """Record that drained steering was spliced into an invocation's prompt."""
        self._events.emit(
            CoreEventType.STEER_CONSUMED,
            agent_kind=agent_kind,
            round_label=round_label,
            execution_id=execution_id,
        )

    def wait_while_paused(self) -> None:
        """Block the run's own thread while a pause is in effect.

        Emits `PAUSED` once, right before parking, not on every wakeup: a
        resume that lands before this is ever called (pause and resume
        racing on two different threads) leaves `PAUSED` unemitted, the same
        as the old controller's exit-side-only pause landing. Always
        re-checks for a stop after waking, since a stop also releases this
        wait, and defers to `raise_if_stopped` to land and raise it.
        """
        with self._lock:
            should_emit_paused = self._paused and not self._stop_requested
        if should_emit_paused:
            self._events.emit(CoreEventType.PAUSED)
        with self._lock:
            while self._paused and not self._stop_requested:
                self._lock.wait()
        self.raise_if_stopped()

    def raise_if_stopped(self) -> None:
        """Raise `RunStopped` if a stop is pending, landing it first."""
        with self._lock:
            stopped = self._stop_requested
        if not stopped:
            return
        self._events.emit(CoreEventType.STOPPED)
        raise RunStopped


def splice_steering(user_prompt: str, messages: list[str]) -> str:
    """Append queued operator steering to *user_prompt*.

    Preserves the exact block format the server's `RunController` used
    before run control moved into core, so a spliced prompt reads the same
    regardless of which layer applied it.
    """
    if not messages:
        return user_prompt
    block = "\n".join(f"- {message}" for message in messages)
    return (
        f"{user_prompt.rstrip()}\n\n"
        "## Operator steering (live)\n\n"
        "The operator sent the following instruction(s) for this invocation. "
        "Treat them as high-priority guidance for the work you do now:\n\n"
        f"{block}\n"
    )
