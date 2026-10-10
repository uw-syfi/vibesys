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


class ThreadBlockingRunner:
    """Runs the function on a worker thread, as ``asyncio.to_thread`` does."""

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Call ``function`` on a worker thread."""
        return await asyncio.to_thread(function, *args, **kwargs)
