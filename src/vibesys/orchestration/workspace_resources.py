"""Product composition for runtime-owned workspace resources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.context import WorkspaceResourceSpec, create_workspace_resources
from vibesys.events import (
    CoreEventType,
    CoreEventWriter,
    FrameworkSource,
    FrameworkWarningData,
)
from vs_runtime.api.infrastructure import resolve_workspace_resource

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.context import _RunResources
    from vibesys.orchestration.request import RunRequest
    from vs_runtime.api import Workspace, Workspaces
    from vs_runtime.api.infrastructure import WorkspaceResource


class _WorkspaceResource:
    """Semantic workspace effects over one product resource assembly."""

    def __init__(
        self,
        parent: _RunResources,
        context: _RunResources,
        workspace_id: str | None,
        memory_paths: tuple[str, ...],
        events: CoreEventWriter,
    ) -> None:
        self._parent = parent
        self.context = context
        self._id = workspace_id
        self._memory_paths = memory_paths
        self._events = events
        self._revision = context.git.current_sha()
        self._closed = False

    @property
    def id(self) -> str | None:
        return self._id

    @property
    def path(self) -> Path:
        return self.context.workspace

    @property
    def revision(self) -> str | None:
        if self._id is None:
            return self.context.git.current_sha()
        return self._revision

    @property
    def trusted_input_baseline(self) -> str | None:
        return self.context.git.trusted_input_baseline

    def snapshot(self, label: str) -> str:
        self.context.git.snapshot(label)
        revision = self.context.git.current_sha()
        if revision is None:
            message = "workspace snapshot completed without a Git revision"
            raise RuntimeError(message)
        self._revision = revision
        if self._id is not None:
            self._parent.git.retain_candidate(self._id, revision)
        return revision

    def restore(
        self,
        revision: str,
        *,
        clean: bool,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> bool:
        if preserve_memory:
            preserve_paths = tuple(dict.fromkeys((*preserve_paths, *self._memory_paths)))
        restored = self.context.git.checkout_tree(
            revision,
            clean=clean,
            preserve_paths=preserve_paths,
        )
        if restored and self._id is not None:
            self._revision = revision
        return restored

    def try_restore(self, revision: str, *, clean: bool) -> bool:
        restored = self.restore(revision, clean=clean)
        if not restored:
            self._events.emit(
                CoreEventType.FRAMEWORK_WARNING,
                data=FrameworkWarningData(
                    summary=(
                        f"could not restore workspace to revision {revision[:8]}; "
                        "will retry on a later round"
                    ),
                    source=FrameworkSource.GIT_TRACKING,
                    source_label="rollback",
                ),
            )
        return restored

    def retain(self, revision: str, reference: str) -> None:
        self._parent.git.retain_candidate(reference, revision)

    def pending_changes(self) -> list[str]:
        return self.context.git.pending_changes()

    def candidate_patch(self, revision: str) -> str:
        return self.context.git.candidate_patch(revision)

    def trusted_input_changes(self) -> list[str]:
        return self.context.git.trusted_input_changes()

    def is_directory(self, path: str) -> bool:
        return (self.context.workspace / path).is_dir()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.context.close()


def root_workspace_resource(
    resources: _RunResources,
    memory_paths: tuple[str, ...],
    events: CoreEventWriter,
) -> WorkspaceResource:
    """Adapt the already-open product workspace for runtime ownership."""
    return _WorkspaceResource(resources, resources, None, memory_paths, events)


class CandidateWorkspaceResourceFactory:
    """Bind product inputs needed to open isolated candidate resources."""

    def __init__(
        self,
        resources: _RunResources,
        request: RunRequest,
        memory_paths: tuple[str, ...],
        events: CoreEventWriter,
    ) -> None:
        """Fix the product resource inputs for every candidate."""
        self._resources = resources
        self._request = request
        self._memory_paths = memory_paths
        self._events = events

    def __call__(self, workspace_id: str, revision: str) -> WorkspaceResource:
        """Open one isolated product environment at a retained revision."""
        context = create_workspace_resources(
            self._resources,
            WorkspaceResourceSpec(
                scope_id=workspace_id,
                revision=revision,
                config=self._request.config,
                agent_backend=self._request.agent_backend,
                cli_provider=self._request.cli_provider,
            ),
        )
        return _WorkspaceResource(
            self._resources,
            context,
            workspace_id,
            self._memory_paths,
            self._events,
        )


def resources_for(workspaces: Workspaces, workspace: Workspace) -> _RunResources:
    """Resolve a live runtime handle to its product resource assembly."""
    resource = resolve_workspace_resource(workspaces, workspace)
    if not isinstance(resource, _WorkspaceResource):
        message = "workspace resource was not created by VibeSys composition"
        raise TypeError(message)
    return resource.context
