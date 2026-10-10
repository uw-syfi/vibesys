"""Incoming and outgoing process signals behind deterministic interfaces."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable


class SignalSource(Protocol):
    """Where a process learns that a signal arrived."""

    def add_handler(self, number: signal.Signals, handler: Callable[[], None]) -> None:
        """Call ``handler`` when ``number`` arrives, replacing an earlier handler for it."""
        ...

    def remove_handler(self, number: signal.Signals) -> bool:
        """Stop handling ``number``; whether a handler was installed."""
        ...


class LoopSignalSource:
    """The running event loop's signal handlers (main thread, Unix)."""

    def add_handler(self, number: signal.Signals, handler: Callable[[], None]) -> None:
        """Install ``handler`` on the running loop."""
        asyncio.get_running_loop().add_signal_handler(number, handler)

    def remove_handler(self, number: signal.Signals) -> bool:
        """Remove the running loop's handler for ``number``."""
        return asyncio.get_running_loop().remove_signal_handler(number)


class ProcessSignaller(Protocol):
    """Terminate an existing process only after validating a stable identity for it."""

    def terminate_if_current(self, pid: int, current: Callable[[], bool]) -> bool:
        """Send SIGTERM to stable ``pid`` iff ``current`` remains true; whether it was sent.

        The implementation acquires its stable process reference before evaluating
        ``current``. It raises ``ProcessLookupError`` when ``pid`` no longer exists and
        ``NotImplementedError`` when the host cannot provide a stable reference.
        """
        ...


class PidfdProcessSignaller:
    """Linux pidfd-backed signalling that cannot target a reused process ID."""

    def terminate_if_current(self, pid: int, current: Callable[[], bool]) -> bool:
        """Open ``pid``, revalidate caller identity, and send SIGTERM through its pidfd."""
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if pidfd_open is None or pidfd_send_signal is None:
            raise NotImplementedError("safe process signalling requires Linux pidfds")
        descriptor = pidfd_open(pid)
        try:
            if not current():
                return False
            pidfd_send_signal(descriptor, signal.SIGTERM)
            return True
        finally:
            os.close(descriptor)
