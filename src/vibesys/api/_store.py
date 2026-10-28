"""Private product composition for persisted run records."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from vibesys.api.contracts import RunStatus
from vibesys.plugin_catalog import project_run, project_steer_targets
from vibesys.workspace_changes import (
    WorkspaceChange,
    WorkspaceChangeKind,
    is_user_change,
    parse_name_status,
)
from vs_project.api import (
    GitRunHistory,
    GitTracker,
    NullGitTrackerEvents,
    ProjectStateError,
    open_git_repository,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vibesys.api.contracts import RunView
    from vibesys.plugin_catalog import OrchestrationRegistration, OrchestrationRegistry
    from vs_project.api import Git, OrchestrationRunManifest, Project, RunHistory
    from vs_runtime.api.infrastructure import SteerTargets
    from vs_sim.api import Threads


_EFFECTIVE_OBJECTIVE = PurePosixPath("runtime/effective-objective.md")


class RunDocument(BaseModel):
    """One immutable file from portable state, without its storage layout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: PurePosixPath
    contents: bytes


class RunRecordFacts(BaseModel):
    """Immutable policy-neutral facts needed by run frontends."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trusted_input_baseline: str
    effective_objective: str | None = None


class RunRecordReadError(RuntimeError):
    """A non-fatal workspace-history read failed for a recorded run."""

    def __init__(self, summary: str, *, detail: str | None = None) -> None:
        """Capture stable summary and optional implementation detail."""
        super().__init__(summary if detail is None else f"{summary}: {detail}")
        self.summary = summary
        self.detail = detail

    @classmethod
    def unavailable(cls) -> RunRecordReadError:
        """Build the fallback used when a lower reader supplied no detail."""
        return cls("workspace history is unavailable")


@runtime_checkable
class RunRecord(Protocol):
    """One run's semantic read model, independent of project storage."""

    @property
    def run_id(self) -> str:
        """Return the stable run identity."""
        ...

    @property
    def identity(self) -> str:
        """Return an opaque identity suitable for frontend cache partitioning."""
        ...

    def view(self) -> RunView:
        """Return the current policy-owned historical projection."""
        ...

    def facts(self) -> RunRecordFacts:
        """Return immutable manifest and effective-objective facts."""
        ...

    def steer_targets(self) -> SteerTargets | None:
        """Return the roles, attempts and items a steer may name as of the last committed state.

        Read each call, so it follows a run that is still working. None when the run has no
        committed state to name anything in.
        """
        ...

    def history_documents(self) -> tuple[RunDocument, ...]:
        """Return policy-selected documents used for historical inspection."""
        ...

    def portable_documents(self) -> tuple[RunDocument, ...]:
        """Return every portable state document for detailed investigation."""
        ...

    def workspace_changes(self, base: str, head: str) -> tuple[WorkspaceChange, ...]:
        """Return user-visible workspace changes between two commit objects."""
        ...

    def workspace_patch(self, base: str, head: str, paths: tuple[str, ...]) -> str:
        """Return a unified patch for validated paths in one recorded range."""
        ...


class RunStore(Protocol):
    """Read-only history of runs recorded under one project."""

    def list_runs(self) -> Sequence[RunView]:
        """Return every recorded run, most recent first."""
        ...

    def get_run(self, run_id: str) -> RunView:
        """Return the recorded view for one run."""
        ...

    def get_record(self, run_id: str) -> RunRecord:
        """Return one semantic record without exposing project storage."""
        ...


def _open_run_store(
    project: Project,
    *,
    registry: OrchestrationRegistry,
    git: Git,
    threads: Threads,
) -> RunStore:
    """Compose a product store over one canonical project."""
    return _LocalRunStore(project, registry=registry, git=git, threads=threads)


def _run_history(project: Project, git: Git) -> RunHistory:
    """Every run of ``project``, read from its run branches and its checkout."""
    return GitRunHistory(
        project, open_git_repository(project.root, git=git, faults=NullGitTrackerEvents())
    )


class _GitReadEvents(NullGitTrackerEvents):
    """Capture the most recent lower-level warning for typed API failure."""

    def __init__(self) -> None:
        self.warning_value: tuple[str, str | None] | None = None

    def reset(self) -> None:
        self.warning_value = None

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        self.warning_value = (summary, detail)


class _LocalRunRecord:
    """Semantic run reads backed by one canonical project and registry."""

    def __init__(
        self,
        project: Project,
        run_id: str,
        *,
        registry: OrchestrationRegistry,
        git: Git,
        threads: Threads,
    ) -> None:
        self._threads = threads
        self._project = project
        self._history = _run_history(project, git)
        self._run_id = run_id
        self._registry = registry
        self._identity = hashlib.sha256(f"{project.root.resolve()}\0{run_id}".encode()).hexdigest()[
            :16
        ]
        self._git_events = _GitReadEvents()
        self._git = GitTracker(
            project.root,
            run_id=run_id,
            events=self._git_events,
            repository=open_git_repository(project.root, git=git, faults=self._git_events),
        )
        self._git_lock = threads.lock()
        registration = self._registration(self._manifest())
        self._framework_prefixes = (
            registration.plugin.core.artifact_directories if registration is not None else ()
        )

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def identity(self) -> str:
        return self._identity

    def view(self) -> RunView:
        manifest = self._manifest()
        registration = self._registration(manifest)
        return project_run(
            registration,
            self._project,
            run_id=self._run_id,
            status=RunStatus.UNKNOWN,
            loop=manifest.orchestration.id,
        )

    def steer_targets(self) -> SteerTargets | None:
        return project_steer_targets(
            self._registration(self._manifest()), self._project, run_id=self._run_id
        )

    def facts(self) -> RunRecordFacts:
        manifest = self._manifest()
        return RunRecordFacts(
            trusted_input_baseline=manifest.trusted_input_baseline,
            effective_objective=self._effective_objective(),
        )

    def history_documents(self) -> tuple[RunDocument, ...]:
        # A core run keeps its state in the run's state store, read through the projector.
        return ()

    def portable_documents(self) -> tuple[RunDocument, ...]:
        return tuple(
            RunDocument(relative_path=item.relative_path, contents=item.contents)
            for item in self._history.documents(self._run_id)
        )

    def workspace_changes(self, base: str, head: str) -> tuple[WorkspaceChange, ...]:
        output = self._git_read(self._git.diff_name_status, base, head)
        return tuple(
            change
            for change in parse_name_status(output)
            if is_user_change(change, self._framework_prefixes)
        )

    def workspace_patch(self, base: str, head: str, paths: tuple[str, ...]) -> str:
        return self._git_read(self._git.diff_patch, base, head, paths)

    def _manifest(self) -> OrchestrationRunManifest:
        return self._history.manifest(self._run_id)

    def _registration(self, manifest: OrchestrationRunManifest) -> OrchestrationRegistration | None:
        try:
            return self._registry.resolve(manifest.orchestration.id)
        except ValueError:
            return None

    def _effective_objective(self) -> str | None:
        try:
            contents = self._history.document(self._run_id, _EFFECTIVE_OBJECTIVE)
        except (OSError, ProjectStateError):
            return None
        return None if contents is None else contents.decode("utf-8", errors="replace")

    def _git_read(
        self,
        operation: Callable[..., str | None],
        *args: object,
    ) -> str:
        with self._git_lock:
            self._git_events.reset()
            result = operation(*args)
            warning = self._git_events.warning_value
        if result is not None:
            return result
        if warning is None:
            raise RunRecordReadError.unavailable()
        raise RunRecordReadError(warning[0], detail=warning[1])


class _LocalRunStore:
    """`RunStore` backed by one `vs_project.Project`'s persisted state.

    Runs are read through ``RunHistory``, so every run of the project lists and opens
    whichever branch is checked out (each run commits its state on its own run branch).
    """

    def __init__(
        self,
        project: Project,
        *,
        registry: OrchestrationRegistry,
        git: Git,
        threads: Threads,
    ) -> None:
        self._project = project
        self._registry = registry
        self._git = git
        self._threads = threads
        self._history = _run_history(project, git)

    def list_runs(self) -> Sequence[RunView]:
        manifests = self._history.recorded().runs
        return [self.get_record(manifest.run_id).view() for manifest in reversed(manifests)]

    def get_run(self, run_id: str) -> RunView:
        return self.get_record(run_id).view()

    def get_record(self, run_id: str) -> RunRecord:
        # Load now so a missing or unsupported run fails at this boundary, not
        # on the record's first later use.
        self._history.manifest(run_id)
        return _LocalRunRecord(
            self._project, run_id, registry=self._registry, git=self._git, threads=self._threads
        )


__all__ = [
    "RunDocument",
    "RunRecord",
    "RunRecordFacts",
    "RunRecordReadError",
    "RunStore",
    "WorkspaceChange",
    "WorkspaceChangeKind",
    "_open_run_store",
]
