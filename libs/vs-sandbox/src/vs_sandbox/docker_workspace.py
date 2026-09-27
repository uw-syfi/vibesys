"""Docker mechanics for host-mounted workspaces."""

from __future__ import annotations

import os
import shlex
import subprocess
from contextlib import suppress
from typing import TYPE_CHECKING

from vs_sandbox.host_resources import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.project_paths import ProjectPathPolicy

_MAINTENANCE_TIMEOUT_SECONDS = 120


class DockerWorkspaceRepairError(RuntimeError):
    """Raised when Docker cannot restore host ownership of a workspace."""

    @classmethod
    def launch_failed(cls, workspace: Path, error: Exception) -> DockerWorkspaceRepairError:
        """Describe a Docker process that could not be launched or completed."""
        return cls(f"chown failed for {workspace}: {error}")

    @classmethod
    def command_failed(
        cls, workspace: Path, returncode: int, detail: str
    ) -> DockerWorkspaceRepairError:
        """Describe a completed maintenance command that returned nonzero."""
        return cls(f"chown failed for {workspace} (rc={returncode}): {detail}")


def docker_project_path_resources(
    policy: ProjectPathPolicy,
    workspace: Path,
    *,
    mask_root: Path,
) -> tuple[HostResource, ...]:
    """Lower project visibility policy to Docker overlay resources.

    Read-only project paths are mounted over Docker's writable workspace.
    Hidden paths are overlaid with empty operator-owned files or directories
    below *mask_root*, preserving whether each hidden target is a file or a
    directory.
    """
    resolved = policy.resolve(workspace)
    resolved_workspace = workspace.resolve()
    resources = [
        HostResource(
            protected.path,
            HostResourceAccess.READ_ONLY,
            "container mount",
            f"/workspace/{protected.path.relative_to(resolved_workspace).as_posix()}",
        )
        for protected in resolved.read_only_paths
    ]

    for index, hidden in enumerate(resolved.hidden_paths):
        mask = mask_root / str(index)
        if hidden.is_directory:
            mask.mkdir(parents=True, exist_ok=True)
        else:
            mask.parent.mkdir(parents=True, exist_ok=True)
            mask.touch(exist_ok=True)
        resources.append(
            HostResource(
                mask,
                HostResourceAccess.READ_ONLY,
                "container mount",
                f"/workspace/{hidden.path.relative_to(resolved_workspace).as_posix()}",
            )
        )
    return tuple(resources)


def repair_docker_workspace(workspace: Path, *, image: str) -> None:
    """Restore host ownership after a root-running container wrote the workspace."""
    if not workspace.exists():
        return
    uid, gid = os.getuid(), os.getgid()
    try:
        result = _run(
            workspace,
            image=image,
            shell_command=f"chown -R {uid}:{gid} /workspace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerWorkspaceRepairError.launch_failed(workspace, exc) from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise DockerWorkspaceRepairError.command_failed(workspace, result.returncode, detail)


def remove_docker_workspace_child(workspace: Path, rel_path: str, *, image: str) -> bool:
    """Best-effort remove one workspace child through a privileged container."""
    target = workspace / rel_path
    with suppress(OSError, subprocess.TimeoutExpired):
        _run(
            workspace,
            image=image,
            shell_command=f"rm -rf -- {shlex.quote(f'/workspace/{rel_path}')}",
        )
    return not (target.exists() or target.is_symlink())


def _run(
    workspace: Path,
    *,
    image: str,
    shell_command: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603  # lint-waiver: LW-948008 [S603]; Docker receives framework-built commands and a quoted workspace path with no host shell.
        [  # noqa: S607  # lint-waiver: LW-948009 [S607]; Docker is intentionally resolved from the operator's configured PATH.
            "docker",
            "run",
            "--rm",
            "-v",
            f"{workspace}:/workspace",
            image,
            "bash",
            "-c",
            shell_command,
        ],
        capture_output=True,
        check=False,
        timeout=_MAINTENANCE_TIMEOUT_SECONDS,
    )


__all__ = [
    "DockerWorkspaceRepairError",
    "remove_docker_workspace_child",
    "repair_docker_workspace",
]
