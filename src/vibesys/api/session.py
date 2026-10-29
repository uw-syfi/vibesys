"""Public run-session protocols and the ``create_session`` entry point."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vs_runtime.api.infrastructure import StopMode

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api.assembly import SessionImplementations
    from vibesys.api.auxiliary import AuxiliaryAgentLaunch, AuxiliaryAgents, ManagedAgent, RunReady
    from vibesys.api.contracts import EventSink, RunResult, RunView
    from vibesys.plugin_catalog import OrchestrationRegistry
    from vibesys.run.contracts import RunRequest
    from vs_runtime.api.infrastructure import ControlAck, ControlRequest


class RunQuery(Protocol):
    """Semantic reads of a run's authoritative facts."""

    def view(self) -> RunView:
        """Return the current read-only snapshot of this run."""
        ...


class RunControl(Protocol):
    """Messages to the run loop, the sole writer of run state.

    `submit` is the one entry point every control ingress calls (the server's socket, the
    headless inbox): it takes a `ControlRequest`, the one schema of operator controls, and
    applies it through the per-action methods below. `steer`/`pause`/`resume`/`stop` are
    messages to the writer, which holds an exclusive write lease, not writes performed by
    the caller. `pause`
    means: reach the next safe boundary, checkpoint durable state, and
    release the lease. `resume` means: acquire the lease, restore the
    checkpoint, and continue. A cross-process resume is
    `Runs.resume(RunRequest(resume=ResumeRef(run_id), ...))`.
    """

    def submit(self, request: ControlRequest) -> ControlAck:
        """Apply one operator control request and say what became of it.

        Once per ``control_id``: the same request again returns the first answer and changes
        nothing; a different request under the same id answers ``ControlStatus.CONFLICT``.
        """
        ...

    def steer(self, text: str) -> None:
        """Send free-text steering input to the active run."""
        ...

    def pause(self) -> None:
        """Request the run reach its next safe boundary and release the write lease."""
        ...

    def resume(self) -> None:
        """Request the run acquire the write lease and continue from checkpoint.

        A stop only escalates: while a stop is pending, a resume changes nothing.
        """
        ...

    def resume_with_fallback(self) -> bool:
        """Resume, and switch a run paused on a provider capacity limit to its fallback.

        False, changing nothing, when the run is not paused on a capacity limit. A run with
        no configured fallback resumes as `resume` does.
        """
        ...

    def resume_when_capacity_returns(self) -> bool:
        """Keep a run paused on a provider capacity limit paused until capacity returns.

        False, changing nothing, when the run is not paused on a capacity limit. `resume`
        ends the wait at once and `pause` cancels it.
        """
        ...

    def stop(self, mode: StopMode = StopMode.DRAIN) -> None:
        """Request the run terminate, draining or cancelling work in flight as ``mode`` says."""
        ...


class RunSession(RunQuery, RunControl, Protocol):
    """One live or resumable run: query + workspace + control + lifecycle."""

    def start(self) -> None:
        """Begin executing the run."""
        ...

    async def await_result(self) -> RunResult:
        """Wait for the run to reach a terminal state and return its outcome."""
        ...

    def on_committed_view(
        self, listener: Callable[[RunView, tuple[str, ...] | None], None]
    ) -> None:
        """Register a listener for policy views and policy-defined changed keys."""
        ...

    def on_ready(self, listener: Callable[[RunReady], None]) -> None:
        """Register the sole frontend listener for this run's readiness facts."""
        ...

    def create_auxiliary_agent(self, launch: AuxiliaryAgentLaunch) -> ManagedAgent:
        """Create a fresh conversation owned by this run session."""
        ...

    def open_auxiliary_agents(self) -> AuxiliaryAgents:
        """Create an independent caller-owned scope once the run is ready."""
        ...

    def close(self) -> None:
        """Close run-owned agents; independent scopes remain caller-owned."""
        ...


def create_session(
    request: RunRequest,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry,
    implementations: SessionImplementations,
) -> RunSession:
    """Build a session from an explicit orchestration catalog.

    Deprecated application assembly seam. Use launch.default_runs to start an
    independent RunHandle with built-in wiring.
    """
    # lint-waiver: LW-020012 [PLC0415]; keep importing the public facade independent of session execution machinery and built-in plugins.
    from vibesys.api._session import _create_session  # noqa: PLC0415

    return _create_session(request, sink=sink, registry=registry, implementations=implementations)


__all__ = ["RunControl", "RunQuery", "RunSession", "create_session"]
