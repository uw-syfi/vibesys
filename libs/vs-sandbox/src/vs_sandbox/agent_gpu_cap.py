"""Cap how many of a run's agent GPU jobs are submitted to the scheduler at once.

A run's agents share one :class:`CappedGpuLauncher` (each agent session has its
own broker, but all of them submit through it). A request past the cap waits
here, on the submit host, in arrival order, and is handed to the wrapped
launcher when one of the run's jobs ends. Waiting happens before ``srun``, so a
job's ``--time`` limit starts when it is submitted, never while it waits, and a
request cancelled while it waits is never submitted.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sandbox.agent_gpu import GpuCommand, GpuJobRequest, GpuLauncher
    from vs_sim.api import Event, Threads

#: The status of a request cancelled before it was submitted. The broker sends no
#: status for a cancelled request, so the value is only what a direct caller sees;
#: it is the shell's status for a command stopped by ``SIGTERM``.
CANCELLED_BEFORE_SUBMISSION = 143
#: How often a waiting request looks at its cancel flag. The flag is the broker's
#: event and cannot wake the queue's condition itself; a slot that frees wakes it.
_CANCEL_POLL_SECONDS = 0.5


@dataclass(slots=True)
class _Queue:
    """The pure admission state: which tickets hold a slot and which wait, in order."""

    cap: int
    active: set[int] = field(default_factory=set)
    waiting: deque[int] = field(default_factory=deque)

    def admit(self, ticket: int) -> bool:
        """Give *ticket* a slot if it is first in line and a slot is free."""
        if self.waiting[0] != ticket or len(self.active) >= self.cap:
            return False
        self.waiting.popleft()
        self.active.add(ticket)
        return True

    def leave(self, ticket: int) -> None:
        """Drop *ticket*, waiting or holding a slot."""
        self.active.discard(ticket)
        if ticket in self.waiting:
            self.waiting.remove(ticket)


class CappedGpuLauncher:
    """A :class:`~vs_sandbox.agent_gpu.GpuLauncher` that submits at most *cap* jobs at once.

    *inner* submits each job (the run's :class:`~vs_sandbox.agent_gpu.AgentGpuLauncher`).
    Share one instance across every session of a run: the cap counts the jobs
    submitted through this instance. A request that must wait writes one notice
    line to its output, then waits until it is first in line and a slot is free,
    or until its *cancel* is set, whichever comes first; a cancelled request
    leaves the line and returns :data:`CANCELLED_BEFORE_SUBMISSION`.
    """

    def __init__(self, inner: GpuLauncher, cap: int, threads: Threads) -> None:
        """Wrap *inner* with a first-come, first-served cap of *cap* submitted jobs."""
        if cap <= 0:
            message = f"the concurrent GPU job cap must be positive, got {cap}"
            raise ValueError(message)
        self._inner = inner
        self._condition = threads.condition(threads.lock())
        self._queue = _Queue(cap)
        self._next_ticket = 0

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> int:
        """Wait for a slot, then run *command* through the wrapped launcher."""
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._queue.waiting.append(ticket)
        try:
            if not self._wait_for_slot(ticket, write, cancel):
                return CANCELLED_BEFORE_SUBMISSION
            return self._inner.run(request, command, write=write, cancel=cancel)
        finally:
            with self._condition:
                self._queue.leave(ticket)
                self._condition.notify_all()

    def _wait_for_slot(self, ticket: int, write: Callable[[bytes], None], cancel: Event) -> bool:
        """Return once *ticket* holds a slot (``True``) or *cancel* is set first (``False``)."""
        with self._condition:
            if cancel.is_set():
                return False
            if self._queue.admit(ticket):
                return True
            active, ahead = len(self._queue.active), self._queue.waiting.index(ticket)
        # Written outside the lock: the broker's write may block on a slow client.
        write(
            f"[vibesys-gpu] waiting: {active} of this run's GPU jobs are active "
            f"(cap {self._queue.cap}), {ahead} queued ahead; the job is submitted, "
            "and its --time starts, when a slot frees\n".encode()
        )
        with self._condition:
            while not cancel.is_set():
                if self._queue.admit(ticket):
                    # A cancel that raced the admission still wins: nothing was submitted.
                    return not cancel.is_set()
                self._condition.wait(_CANCEL_POLL_SECONDS)
            return False


__all__ = ["CANCELLED_BEFORE_SUBMISSION", "CappedGpuLauncher"]
