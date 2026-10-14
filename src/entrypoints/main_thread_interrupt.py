"""Interrupt the main thread from any thread, even when its wake-up signal lands early.

CPython runs a signal's Python handler on the main thread only, and only between
bytecodes. A thread that wants the main thread to stop sends it a signal, which ends the
system call the main thread is blocked in (``epoll_wait`` in an idle event loop, a lock
wait) so the handler runs. A signal that lands after the main thread's last check for
pending handlers but before it enters that system call ends nothing: the handler stays
pending until something else wakes the main thread, which in an idle run never happens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Threads

RESEND_SECONDS = 0.05
"""How long a request waits for the main thread to take its signal before sending it again.

It bounds only how late a signal that landed early is repeated; a signal that ends the
main thread's wait is taken at once.
"""


class MainThreadInterrupt:
    """Raise ``KeyboardInterrupt`` on the main thread once, however early its signal lands.

    ``wake`` sends the main thread a signal whose handler calls :meth:`take`. A request
    sends it again until the main thread has taken it, so a signal lost to the window
    before a blocking call is followed by one that ends the call. Only the first take
    raises: a repeated signal, or a later request, never interrupts the cleanup that the
    first interruption started.
    """

    def __init__(self, wake: Callable[[], None], threads: Threads) -> None:
        """Interrupt through ``wake``, waiting between sends on ``threads``."""
        self._wake = wake
        self._taken = threads.event()

    def take(self) -> None:
        """Called by the wake signal's handler on the main thread.

        Raises:
            KeyboardInterrupt: the first time it is called.
        """
        if self._taken.is_set():
            return
        self._taken.set()
        raise KeyboardInterrupt

    def request(self) -> None:
        """Interrupt the main thread from any other thread; returns once it has taken it."""
        while not self._taken.is_set():
            self._wake()
            self._taken.wait(RESEND_SECONDS)
