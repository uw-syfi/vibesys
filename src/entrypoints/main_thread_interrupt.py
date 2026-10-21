"""Interrupt the main thread despite early signals and discarded handler exceptions.

A wake can land before a blocking call, or its ``KeyboardInterrupt`` can land in a
GC callback or finalizer where Python discards exceptions. Resend wakes until the
scope ends, but raise again only when Python reports that it discarded our exception.
An exception still unwinding must never be followed by one that interrupts cleanup.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from sys import UnraisableHookArgs

    from vs_sim.api import Threads

RESEND_SECONDS = 0.05
"""How long a request waits before resending a wake that may have landed early."""

STUCK_SECONDS = 5.0
"""How long an unanswered request waits before reporting the main thread as stuck."""


class MainThreadInterrupt:
    """Raise once unless Python reports that the exception was discarded.

    Enter ``recovering`` before requesting an interruption. Its hook chains to the
    process's previous unraisable hook and rearms only the exact exception raised by
    ``take``. ``finish`` ends the resend loop before the signal relay is joined.
    """

    def __init__(
        self, wake: Callable[[], None], threads: Threads, stuck: Callable[[float], None]
    ) -> None:
        """Interrupt through ``wake``, waiting between sends on ``threads``."""
        self._wake = wake
        self._threads = threads
        self._stuck = stuck
        self._finished = threads.event()
        self._raised: KeyboardInterrupt | None = None

    @contextmanager
    def recovering(self) -> Iterator[None]:
        """Recover discarded interruptions and restore the previous hook on every exit."""
        previous = sys.unraisablehook

        def unraisable(error: UnraisableHookArgs) -> None:
            discarded = error.exc_value is self._raised
            try:
                previous(error)
            finally:
                # Reporting may block too. Keep later wakes harmless until it finishes.
                if discarded:
                    self._raised = None

        sys.unraisablehook = unraisable
        try:
            yield
        finally:
            self.finish()
            sys.unraisablehook = previous
            # The traceback retains this scope and the run until the exception is released.
            self._raised = None

    def take(self) -> None:
        """Raise on the main thread unless the interruption is already propagating.

        Raises:
            KeyboardInterrupt: until one escapes its callback instead of being discarded.
        """
        if self._finished.is_set() or self._raised is not None:
            return
        self._raised = KeyboardInterrupt()
        raise self._raised

    def finish(self) -> None:
        """Disarm delivery and release requesters before joining the signal relay."""
        self._finished.set()

    def request(self) -> None:
        """Resend until ``finish``; a taken wake may still be reported as discarded."""
        started = self._threads.now()
        reported = False
        while not self._finished.is_set():
            self._wake()
            self._finished.wait(RESEND_SECONDS)
            waited = self._threads.now() - started
            if (
                not reported
                and not self._finished.is_set()
                and self._raised is None
                and waited >= STUCK_SECONDS
            ):
                reported = True
                self._stuck(waited)
