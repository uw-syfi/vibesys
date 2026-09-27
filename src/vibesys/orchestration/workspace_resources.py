"""Product composition for runtime-owned workspace resources."""

# This adapter intentionally binds sibling host-owned product resources.
# lint-waiver: LW-228415 [SLF001]; composition must translate the temporary private RunContext owner until that owner is deleted.
# ruff: noqa: SLF001

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.context import WorkspaceResourceSpec, create_workspace_resources
from vibesys.events import FrameworkSource
from vs_runtime.api.infrastructure import resolve_workspace_resource

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.context import _RunResources
    from vibesys.orchestration._host import HostResources
    from vs_runtime.api import Workspace, Workspaces
    from vs_runtime.api.infrastructure import WorkspaceResource


class _WorkspaceResource:
    """Semantic workspace effects over one product resource assembly."""

    def __init__(
        self,
        host: HostResources,
        context: _RunResources,
        workspace_id: str | None,
    ) -> None:
        self._host = host
        self.context = context
        self._id = workspace_id
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
            self._host._resources.git.retain_candidate(self._id, revision)
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
            preserve_paths = tuple(
                dict.fromkeys((*preserve_paths, *self._host._setup.memory_paths))
            )
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
            self._host.warning(
                f"could not restore workspace to revision {revision[:8]}; "
                "will retry on a later round",
                source=FrameworkSource.GIT_TRACKING,
                source_label="rollback",
            )
        return restored

    def retain(self, revision: str, reference: str) -> None:
        self._host._resources.git.retain_candidate(reference, revision)

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


class WorkspaceResourceProvider:
    """Create product environments while runtime owns their collection lifetime."""

    def __init__(self, host: HostResources) -> None:
        """Bind the temporary product host without opening resources eagerly."""
        self._host = host
        self._root: _WorkspaceResource | None = None

    @property
    def root(self) -> WorkspaceResource:
        """Return the root resource after the product host is prepared."""
        if self._root is None:
            self._root = _WorkspaceResource(self._host, self._host._resources, None)
        return self._root

    @property
    def supports_parallel_candidates(self) -> bool:
        """Return the selected environment's fixed isolation capability."""
        return self._host._resources.run_environment_view.supports_parallel_candidate_evaluation

    def create_candidate(self, workspace_id: str, revision: str) -> WorkspaceResource:
        """Open one isolated product environment at a retained revision."""
        request = self._host.request
        context = create_workspace_resources(
            self._host._resources,
            WorkspaceResourceSpec(
                scope_id=workspace_id,
                revision=revision,
                config=request.config,
                agent_backend=request.agent_backend,
                cli_provider=request.cli_provider,
            ),
        )
        return _WorkspaceResource(self._host, context, workspace_id)

    async def close_sessions(self, workspace: Workspace) -> None:
        """Close sessions before runtime tears down the workspace resource."""
        await self._host.agents.close_workspace(workspace)


def resources_for(workspaces: Workspaces, workspace: Workspace) -> _RunResources:
    """Resolve a live runtime handle to its product resource assembly."""
    resource = resolve_workspace_resource(workspaces, workspace)
    if not isinstance(resource, _WorkspaceResource):
        message = "workspace resource was not created by VibeSys composition"
        raise TypeError(message)
    return resource.context
