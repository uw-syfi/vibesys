"""Awaiting a session operation so its resources outlive a cancelled caller."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


async def await_session_operation[Result](
    operation: asyncio.Task[Result], *, stop: Callable[[], None] | None = None
) -> Result:
    """Retain the session's resources until dispatch or access enforcement settles.

    A cancelled caller cannot interrupt the operation's thread, so ``stop`` asks the
    provider to end the turn (once, off the event loop); without it a hung provider turn
    would outlive the cancellation that is waiting for it.
    """
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError as cancelled:
        stopping = [] if stop is None else [asyncio.ensure_future(asyncio.to_thread(stop))]
        settled = asyncio.gather(operation, *stopping, return_exceptions=True)
        while not settled.done():
            try:
                await asyncio.shield(settled)
            except asyncio.CancelledError:
                continue
        outcome, *stopped = settled.result()
        if isinstance(outcome, BaseException):
            cancelled.add_note(
                f"session operation also failed: {type(outcome).__name__}: {outcome}"
            )
        for failure in stopped:
            if isinstance(failure, BaseException):
                cancelled.add_note(
                    f"provider cancel also failed: {type(failure).__name__}: {failure}"
                )
        raise


__all__ = ["await_session_operation"]
