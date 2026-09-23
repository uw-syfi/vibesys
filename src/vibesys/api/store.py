"""Read-only run history: `RunStore` and `open_run_store`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vibesys.api._agent_state import load_agent_run_state
from vibesys.api._readmodel import project_run_view
from vibesys.api.contracts import LoopKind, RunStatus
from vibesys.loops.agent.model import AgentRunState
from vs_project.api import OrchestrationRunManifest
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.api.contracts import RunView
    from vs_project.api import Project, RunManifestRecord


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


def open_run_store(project: Project) -> RunStore:
    """Open a read-only run history store for *project*."""
    return _LocalRunStore(project)


class _LocalRunStore:
    """`RunStore` backed by one `vs_project.Project`'s persisted state.

    `get_run` always reports `RunStatus.UNKNOWN`: a project's persisted files
    carry no lifecycle field (whether the writing process is still running,
    finished cleanly, or crashed) -- that fact lives only in the server's own
    run journal, which is out of scope for a core-state projection (see
    `RunView`). A live run's real-time status comes from `RunQuery.view()` on
    its own session instead.
    """

    def __init__(self, project: Project) -> None:
        self._project = project

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

    def _view(self, manifest: RunManifestRecord) -> RunView:
        if isinstance(manifest, OrchestrationRunManifest):
            try:
                loop: LoopKind | str = LoopKind(manifest.orchestration.id)
            except ValueError:
                loop = manifest.orchestration.id
        else:
            loop = LoopKind(manifest.configuration.outer_loop)
        state = load_agent_run_state(self._project, manifest.run_id) or AgentRunState()
        return project_run_view(
            state,
            run_id=manifest.run_id,
            status=RunStatus.UNKNOWN,
            experiment_revision=state.experiment_revision,
            loop=loop,
        )
