"""Subscription lifetime accounting for the transport server."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

# How long ``wait_for_none_active`` lingers after the count reaches zero
# before declaring the server subscriber-free. A dropped client redials on a
# finite backoff whose first delay is 500ms, so returning the instant the
# count hits zero would let teardown unlink the socket underneath that
# redial. The window shares the launcher's 2s backend exit grace with the
# transport's disconnect and shutdown polls (``unix_jsonl.py``); the three
# together must stay under it so a deliberate quit still tears down without a
# SIGTERM.
RECONNECT_SETTLE_SECONDS = 1.0


class SubscriptionTracker:
    """Count active event subscriptions across handler threads.

    A disconnect must not end the server's lifetime while another subscription
    is still streaming: a client that reconnects after a dropped connection,
    or a second concurrent client, holds the count above zero until its own
    stream closes. ``wait_for_subscriber`` answers the separate question of
    whether any client has ever subscribed, and never resets.
    """

    def __init__(self) -> None:
        """Initialize subscription lifetime tracking and its condition lock."""
        self._ever_subscribed = threading.Event()
        self._condition = threading.Condition()
        self._active = 0

    @contextmanager
    def track(self) -> Generator[None]:
        """Count one subscription stream for the duration of the block."""
        with self._condition:
            self._active += 1
            # Wake a disconnect waiter sitting in its settle window so the
            # reconnect extends the server's lifetime immediately.
            self._condition.notify_all()
        self._ever_subscribed.set()
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                if self._active == 0:
                    self._condition.notify_all()

    def wait_for_subscriber(self, timeout: float) -> bool:
        """Wait until any client has established an event stream."""
        return self._ever_subscribed.wait(timeout)

    def wait_for_none_active(self, settle_seconds: float | None = None) -> None:
        """Block until no stream has been active for ``settle_seconds``.

        The settle window bridges a client's redial backoff: a reconnect that
        lands inside it keeps the wait blocked, so a transient drop cannot
        hand teardown a socket the client is about to dial again.
        """
        if settle_seconds is None:
            settle_seconds = RECONNECT_SETTLE_SECONDS
        with self._condition:
            while True:
                self._condition.wait_for(lambda: self._active == 0)
                if not self._condition.wait_for(lambda: self._active > 0, timeout=settle_seconds):
                    return
