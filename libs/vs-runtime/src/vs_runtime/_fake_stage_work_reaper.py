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
        self.other_run_alive: set[RequestId] = set()
        self.reaped: list[RequestId] = []
        self.refusing = False
        self.stuck = False
        """The work survives its kill, so the host can never be shown clear."""

    def leave(self, tag: RequestId) -> None:
        """A process that ran ``tag``'s stages died and left them running."""
        self.alive.add(tag)

    def leave_in_other_run(self, tag: RequestId) -> None:
        """Another run on the host left work under the same tag."""
        self.other_run_alive.add(tag)

    async def reap(self, tag: RequestId) -> None:
        """End the work left under *tag*; raise when the host cannot be reached or kept it."""
        if self.refusing:
            raise ClearUnavailableError(tag, "host")
        if self.stuck and tag in self.alive:
            raise ClearUnavailableError(tag, "work that survives its kill")
        self.alive.discard(tag)
        self.reaped.append(tag)
