"""Waiting for a step's work or its cancel signal, whichever comes first."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from vs_core.api.recovery import Cancelled

if TYPE_CHECKING:
    from vs_runtime._step_runner import CancelSignal


async def finished_before_cancel[T](work: asyncio.Future[T], cancel: CancelSignal) -> bool:
    """Wait for ``work`` or ``cancel``; cancel ``work`` if the signal came first.

    Returns whether ``work`` finished, with a result or an exception for the caller to read.
    The work is over, finished or cancelled, when this returns, also when the awaiting task
    itself is cancelled.
    """
    stop = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.cancel()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, stop, return_exceptions=True)
    return not work.cancelled()


def cancelled_for(cancel: CancelSignal) -> Cancelled:
    """The result of work the signal ended: ``Cancelled`` for the reason the signal carries."""
    reason = cancel.reason
    if reason is None:
        message = "work was cancelled although no cancel signal is set"
        raise RuntimeError(message)
    return Cancelled(reason=reason)
