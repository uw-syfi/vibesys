"""Public run-session protocols and the ``create_session`` entry point."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api.auxiliary import AuxiliaryAgentLaunch, ManagedAgent, RunReady
    from vibesys.api.contracts import EventSink, RunResult, RunView
    from vibesys.plugin_catalog import OrchestrationRegistry
    from vibesys.run.contracts import RunRequest


class RunQuery(Protocol):
    """Semantic reads of a run's authoritative facts."""

    def view(self) -> RunView:
        """Return the current read-only snapshot of this run."""
        ...


class RunControl(Protocol):
    """Messages to the run loop, the sole writer of run state.

    `steer`/`pause`/`resume`/`stop` are messages to the writer, which holds
    an exclusive write lease, not writes performed by the caller. `pause`
    means: reach the next safe boundary, checkpoint durable state, and
    release the lease. `resume` means: acquire the lease, restore the
    checkpoint, and continue. A cross-process resume is
    `create_session(RunRequest(resume=ResumeRef(run_id)))`.
    """

    def steer(self, text: str) -> None:
        """Send free-text steering input to the active run."""
        ...

    def pause(self) -> None:
        """Request the run reach its next safe boundary and release the write lease."""
        ...

    def resume(self) -> None:
        """Request the run acquire the write lease and continue from checkpoint."""
        ...

    def stop(self) -> None:
        """Request the run terminate."""
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
        """Create a fresh product-owned auxiliary conversation for this run."""
        ...

    def close(self) -> None:
        """Close any auxiliary agents the caller did not release early."""
        ...


def create_session(
    request: RunRequest,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry | None = None,
) -> RunSession:
    """Build a session for *request*, publishing its event stream to *sink*.

    Headless calls `create_session(req, sink=renderer.handle).start()`
    then `await session.await_result()`; server does the same with its own
    presentation sink, and reaches optional committed-state/readiness seams
    through `on_committed_view`/`on_ready` instead of an injected integration
    object. Pass a registry to execute a custom orchestration ID; otherwise
    the built-in registry is used.
    """
    # lint-waiver: LW-020012 [PLC0415]; keep importing the public facade independent of session execution machinery and built-in plugins.
    from vibesys.api._session import _create_session  # noqa: PLC0415

    return _create_session(request, sink=sink, registry=registry)


__all__ = ["RunControl", "RunQuery", "RunSession", "create_session"]
