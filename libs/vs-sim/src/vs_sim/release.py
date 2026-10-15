"""A close that ends only on proof of release.

A close-once latch (``if self._closed: return; self._closed = True; <effect>``)
reports a failed close as success on every retry: the latch is set before the
effect is known to have worked. :class:`CloseOnce` and :class:`AsyncCloseOnce`
replace it. They remember a *failed* close, so the next call runs the effect
again, and only the :class:`Released` value the effect returns ends them. The
owner's translator mints that value from its evidence (``docker rm`` read as
gone, a session close that returned), so "released" is never set on the
caller's word alone.

``started`` says a close was asked for, so the owner can refuse new work while
a failed close waits for its retry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True)
class Released:
    """Proof that a resource is gone or going; ``evidence`` says what showed it."""

    evidence: str


def _proof(value: object) -> Released:
    if not isinstance(value, Released):
        message = f"a close effect must return Released, not {type(value).__name__}"
        raise TypeError(message)
    return value


def _read_outcome(attempt: asyncio.Task[Released]) -> None:
    if not attempt.cancelled():
        attempt.exception()


class CloseOnce:
    """Run a synchronous release until it returns :class:`Released`.

    Not thread-safe: an owner whose close can race holds its own lock around
    :meth:`run`.
    """

    def __init__(self) -> None:
        """Start open: no close asked for, nothing released."""
        self._started = False
        self._released: Released | None = None

    @property
    def started(self) -> bool:
        """Whether a close was asked for, whatever its outcome."""
        return self._started

    @property
    def released(self) -> Released | None:
        """The proof of release, or ``None`` while the resource may still exist."""
        return self._released

    def run(self, effect: Callable[[], Released]) -> Released:
        """Return the earlier proof, or run *effect* and keep its proof.

        An exception from *effect* propagates and leaves the latch unreleased,
        so the next call runs *effect* again.
        """
        self._started = True
        if self._released is None:
            self._released = _proof(effect())
        return self._released


class AsyncCloseOnce:
    """Run an asynchronous release until it returns :class:`Released`.

    Concurrent callers share one attempt; the attempt is shielded, so a
    cancelled caller does not cancel the release. A failed attempt is
    forgotten, so the next call starts a new one.
    """

    def __init__(self) -> None:
        """Start open: no close asked for, nothing released."""
        self._started = False
        self._released: Released | None = None
        self._attempt: asyncio.Task[Released] | None = None

    @property
    def started(self) -> bool:
        """Whether a close was asked for, whatever its outcome."""
        return self._started

    @property
    def released(self) -> Released | None:
        """The proof of release, or ``None`` while the resource may still exist."""
        return self._released

    async def run(self, effect: Callable[[], Awaitable[Released]]) -> Released:
        """Return the earlier proof, or join or start an attempt of *effect*."""
        self._started = True
        if self._released is not None:
            return self._released
        if self._attempt is None:
            self._attempt = asyncio.create_task(self._settle(effect))
            # A failure every caller stopped waiting for is still read, so asyncio does not log it.
            self._attempt.add_done_callback(_read_outcome)
        return await asyncio.shield(self._attempt)

    async def _settle(self, effect: Callable[[], Awaitable[Released]]) -> Released:
        try:
            self._released = _proof(await effect())
        finally:
            self._attempt = None
        return self._released
