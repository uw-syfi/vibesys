"""Read-only run history: `RunStore` and `open_run_store`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from framework.api import HostResource, HostResourceAccess
from vibesys.api.contracts import RunStatus
from vibesys.orchestration.contracts import project_run

if TYPE_CHECKING:
    from collections.abc import Sequence

    from framework.api import OrchestrationRunManifest, Project, StateSnapshot
    from vibesys.api.contracts import RunView
    from vibesys.orchestration.contracts import OrchestrationRegistry


class RunStore(Protocol):
    """Read-only history of runs recorded under one project.

    No mutating methods: writing run state is the run loop's exclusive
    privilege (see `vibesys.api.session.RunControl`).
    """

    def list_runs(self) -> Sequence[RunView]:
        """Return every recorded run, most recent first."""
        ...

    def get_run(self, run_id: str) -> RunView:
        """Return the recorded view for one run."""
        ...

    def checkout(self, run_id: str) -> HostResource:
        """Return a host resource for inspecting one run's recorded workspace."""
        ...


def open_run_store(project: Project, *, registry: OrchestrationRegistry | None = None) -> RunStore:
    """Open a read-only run history store for *project*."""
    if registry is None:
        # lint-waiver: LW-020005 [PLC0415]; the built-in orchestration registry imports every loop implementation, so it loads only when a caller needs it.
        from vibesys.loops.registry import built_in_orchestrations  # noqa: PLC0415

        registry = built_in_orchestrations()
    return _LocalRunStore(project, registry=registry)


def portable_history_snapshots(
    project: Project, run_id: str, *, registry: OrchestrationRegistry | None = None
) -> tuple[StateSnapshot, ...]:
    """Read the portable namespaces selected by the run's policy."""
    manifest = project.state.load_run(run_id)
    policy_id = manifest.orchestration.id
    if registry is None:
        # lint-waiver: LW-020006 [PLC0415]; the built-in orchestration registry imports every loop implementation, so it loads only when a caller needs it.
        from vibesys.loops.registry import built_in_orchestrations  # noqa: PLC0415

        registry = built_in_orchestrations()
    selected = registry
    try:
        registration = selected.resolve(policy_id)
    except ValueError:
        registration = None
    names = registration.portable_namespaces if registration is not None else ()
    return tuple(project.state.portable_namespace(run_id, name).snapshot() for name in names)


class _LocalRunStore:
    """`RunStore` backed by one `vs_project.Project`'s persisted state.

    `get_run` always reports `RunStatus.UNKNOWN`: a project's persisted files
    carry no lifecycle field (whether the writing process is still running,
    finished cleanly, or crashed) -- that fact lives only in the server's own
    run journal, which is out of scope for a core-state projection (see
    `RunView`). A live run's real-time status comes from `RunQuery.view()` on
    its own session instead.
    """

    def __init__(self, project: Project, *, registry: OrchestrationRegistry) -> None:
        self._project = project
        self._registry = registry

    def list_runs(self) -> Sequence[RunView]:
        manifests = self._project.state.list_runs()
        return [self._view(manifest) for manifest in reversed(manifests)]

    def get_run(self, run_id: str) -> RunView:
        manifest = self._project.state.load_run(run_id)
        return self._view(manifest)

    def checkout(self, run_id: str) -> HostResource:
        # Ensures *run_id* is a recorded run before handing back a resource.
        self._project.state.load_run(run_id)
        # A run's workspace is the project's own Git worktree, checked out to
        # the run's own `vibesys-runs/<run_id>` branch tip (see
        # `vibesys.run.git_tracker.GitTracker`); there is no separate
        # per-run directory to point at.
        return HostResource(
            path=self._project.root,
            access=HostResourceAccess.READ_ONLY,
            purpose=f"recorded workspace for run {run_id!r}",
        )

    def _view(self, manifest: OrchestrationRunManifest) -> RunView:
        loop = manifest.orchestration.id
        try:
            registration = self._registry.resolve(loop)
        except ValueError:
            registration = None
        return project_run(
            registration,
            self._project,
            run_id=manifest.run_id,
            status=RunStatus.UNKNOWN,
            loop=loop,
        )
