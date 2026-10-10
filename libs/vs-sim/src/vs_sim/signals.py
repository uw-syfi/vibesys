"""Signal handlers behind an interface, so a test can deliver a signal without a process."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import signal
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
