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
    from vs_project.api import Project


def open_run_store(project: Project, *, registry: OrchestrationRegistry | None = None) -> RunStore:
    """Open a read-only run history store for *project*."""
    return _open_run_store(project, registry=registry)


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
