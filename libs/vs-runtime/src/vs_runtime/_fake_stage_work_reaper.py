"""A reaper over an in-memory host, for tests of the code that runs a step again after a crash."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime._step_runner import ClearUnavailableError

if TYPE_CHECKING:
    from vs_core.api import RequestId


class FakeStageWorkReaper:
    """The stage work of dead processes, by tag; ``reap`` ends it, unless the host is unreachable."""

    def __init__(self) -> None:
        """Start with no work left and a reachable host."""
        self.alive: set[RequestId] = set()
        self.reaped: list[RequestId] = []
        self.refusing = False

    def leave(self, tag: RequestId) -> None:
        """A process that ran ``tag``'s stages died and left them running."""
        self.alive.add(tag)

    async def reap(self, tag: RequestId) -> None:
        """End the work left under *tag*; raise when the host cannot be reached."""
        if self.refusing:
            raise ClearUnavailableError(tag, "host")
        self.alive.discard(tag)
        self.reaped.append(tag)
