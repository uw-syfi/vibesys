"""Thread-safe close lifecycle coordination for Omnigent-owned resources."""

from __future__ import annotations

import threading
from enum import StrEnum


class LifecycleState(StrEnum):
    """Exclusive close state shared by session and driver owners."""

    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class CloseLifecycle:
    """Coordinate one close owner, recursive calls, waiters, and failures."""

    def __init__(self, condition: threading.Condition | None = None) -> None:
        self.condition = condition or threading.Condition()
        self.state = LifecycleState.OPEN
        self._owner: threading.Thread | None = None
        self._error: BaseException | None = None

    def begin_close(self) -> bool:
        """Claim close ownership, or wait for the current owner to finish."""
        current = threading.current_thread()
        with self.condition:
            if self.state is LifecycleState.OPEN:
                self.state = LifecycleState.CLOSING
                self._owner = current
                return True
            if self.state is LifecycleState.CLOSING and self._owner is current:
                return False
            self.condition.wait_for(lambda: self.state is LifecycleState.CLOSED)
            if self._error is not None:
                raise self._error
            return False

    def finish_close(self, error: BaseException | None) -> None:
        """Publish the owner's terminal result and release every waiter."""
        with self.condition:
            self._error = error
            self._owner = None
            self.state = LifecycleState.CLOSED
            self.condition.notify_all()
