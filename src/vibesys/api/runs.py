"""Run launch and observation roles independent of application assembly."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vs_project.api import Project
from vs_runlog.api import assert_run_unlocked
from vs_runtime.api.infrastructure import StopMode

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

    def stop(self, mode: StopMode = StopMode.DRAIN) -> None:
        """Request cooperative termination in ``mode`` through the run's control.

        The run's control keeps the strongest stop asked for: a cancel after a drain
        escalates it, and a drain never weakens a cancel.
        """
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


def relaunch(request: RunRequest, runs: Runs) -> RunHandle:
    """Resume the saved run as a new epoch using this host's run implementation.

    Refuse a live writer before starting any work. The writer rechecks the lock
    atomically when it opens the log, so concurrent relaunches cannot both write.
    """
    if request.resume is None:
        message = "relaunch requires an existing run (--resume)"
        raise ValueError(message)
    assert_run_unlocked(Project.run_log_path_for(request.project_root, request.resume.run_id))
    return runs.resume(request)


__all__ = ["RunHandle", "Runs", "relaunch"]
