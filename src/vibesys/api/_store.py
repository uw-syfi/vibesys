"""Private product composition for persisted run records."""

from __future__ import annotations

import hashlib
import threading
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from vibesys.api.contracts import RunStatus
from vibesys.plugin_catalog import project_run
from vs_project.api import (
    GitTracker,
    NullGitTrackerEvents,
    ProjectStateError,
    is_project_state_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vibesys.api.contracts import RunView
    from vibesys.plugin_catalog import OrchestrationRegistration, OrchestrationRegistry
    from vs_project.api import OrchestrationRunManifest, Project


class WorkspaceChangeKind(StrEnum):
    """A semantic file change between two recorded workspace revisions."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class RunDocument(BaseModel):
    """One immutable file from portable state, without its storage layout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: PurePosixPath
    contents: bytes


class WorkspaceChange(BaseModel):
    """One user-visible workspace change between immutable checkpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    kind: WorkspaceChangeKind
    renamed_from: str | None = None


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
    registry: OrchestrationRegistry | None = None,
) -> RunStore:
    """Compose a product store over one canonical project."""
    if registry is None:
        # lint-waiver: LW-020005 [PLC0415]; the product catalog imports every built-in policy, so it loads only when a caller needs it.
        from vibesys.plugin_catalog import built_in_orchestrations  # noqa: PLC0415

        registry = built_in_orchestrations()
    return _LocalRunStore(project, registry=registry)


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
    ) -> None:
        self._project = project
        self._run_id = run_id
        self._registry = registry
        self._identity = hashlib.sha256(f"{project.root.resolve()}\0{run_id}".encode()).hexdigest()[
            :16
        ]
        self._git_events = _GitReadEvents()
        self._git = GitTracker(project.root, run_id=run_id, events=self._git_events)
        self._git_lock = threading.Lock()
        registration = self._registration(self._manifest())
        self._framework_prefixes = (
            registration.plugin.memory_paths if registration is not None else ()
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

    def facts(self) -> RunRecordFacts:
        manifest = self._manifest()
        return RunRecordFacts(
            trusted_input_baseline=manifest.trusted_input_baseline,
            effective_objective=self._effective_objective(),
        )

    def history_documents(self) -> tuple[RunDocument, ...]:
        manifest = self._manifest()
        registration = self._registration(manifest)
        namespaces = (
            (registration.plugin.id,)
            if registration is not None and registration.plugin.state is not None
            else ()
        )
        return tuple(
            RunDocument(relative_path=item.relative_path, contents=item.contents)
            for namespace in namespaces
            for item in self._project.state.portable_namespace(self._run_id, namespace)
            .snapshot()
            .files
        )

    def portable_documents(self) -> tuple[RunDocument, ...]:
        snapshot = self._project.state.portable_run_export(self._run_id)
        return tuple(
            RunDocument(relative_path=item.relative_path, contents=item.contents)
            for item in snapshot.files
        )

    def workspace_changes(self, base: str, head: str) -> tuple[WorkspaceChange, ...]:
        output = self._git_read(self._git.diff_name_status, base, head)
        return tuple(
            change for change in _parse_name_status(output) if self._is_user_change(change)
        )

    def workspace_patch(self, base: str, head: str, paths: tuple[str, ...]) -> str:
        return self._git_read(self._git.diff_patch, base, head, paths)

    def _manifest(self) -> OrchestrationRunManifest:
        return self._project.state.load_run(self._run_id)

    def _registration(self, manifest: OrchestrationRunManifest) -> OrchestrationRegistration | None:
        try:
            return self._registry.resolve(manifest.orchestration.id)
        except ValueError:
            return None

    def _effective_objective(self) -> str | None:
        try:
            document = (
                self._project.state.portable_namespace(self._run_id, "runtime").external_directory()
                / "effective-objective.md"
            )
            return document.read_text(encoding="utf-8") if document.is_file() else None
        except (OSError, ProjectStateError):
            return None

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

    def _is_user_change(self, change: WorkspaceChange) -> bool:
        paths = (
            (change.path,) if change.renamed_from is None else (change.path, change.renamed_from)
        )
        return not any(self._is_framework_path(path) for path in paths)

    def _is_framework_path(self, path: str) -> bool:
        if is_project_state_path(path):
            return True
        return any(
            path == prefix or path.startswith(f"{prefix}/") for prefix in self._framework_prefixes
        )


class _LocalRunStore:
    """`RunStore` backed by one `vs_project.Project`'s persisted state."""

    def __init__(self, project: Project, *, registry: OrchestrationRegistry) -> None:
        self._project = project
        self._registry = registry

    def list_runs(self) -> Sequence[RunView]:
        manifests = self._project.state.list_runs()
        return [self.get_record(manifest.run_id).view() for manifest in reversed(manifests)]

    def get_run(self, run_id: str) -> RunView:
        return self.get_record(run_id).view()

    def get_record(self, run_id: str) -> RunRecord:
        # Load now so a missing or unsupported run fails at this boundary, not
        # on the record's first later use.
        self._project.state.load_run(run_id)
        return _LocalRunRecord(self._project, run_id, registry=self._registry)


def _parse_name_status(output: str) -> tuple[WorkspaceChange, ...]:
    """Parse Git's NUL-delimited name-status format into semantic changes."""
    tokens = output.split("\0")
    changes: list[WorkspaceChange] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        if not status:
            break
        if status.startswith(("R", "C")) and index + 3 <= len(tokens):
            renamed_from, path = tokens[index + 1], tokens[index + 2]
            index += 3
            kind = (
                WorkspaceChangeKind.RENAMED if status.startswith("R") else WorkspaceChangeKind.ADDED
            )
            if kind is not WorkspaceChangeKind.RENAMED:
                renamed_from = ""
        elif index + 1 < len(tokens):
            path = tokens[index + 1]
            renamed_from = ""
            index += 2
            kind = {
                "A": WorkspaceChangeKind.ADDED,
                "D": WorkspaceChangeKind.DELETED,
            }.get(status[:1], WorkspaceChangeKind.MODIFIED)
        else:
            break
        changes.append(WorkspaceChange(path=path, kind=kind, renamed_from=renamed_from or None))
    return tuple(changes)


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
