"""Where blocking calls run, so a simulator can run them inline."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable


class BlockingRunner(Protocol):
    """Runs a blocking function without stalling the event loop."""

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Call ``function`` once and return its result; its exception propagates."""
        ...


class ShieldedBlockingRunner:
    """A runner whose caller, when cancelled, waits for the function to end before it is.

    Cancelling the task that awaits :class:`ThreadBlockingRunner` ends the await and leaves the
    thread going. A caller that must not return while its work runs (a step runner: the loop
    starts nothing more on the step until the call returns) goes through this one instead: the
    cancel is raised once the function has ended, and the function's own result or error is
    dropped in favour of it. Further cancels do not shorten the wait.
    """

    def __init__(self, inner: BlockingRunner) -> None:
        """Run on *inner*."""
        self._inner = inner

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Call ``function`` on the inner runner; a cancel waits for it to end, then propagates."""
        work = asyncio.ensure_future(self._inner.run(function, *args, **kwargs))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001  # lint-waiver: LW-451201 [BLE001]; the function's own error after the cancel is dropped: the cancel is what the caller sees.
                    break
            if not work.cancelled():
                work.exception()
            raise


class ThreadBlockingRunner:
    """Runs the function on a worker thread, as ``asyncio.to_thread`` does."""

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Call ``function`` on a worker thread."""
        return await asyncio.to_thread(function, *args, **kwargs)
