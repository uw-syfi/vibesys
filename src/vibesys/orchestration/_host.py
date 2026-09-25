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
    import asyncio
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from pydantic import BaseModel

    from vibesys.context import RunSetup, _RunResources
    from vibesys.events import FrameworkSource
    from vibesys.orchestration.request import RunRequest
    from vibesys.orchestration.view import RunView
    from vibesys.run.event_journal import EventJournal
    from vibesys.runtime import AgentDefinition, AgentHandle, WorkspaceScope
    from vibesys.sandbox.run_environment import RunEnvironmentView


class _LocalAgentHandleLike(Protocol):
    """What ``workspaces.py`` needs from a live spawned agent handle."""

    scope_id: str | None

    async def close(self) -> None:
        """Release this agent and its sandbox; safe to call more than once."""
        ...


class _WorkspaceHandleLike(Protocol):
    """What ``gates.py``/``agents.py`` need from ``ctx.workspaces.root``."""

    async def snapshot(self, label: str) -> str:
        """Snapshot this workspace and return the recorded revision."""
        ...

    async def restore(
        self,
        revision: str,
        *,
        clean: bool = True,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> None:
        """Restore this workspace to a retained revision."""
        ...

    async def pending_changes(self) -> list[str]:
        """List uncommitted candidate changes in this workspace."""
        ...


class _WorkspacesLike(Protocol):
    """What other capabilities need from ``ctx.workspaces``.

    ``_resources_for``/``_scope_of`` accept ``Any`` for ``scope`` (rather
    than a precise union), because their real parameter type is
    ``WorkspaceScope | WorkspaceHandle | None``: ``WorkspaceHandle`` is a
    concrete class private to ``workspaces.py`` that this module cannot name
    without an import cycle, and a *structural* stand-in for it does not
    satisfy Protocol method contravariance (the concrete method would have
    to accept every object shaped like the stand-in, not just real
    ``WorkspaceHandle``s). Every other member here keeps its precise type.
    """

    @property
    def root(self) -> _WorkspaceHandleLike:
        """Return this run's non-isolated (parent-tree) workspace."""
        ...

    def _resources_for(self, scope: Any) -> _RunResources:  # noqa: ANN401
        """Resolve the resource assembly for a scope (parent tree if ``None``)."""
        ...

    def _scope_of(self, scope: Any) -> WorkspaceScope | None:  # noqa: ANN401
        """Normalize a scope or workspace handle to a plain ``WorkspaceScope``."""
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

    async def reconcile_model_requests(self) -> str | None:
        """Stage candidate-declared model weights; return a rejection reason."""
        ...


class _EvaluatorLike(Protocol):
    """What ``workspaces.py`` needs from ``ctx.gates``."""

    def _lock_for(self, scope: WorkspaceScope | None) -> asyncio.Lock:
        """Return the lock guarding a scope's gates and adopt/checkpoint."""
        ...

    def _forget(self, scope: WorkspaceScope) -> None:
        """Release a discarded scope's synchronization state."""
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


class _GateExecutorLike(Protocol):
    """Structurally identical to ``gates.py``'s own ``GateExecutor``.

    Declared again here (rather than imported) for the same reason as
    ``_CommittedStateProjectorLike``: this module must stay at the base of
    the graph, below ``gates.py``.
    """

    def run_accuracy(
        self,
        ctx: Any,  # noqa: ANN401
        *,
        process_id: str,
        timeout_seconds: int | None = None,
        execution_command: str | None = None,
        round_label: str | None = None,
    ) -> Any:  # noqa: ANN401
        """Run the trusted accuracy command for one candidate."""
        ...

    def run_benchmark(  # noqa: PLR0913
        self,
        ctx: Any,  # noqa: ANN401
        *,
        result_spec: Any = None,  # noqa: ANN401
        result_protocol: Any = None,  # noqa: ANN401
        objectives: Any = (),  # noqa: ANN401
        process_id: str,
        output_slug: str,
        timeout_seconds: int | None = None,
        execution_base: str | None = None,
        round_label: str | None = None,
    ) -> Any:  # noqa: ANN401
        """Run the trusted benchmark result contract for one candidate."""
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
    def request(self) -> RunRequest:
        """Return this run's resolved request."""
        ...

    @property
    def workspaces(self) -> _WorkspacesLike:
        """Return this run's workspace capability."""
        ...

    @property
    def environment(self) -> _EnvironmentLike:
        """Return this run's environment capability."""
        ...

    @property
    def gates(self) -> _EvaluatorLike:
        """Return this run's trusted-gate capability."""
        ...

    @property
    def progress(self) -> _ProgressLike:
        """Return this run's progress-board buffer/declaration capability."""
        ...

    _setup: RunSetup
    _projector: _CommittedStateProjectorLike | None
    _gate_executor: _GateExecutorLike | None
    _parent_mutation_lock: asyncio.Lock
    _spawn_lock: asyncio.Lock

    @property
    def _agents(self) -> Mapping[tuple[str | None, str], _LocalAgentHandleLike]:
        """Return live spawned agent handles, keyed by (scope id, role id)."""
        ...

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

    def _spawn(
        self, definition: AgentDefinition, *, scope: WorkspaceScope | None = None
    ) -> Awaitable[AgentHandle]:
        """Open one independently configured agent in a live workspace."""
        ...
