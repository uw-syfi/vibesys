"""Waits that must finish: cancellation-safe ways to see owned tasks through.

A caller that owns a task and needs its effect to land (a cleanup, an external
submission, a cancel) must not let its own cancellation, however often it
arrives, abandon that task. ``asyncio.shield`` alone survives one cancellation:
the next one reaches the unshielded second wait. These helpers keep waiting
until the task has ended and only then re-raise the caller's cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable


async def drain(*tasks: Awaitable[object]) -> None:
    """Wait until every task has ended, however often the caller is cancelled.

    Task outcomes (results, exceptions, their own cancellation) are not raised;
    read them from the tasks. If the caller was cancelled while waiting, the
    cancellation is re-raised once every task has ended.
    """
    settled = asyncio.gather(*tasks, return_exceptions=True)
    cancelled: asyncio.CancelledError | None = None
    while not settled.done():
        try:
            await asyncio.shield(settled)
        except asyncio.CancelledError as error:
            cancelled = error
    if cancelled is not None:
        raise cancelled


async def finish[T](task: asyncio.Future[T]) -> T:
    """Return the task's result; a caller's cancellation waits for the task first.

    When the caller is cancelled, the task is not cancelled: it runs to its end
    (see :func:`drain`) and only then does the cancellation propagate, with the
    task's failure, if any, noted on it. The task's own cancellation raises
    ``CancelledError`` like any await.
    """
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        if task.done():
            raise
        with contextlib.suppress(asyncio.CancelledError):
            await drain(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            cancelled.add_note(f"awaited task also failed: {error!r}")
        raise


__all__ = ["drain", "finish"]
