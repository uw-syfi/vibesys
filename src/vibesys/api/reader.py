"""Public contract for reading a finished run: conversations over its saved state.

A ``RunSession`` drives a live run. A ``RunReader`` only opens a run that already ended, to
converse about it: it takes no write lease, runs no agent of the run, and changes nothing of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api.assembly import SessionImplementations
    from vibesys.api.auxiliary import AuxiliaryAgents, RunReady
    from vibesys.api.contracts import CoreEvent
    from vibesys.plugin_catalog import OrchestrationRegistry
    from vibesys.run.contracts import RunRequest


class RunReader(Protocol):
    """One finished run, opened for conversation."""

    def ready(self) -> RunReady:
        """The facts a frontend attaches chat with, as a live run publishes them."""
        ...

    def open_auxiliary_agents(
        self, *, sink: Callable[[CoreEvent], object] | None = None
    ) -> AuxiliaryAgents:
        """An independent caller-owned scope of conversations over the saved run."""
        ...

    def close(self) -> None:
        """Release the reader; scopes already transferred stay with their owners."""
        ...


class RunReaders(Protocol):
    """Opens the reader of the saved run a request resumes."""

    def __call__(self, request: RunRequest) -> RunReader:
        """Open ``request`` (a resume of a finished run) for reading; it starts nothing."""
        ...


def open_run_reader(
    request: RunRequest,
    *,
    registry: OrchestrationRegistry,
    implementations: SessionImplementations,
) -> RunReader:
    """Open the run ``request`` resumes for reading, over an explicit catalog.

    Application assembly seam, as ``create_session`` is: use ``launch.default_run_readers``.
    """
    # lint-waiver: LW-020013 [PLC0415]; keep importing the public facade independent of session execution machinery and built-in plugins.
    from vibesys.api._reader import _LocalRunReader  # noqa: PLC0415

    return _LocalRunReader(request, registry=registry, implementations=implementations)


__all__ = ["RunReader", "RunReaders", "open_run_reader"]
