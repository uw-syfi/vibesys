"""A scripted process group leader on a simulated clock, for testing the stop policy.

:class:`FakeStoppableProcess` stands in for a ``Popen`` that
:func:`~vs_sandbox.process_execution.wait_stoppable` waits for and stops. It runs no
program: a :class:`StoppableScript` says how long the process works, what it writes,
and how it reacts to ``SIGTERM``. All waiting goes through the ``Threads`` it is given,
so under ``SimThreads`` a 30 s grace period costs no wall time and the same seed gives the
same interleaving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_sandbox.process_execution import KILL, TERMINATE

if TYPE_CHECKING:
    from vs_sandbox.process_execution import StopSignal
    from vs_sim.api import Threads


@dataclass(frozen=True, slots=True)
class StoppableScript:
    """What the process does when nothing stops it, and how it answers signals."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    """The status when it exits on its own."""
    runs_for: float | None = 0.0
    """Seconds of work before it exits on its own; ``None`` runs until it is signalled."""
    term_delay: float | None = 0.0
    """Seconds from ``SIGTERM`` to exit (``-15``); ``None`` ignores ``SIGTERM``."""


class FakeStoppableProcess:
    """A process that follows its script on the clock of the given ``Threads``.

    ``SIGKILL`` always ends it at once with ``-9``; ``SIGTERM`` ends it ``term_delay``
    seconds later unless it would exit sooner on its own. :attr:`signals` lists every
    signal sent to the group, in order, with the clock reading at the time.
    """

    def __init__(self, threads: Threads, script: StoppableScript | None = None) -> None:
        """Start the process now, on *threads*' clock."""
        self._threads = threads
        self._script = script or StoppableScript()
        self._changed = threads.condition(threads.lock())
        self._exit_at = (
            None if self._script.runs_for is None else threads.now() + self._script.runs_for
        )
        self._code = self._script.returncode
        self.signals: list[tuple[StopSignal, float]] = []

    @property
    def returncode(self) -> int:
        """The status it exited with."""
        with self._changed:
            if not self._exited():
                message = "the process has not exited"
                raise RuntimeError(message)
            return self._code

    def wait(self, timeout: float | None) -> tuple[str, str] | None:
        """Wait on the simulated clock until it exits or *timeout* seconds pass."""
        threads = self._threads
        deadline = None if timeout is None else threads.now() + timeout
        with self._changed:
            while not self._exited():
                limits = [] if self._exit_at is None else [self._exit_at - threads.now()]
                if deadline is not None:
                    if deadline <= threads.now():
                        return None
                    limits.append(deadline - threads.now())
                self._changed.wait(min(limits) if limits else None)
            return self._script.stdout, self._script.stderr

    def signal_group(self, signal_number: StopSignal) -> None:
        """Record the signal and apply it unless the process already exited."""
        with self._changed:
            self.signals.append((signal_number, self._threads.now()))
            if self._exited():
                return
            now = self._threads.now()
            if signal_number is KILL:
                self._end_at(now, -KILL)
            elif signal_number is TERMINATE and self._script.term_delay is not None:
                due = now + self._script.term_delay
                if self._exit_at is None or due < self._exit_at:
                    self._end_at(due, -TERMINATE)
            self._changed.notify_all()

    def _end_at(self, instant: float, code: int) -> None:
        self._exit_at = instant
        self._code = code

    def _exited(self) -> bool:
        return self._exit_at is not None and self._exit_at <= self._threads.now()
