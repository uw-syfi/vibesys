"""Public read-only contracts and factory for persisted VibeSys runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api._store import (
    RunDocument,
    RunRecord,
    RunRecordFacts,
    RunRecordReadError,
    RunStore,
    WorkspaceChange,
    WorkspaceChangeKind,
    _open_run_store,
)

if TYPE_CHECKING:
    from vibesys.plugin_catalog import OrchestrationRegistry
    from vs_project.api import Git, Project
    from vs_sim.api import Threads


def open_run_store(
    project: Project, *, registry: OrchestrationRegistry, git: Git, threads: Threads
) -> RunStore:
    """Open a read-only run history store for *project*; *threads* makes its locks.

    *git* starts the Git commands that read the project's history.
    """
    return _open_run_store(project, registry=registry, git=git, threads=threads)


__all__ = [
    "RunDocument",
    "RunRecord",
    "RunRecordFacts",
    "RunRecordReadError",
    "RunStore",
    "WorkspaceChange",
    "WorkspaceChangeKind",
    "open_run_store",
]
