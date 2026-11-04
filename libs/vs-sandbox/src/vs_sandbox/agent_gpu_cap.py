"""Cap how many of a run's agent GPU jobs are submitted to the scheduler at once.

A run's agents share one :class:`CappedGpuLauncher` (each agent session has its
own broker, but all of them submit through it). A request past the cap waits
here, on the submit host, in arrival order, and is handed to the wrapped
launcher when one of the run's jobs ends. Waiting happens before ``srun``, so a
job's ``--time`` limit starts when it is submitted, never while it waits, and a
request cancelled while it waits is never submitted.

An outcome whose scheduler termination is unconfirmed retains its admission
ticket under the outcome's stable job name. The ticket is released only when
``reconcile`` proves that identity absent, so a lost local wrapper can never
admit a replacement while its allocation may still be alive.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vs_sandbox.agent_gpu import GpuJobOutcome

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


@runtime_checkable
class _GpuJobReconciler(Protocol):
    def reconcile(self, job_name: str) -> bool: ...


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
    leaves the line and returns :data:`CANCELLED_BEFORE_SUBMISSION`. An
    unconfirmed outcome exposes its stable ``job_name`` and retains its slot
    until explicit successful reconciliation of that same identity.
    """

    def __init__(self, inner: GpuLauncher, cap: int, threads: Threads) -> None:
        """Wrap *inner* with a first-come, first-served cap of *cap* submitted jobs."""
        if cap <= 0:
            message = f"the concurrent GPU job cap must be positive, got {cap}"
            raise ValueError(message)
        self._inner = inner
        self._condition = threads.condition(threads.lock())
        self._queue = _Queue(cap)
        self._unsettled: dict[str, set[int]] = {}
        self._next_ticket = 0

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> GpuJobOutcome:
        """Wait for a slot, then run *command* through the wrapped launcher."""
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._queue.waiting.append(ticket)
        release_ticket = True
        try:
            if not self._wait_for_slot(ticket, write, cancel):
                return GpuJobOutcome(CANCELLED_BEFORE_SUBMISSION)
            outcome = self._inner.run(request, command, write=write, cancel=cancel)
            if not outcome.termination_confirmed:
                job_name = outcome.job_name
                if job_name is None:
                    message = "an unconfirmed GPU job outcome has no scheduler identity"
                    raise RuntimeError(message)
                with self._condition:
                    self._unsettled.setdefault(job_name, set()).add(ticket)
                release_ticket = False
            return outcome
        finally:
            if release_ticket:
                self._release((ticket,))

    def reconcile(self, job_name: str) -> bool:
        """Reconcile a submitted job and release any slot retained for its lifetime."""
        if not isinstance(self._inner, _GpuJobReconciler):
            return False
        if not self._inner.reconcile(job_name):
            return False
        with self._condition:
            tickets = tuple(self._unsettled.pop(job_name, ()))
        self._release(tickets)
        return True

    def _release(self, tickets: tuple[int, ...]) -> None:
        """Release confirmed tickets and wake requests whose admission may now change."""
        with self._condition:
            for ticket in tickets:
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
