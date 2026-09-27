"""Structural type for the ``host`` every orchestration capability holds.

``agents.py``, ``workspaces.py``, ``gates.py``, ``state.py``, and
``environment.py`` are capability classes split out of ``runtime.py``
(see that module's docstring); each keeps a private reference to its owning
``RunContext`` as ``self._host``. ``runtime.py`` imports these modules to
build ``RunContext``, so none of them can import ``RunContext`` back
(``tach`` freezes ``TYPE_CHECKING`` imports too: ``ignore_type_checking_imports
= false`` in ``tach.toml``) -- that back-reference would be a module cycle.

Before this module, ``host`` was typed ``Any`` to sidestep the cycle.
``HostResources`` names exactly the attributes the capability classes read
off ``host`` (including the cross-capability ones, e.g. ``workspaces``
reading ``host.gates``), so ``runtime.RunContext`` satisfies it
structurally with no import from this module to any capability module.
This mirrors ``state.py``'s own ``_CommittedStateProjector`` and
``_EventSink``: a small Protocol declared locally, at the base of the
graph, instead of importing the type it structurally matches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel

    from vibesys.context import RunSetup, _RunResources
    from vibesys.events import FrameworkSource
    from vibesys.orchestration.request import RunRequest
    from vibesys.orchestration.view import RunView
    from vibesys.run.event_journal import EventJournal
    from vibesys.sandbox.run_environment import RunEnvironmentView
    from vs_agent.api import DurableSessionStore
    from vs_runtime.api import Commands, Skills, Workspace, Workspaces


class _ExecutionResultLike(Protocol):
    """The bounded command outcome local validation consumes."""

    @property
    def output(self) -> str:
        """Return captured command output."""
        ...

    @property
    def exit_code(self) -> int | None:
        """Return the process exit status, or ``None`` when unavailable."""
        ...


class _EnvironmentLike(Protocol):
    """What other capabilities need from ``ctx.environment``."""

    @property
    def view(self) -> RunEnvironmentView:
        """Return the parent tree's resolved run-environment view."""
        ...

    @property
    def skill_source_paths(self) -> tuple[Path, ...]:
        """Return the run's configured skill source directories."""
        ...

    async def execute(
        self,
        command: str,
        *,
        timeout_seconds: int | None = None,
        # lint-waiver: LW-040118 [ANN401]; the concrete workspace handle cannot be named here without a sibling-module cycle.
        scope: Any = None,  # noqa: ANN401
    ) -> _ExecutionResultLike:
        """Execute one trusted validation command in the selected workspace."""
        ...


class _ProgressLike(Protocol):
    """What ``state.py``/``gates.py`` need from ``ctx.progress``."""

    @property
    def path(self) -> Path | None:
        """Return this run's declared progress-board path, or ``None`` if undeclared."""
        ...

    def drain(self) -> list[str]:
        """Take and clear the pending framework-log blocks, in order."""
        ...


class _AgentSessionsLike(Protocol):
    """Product session owner with candidate-scoped early cleanup."""

    async def close_workspace(self, workspace: Workspace) -> None:
        """Close sessions bound to a candidate before its environment closes."""
        ...


class _CommittedStateProjectorLike(Protocol):
    """Structurally identical to ``state.py``'s own ``_CommittedStateProjector``.

    Declared again here (rather than imported) for the same reason: this
    module must stay at the base of the graph, below every capability module.
    """

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project a state just committed by the host."""
        ...


class HostResources(Protocol):
    """The ``RunContext`` surface every orchestration capability class uses.

    Every member below is read-only (a ``@property``, even where the real
    ``RunContext`` uses a plain attribute): a plain read-write attribute in a
    ``Protocol`` is invariant, so ``RunContext``'s concrete, narrower
    attribute type would fail to satisfy a broader read-write member here.
    """

    @property
    def run_id(self) -> str:
        """Return this run's durable identity."""
        ...

    @property
    def request(self) -> RunRequest:
        """Return this run's resolved request."""
        ...

    @property
    def workspaces(self) -> Workspaces:
        """Return this run's workspace capability."""
        ...

    @property
    def environment(self) -> _EnvironmentLike:
        """Return this run's environment capability."""
        ...

    @property
    def progress(self) -> _ProgressLike:
        """Return this run's progress-board buffer/declaration capability."""
        ...

    @property
    def skills(self) -> Skills:
        """Return run-owned installed-skill resolution."""
        ...

    @property
    def commands(self) -> Commands:
        """Return run-owned sandboxed command execution."""
        ...

    @property
    def agents(self) -> _AgentSessionsLike:
        """Return run-owned explicit agent sessions."""
        ...

    _setup: RunSetup
    _projector: _CommittedStateProjectorLike | None
    _session_store: DurableSessionStore | None

    @property
    def _resources(self) -> _RunResources:
        """Return the host-owned resource assembly after preparation."""
        ...

    @property
    def events(self) -> EventJournal:
        """Return the run's semantic event journal."""
        ...

    def log(self, message: str) -> None:
        """Write one line to the active run log."""
        ...

    def warning(
        self,
        summary: str,
        *,
        detail: str | None = None,
        source: FrameworkSource = ...,
        source_label: str | None = None,
        round_label: str | None = None,
    ) -> None:
        """Publish one non-fatal framework fault as a FRAMEWORK_WARNING event."""
        ...

    async def _run_blocking[**P, Result](
        self, operation: Callable[P, Result], *args: P.args, **kwargs: P.kwargs
    ) -> Result:
        """Run synchronous policy or host work without racing resource teardown."""
        ...
