"""Subscription lifetime accounting for the transport server."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from vs_sim.api import Condition, Threads

# How long ``wait_for_none_active`` lingers after the count reaches zero
# before declaring the server subscriber-free. A dropped client redials on a
# finite backoff whose first delay is 500ms, so returning the instant the
# count hits zero would let teardown unlink the socket underneath that
# redial. The window shares the launcher's 2s backend exit grace with the
# transport's disconnect and shutdown polls (``unix_jsonl.py``); the three
# together must stay under it so a deliberate quit still tears down without a
# SIGTERM.
RECONNECT_SETTLE_SECONDS = 1.0


class SubscriptionAdmissionClosedError(RuntimeError):
    """A terminal drain has closed admission for new subscription streams."""

    diagnostic_code = "subscription_admission_closed"

    def __init__(self) -> None:
        """Describe the terminal lifecycle transition that refused admission."""
        super().__init__("The server is closing and no longer accepts event subscriptions")


class SettleWindow(Protocol):
    """How a disconnect waiter lets its settle window elapse.

    The tracker asks whether a reconnect arrives inside the window; the
    implementation decides when the window ends. Production ends it after real
    seconds, so a test substitutes an implementation whose window ends when the
    test says so.
    """

    def wait_for(
        self,
        condition: Condition,
        predicate: Callable[[], bool],
        seconds: float,
    ) -> bool:
        """Wait on *condition* (held by the caller) for *predicate*, for one window.

        Returns the value of *predicate* when the wait ends: True when it held
        before the window elapsed, False when the window elapsed first.
        """
        ...


class ThreadingSettleWindow:
    """A settle window that lasts real seconds."""

    def wait_for(
        self,
        condition: Condition,
        predicate: Callable[[], bool],
        seconds: float,
    ) -> bool:
        """Wait up to *seconds* for *predicate* on *condition*."""
        return condition.wait_for(predicate, timeout=seconds)


@dataclass(frozen=True, slots=True)
class DeliveryWatermark:
    """The event-store generation and sequence one stream has written through."""

    store_id: str
    sequence: int

    def covers(self, target: DeliveryWatermark) -> bool:
        """Whether this watermark contains *target* in the same sequence space."""
        return self.store_id == target.store_id and self.sequence >= target.sequence


class StreamDelivery:
    """One subscription stream's progress: the last watermark written to its client."""

    def __init__(self, condition: Condition) -> None:
        """Start before any batch; ``SubscriptionTracker.track`` creates these."""
        self._condition = condition
        self.watermark: DeliveryWatermark | None = None

    def delivered(self, watermark: DeliveryWatermark) -> None:
        """Record that every event through *watermark* was written to the client."""
        with self._condition:
            current = self.watermark
            self.watermark = (
                watermark
                if current is None or current.store_id != watermark.store_id
                else DeliveryWatermark(
                    store_id=watermark.store_id,
                    sequence=max(current.sequence, watermark.sequence),
                )
            )
            self._condition.notify_all()


class SubscriptionTracker:
    """Count active event subscriptions across handler threads.

    A disconnect must not end the server's lifetime while another subscription
    is still streaming: a client that reconnects after a dropped connection,
    or a second concurrent client, holds the count above zero until its own
    stream closes. ``wait_for_subscriber`` answers the separate question of
    whether any client has ever subscribed, and never resets.
    """

    def __init__(self, settle: SettleWindow | None = None, *, threads: Threads) -> None:
        """Initialize subscription lifetime tracking and its condition lock."""
        self._settle = settle or ThreadingSettleWindow()
        self._ever_subscribed = threads.event()
        self._condition = threads.condition()
        self._active = 0
        self._streams: list[StreamDelivery] = []
        self._accepting = True

    @contextmanager
    def track(self) -> Generator[StreamDelivery]:
        """Count one subscription stream for the duration of the block.

        The yielded handle reports what the stream has written, so a server
        about to exit can let its clients read the run's last events first.
        """
        stream = StreamDelivery(self._condition)
        with self._condition:
            if not self._accepting:
                raise SubscriptionAdmissionClosedError
            self._active += 1
            self._streams.append(stream)
            # Wake a disconnect waiter sitting in its settle window so the
            # reconnect extends the server's lifetime immediately.
            self._condition.notify_all()
        self._ever_subscribed.set()
        try:
            yield stream
        finally:
            with self._condition:
                self._active -= 1
                self._streams.remove(stream)
                self._condition.notify_all()

    def wait_until_delivered(self, watermark: DeliveryWatermark, timeout: float) -> bool:
        """Close admission, then wait until every admitted stream reached *watermark*.

        A server whose run has ended calls this before closing its transport,
        so an attached client reads the terminal status before the connection
        closes instead of seeing the close first. Closing admission and taking
        the stream set share one condition lock: a racing stream is either in
        this drain or refused by ``track``. A stream that closes counts as done.
        Returns False if the bound elapsed with an admitted stream behind.
        """
        with self._condition:
            self._accepting = False
            return self._condition.wait_for(
                lambda: all(
                    stream.watermark is not None and stream.watermark.covers(watermark)
                    for stream in self._streams
                ),
                timeout=timeout,
            )

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
                if not self._settle.wait_for(
                    self._condition, lambda: self._active > 0, settle_seconds
                ):
                    return
