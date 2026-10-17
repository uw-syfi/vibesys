"""What a Docker sandbox mounts for a run: host resources, setup symlinks, workspace paths.

Split from ``vs_runtime._run_environment`` (which assembles the environments that use
them) so each module stays within the file-length ceiling.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vs_agent.api import CONTAINER_FRAMEWORK_ROOT, AgentBackend, auth_bind_mounts
from vs_runtime._objective_document import materialize_objective_document
from vs_sandbox.api import (
    HostResource,
    HostResourceAccess,
    host_resource_for_mount,
    linked_worktree_git_paths,
    workspace_container_root,
)
from vs_sandbox.api.docker_workspace import docker_project_path_resources
from vs_sandbox.api.symlink_mounts import SymlinkMountScope, collect_symlink_mounts, find_mount_root

if TYPE_CHECKING:
    from vs_runtime._run_environment import RunEnvironmentRequest

RUNTIME_OBJECTIVE_CONTAINER_PATH = "/opt/vibesys-runtime/objective.md"
"""The runtime resource declaration's ``agent_path`` for the effective objective.

Used in exactly one place: the ``HostResource`` :func:`container_mount_plan`
declares for the materialized objective document. Every environment's
:class:`AgentPaths` instead asks the started sandbox's own
:meth:`~vs_sandbox.docker_sandbox.DockerSandbox.agent_path` for that document's
container path, so this constant and the resource declaration are the single
source of truth an environment consults."""


def materialize_effective_objective(request: RunEnvironmentRequest) -> Path | None:
    """Select and materialize the effective objective for this run.

    Operator constraints are composed at the CLI boundary. VibeSys decides
    whether an objective exists and which authored document it represents;
    runtime infrastructure validates or writes the selected document.
    """
    if request.objective is None:
        return None
    return materialize_objective_document(
        request.objective,
        workspace=request.git_history_root or request.workspace,
        authored_document=request.objective_document,
        destination=request.log_dir / "effective-objective.md",
    )


def container_mount_plan(
    request: RunEnvironmentRequest,
    *,
    include_cli_provider_mounts: bool = True,
    same_path: bool = False,
) -> tuple[list[HostResource], list[tuple[str, str]]]:
    """Build the host resources + setup symlinks for a sandbox.

    Returns the resource list the sandbox enforces and exposes through
    :meth:`~vs_sandbox.docker_sandbox.DockerSandbox.agent_path` (workspace
    read-write mapping aside, which the sandbox itself always provides),
    and the setup symlinks a lifecycle hook must create once the container is
    up.

    ``include_cli_provider_mounts`` controls whether CLI auth state and the
    full project tree are added under ``/opt/vibesys-auth`` and
    ``/opt/vibesys``. Defaults to True; both supported environments (local
    Docker and the Modal-via-Docker mode) bind-mount these read-only and copy
    auth state into the container's writable layer during setup.
    """
    bind_mounts: list[tuple[str, str, bool]] = []
    symlinks: list[tuple[str, str]] = []
    ref_dir = request.ref_dir
    root = sandbox_workspace_root(request, same_path=same_path)

    mount_scope = SymlinkMountScope(
        workspace_root=root,
        skip=frozenset(mount.host_path.name for mount in request.environment_bind_mounts),
    )

    if ref_dir is not None:
        reference_path = reference_container_path(request, same_path=same_path)
        collect_symlink_mounts(
            ref_dir,
            reference_path,
            bind_mounts=bind_mounts,
            symlinks=symlinks,
            scope=mount_scope,
        )
        if ref_dir.parent != ref_dir:
            collect_symlink_mounts(
                ref_dir.parent,
                str(Path(reference_path).parent),
                bind_mounts=bind_mounts,
                symlinks=symlinks,
                scope=mount_scope,
            )

    objective_document = materialize_effective_objective(request)
    if objective_document is not None:
        bind_mounts.append((str(objective_document), RUNTIME_OBJECTIVE_CONTAINER_PATH, True))
    if request.git_history_root is not None:
        bind_mounts.append((str(request.git_history_root), "/opt/vibesys-history", True))
    for mount in request.environment_bind_mounts:
        resolved = mount.host_path.resolve()
        host_path = find_mount_root(resolved)
        if host_path == resolved:
            bind_mounts.append((str(host_path), mount.container_path, mount.read_only))
        else:
            rel = resolved.relative_to(host_path)
            mount_name = mount.container_path.strip("/").replace("/", "_") or "environment_mount"
            ancestor_mount = f"{root}/_mounts/{mount_name}"
            bind_mounts.append((str(host_path), ancestor_mount, mount.read_only))
            symlinks.append((mount.container_path, f"{ancestor_mount}/{rel}"))

    if request.profiler_support_path and request.profiler_support_name:
        bind_mounts.append(
            (
                request.profiler_support_path,
                f"{root}/{request.profiler_support_name}",
                True,
            )
        )
        bind_mounts.extend(
            (extra_path, f"{root}/{extra_name}", True)
            for extra_path, extra_name in request.profiler_support_extra
        )

    if request.evaluator_requirements.package_root is not None:
        bind_mounts.append(
            (
                str(request.evaluator_requirements.package_root),
                "/opt/vibesys-evaluator-package",
                True,
            )
        )

    resources = [
        host_resource_for_mount(host, container, read_only=read_only)
        for host, container, read_only in bind_mounts
    ]
    resources.extend(
        docker_project_path_resources(
            request.project_path_policy,
            request.workspace,
            mask_root=request.log_dir / "sandbox-hidden",
            container_root=root,
        )
    )

    # A candidate workspace is a linked Git worktree whose ``.git`` file names its
    # metadata by host path. Mounted at that same path, read-only, ``git status``
    # and ``git diff`` work in the container while history and other candidates'
    # refs and worktrees stay unwritable (``agent_git_writable_in`` reports that).
    resources.extend(
        HostResource(path, HostResourceAccess.READ_ONLY, "linked worktree Git metadata")
        for path in linked_worktree_git_paths(request.workspace)
    )

    if (
        include_cli_provider_mounts
        and (request.agent_backend or AgentBackend.CLI) == AgentBackend.CLI
        and request.cli_provider
    ):
        resources.extend(
            host_resource_for_mount(host, container, read_only=read_only)
            for host, container, read_only in auth_bind_mounts(request.cli_provider)
        )
        resources.append(
            host_resource_for_mount(
                str(request.framework_root), CONTAINER_FRAMEWORK_ROOT, read_only=True
            )
        )

    return resources, symlinks


def sandbox_workspace_root(request: RunEnvironmentRequest, *, same_path: bool = False) -> str:
    """Return where the request's Docker sandbox mounts the workspace."""
    return workspace_container_root(
        str(request.workspace), same_path=request.docker_in_docker or same_path
    )


def reference_container_path(request: RunEnvironmentRequest, *, same_path: bool = False) -> str:
    """Return the reference path inside an isolated workspace.

    Normal project references retain their repository-relative location. An
    external reference directory uses the legacy ``reference`` location under
    the workspace root while its external symlink targets are mounted
    separately.
    """
    root = sandbox_workspace_root(request, same_path=same_path)
    if request.ref_dir is None:
        return f"{root}/reference"
    try:
        relative = request.ref_dir.resolve().relative_to(request.workspace.resolve())
    except ValueError:
        return f"{root}/reference"
    return f"{root}/{relative.as_posix()}"
