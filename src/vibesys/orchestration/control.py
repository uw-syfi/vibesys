"""``ctx.control``: the cooperative stop/pause/debug boundary between policy steps.

Split from ``runtime.py`` by capability; see that module's docstring.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.run.integration import LocalRunIntegration


class _RunControl:
    """A cooperative boundary between policy steps and paid agent turns."""

    def __init__(self, integration: LocalRunIntegration, *, debug: bool) -> None:
        self._channel = integration.control
        self._debug = debug

    async def boundary(self) -> None:
        """Land stop or pause without consuming steering intended for an agent."""
        self._channel.raise_if_stopped()
        await asyncio.to_thread(self._channel.wait_while_paused)

    async def debug_step(self, message: str) -> None:
        """Pause at a policy step when interactive debug mode was requested."""
        await self.boundary()
        if self._debug:
            await asyncio.to_thread(input, f"\n[debug] {message}. Press Enter to continue...")
