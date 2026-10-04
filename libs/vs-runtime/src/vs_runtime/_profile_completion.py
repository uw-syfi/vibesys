"""Execute a profile outside the state lock, then serialize its durable completion."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    from vs_runtime.contracts import CandidateProfile


type ProfileCompletion = Coroutine[object, object, None]
"""A profile shell operation ready for the run worker to schedule."""


async def complete_profile(
    capture: Callable[[], Awaitable[CandidateProfile]] | None,
    lock: asyncio.Lock,
    record: Callable[[CandidateProfile], Awaitable[None]],
) -> None:
    """Call capture once, then record and commit its result under the supplied lock.

    Capture failures and cancellation propagate without a completion. Capture
    holds no state lock, so independent work continues. Once it returns, the
    caller's synchronous record transition and its durable commit both run
    under the same lock. ``None`` denotes already completed durable intent.
    """
    if capture is None:
        return
    outcome = await capture()
    async with lock:
        await record(outcome)


__all__ = ["ProfileCompletion", "complete_profile"]
