"""Run launch and observation roles independent of application assembly."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vibesys.api.contracts import CoreEvent, RunRequest, RunResult
    from vibesys.api.session import RunSession


class RunHandle(Protocol):
    """One independently executing run and its replayable semantic events.

    Cancellation of a result waiter or event subscriber never stops the run.
    Execution owns resource cleanup on completion, failure and cancellation.
    """

    @property
    def run_id(self) -> str:
        """The identity known before execution begins."""
        ...

    @property
    def session(self) -> RunSession:
        """Transitional query, readiness, auxiliary-agent and control seam."""
        ...

    def start(self) -> None:
        """Start the execution task, idempotently; Runs already calls this."""
        ...

    def events(self) -> AsyncIterator[CoreEvent]:
        """Replay from the first event and follow until the run settles."""
        ...

    def stop(self) -> None:
        """Request cooperative termination through the existing run control."""
        ...

    def cancel(self) -> None:
        """Force task cancellation and unwind owned resources."""
        ...

    async def result(self) -> RunResult:
        """Await terminal completion without taking ownership of execution."""
        ...


class Runs(Protocol):
    """Launch runs within an active event loop and retain attachable handles."""

    def start(self, request: RunRequest) -> RunHandle:
        """Validate a fresh request and return an already started handle."""
        ...

    def resume(self, request: RunRequest) -> RunHandle:
        """Validate request.resume and return an already started handle."""
        ...

    def attach(self, run_id: str) -> RunHandle:
        """Return an active or terminal handle; missing identities raise KeyError."""
        ...

    def list_active(self) -> tuple[RunHandle, ...]:
        """List currently executing runs in this process, in launch order."""
        ...


__all__ = ["RunHandle", "Runs"]
