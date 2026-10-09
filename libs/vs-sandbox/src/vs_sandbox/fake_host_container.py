"""A container Fake that runs its commands on the host, for tests that need a real shell."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_sandbox.compute_backends import ComputeBackend, SandboxKind
from vs_sandbox.lifecycle import SandboxLifecycle
from vs_sandbox.local_compute_backend import LocalBackend
from vs_sandbox.local_shell import LocalShellRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vs_sandbox.execution import CommandRunner
    from vs_sandbox.host_resources import HostResource
    from vs_sandbox.lifecycle import SandboxLifecycleHooks


class HostExecutedContainer(LocalShellRunner):
    """A started-and-stopped container whose commands run directly on the host.

    Paths are the host's own, so ``agent_path`` is the identity. Nothing is
    isolated; use it only where a test needs real shell semantics for the
    run's commands but no Docker daemon.
    """

    def start(self) -> None:
        """Start nothing: the host is already running."""

    def stop(self) -> None:
        """Stop nothing: the host keeps running."""


class HostExecutedContainerBackend(LocalBackend):
    """CPU backend whose Docker sandboxes are :class:`HostExecutedContainer`s."""

    def __init__(
        self,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
    ) -> None:
        """Report a base image so Docker run environments can derive an agent image."""
        super().__init__(ComputeBackend.CPU, log_dir, log=log, image=image or "fake-backend-image")

    def make_sandbox(  # noqa: PLR0913  # lint-waiver: LW-012001 [PLR0913]; mirrors the structural ComputeBackendImpl.make_sandbox signature this Fake substitutes for.
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None = None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        extra_env: dict[str, str] | None = None,
        extra_init_commands: list[str] | None = None,
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
        attach_accelerator: bool = True,
        ephemeral: bool = False,
        container_image: str | None = None,
        auth_files: list[tuple[str, str]] | None = None,
        resources: Sequence[HostResource] = (),
        docker_in_docker: bool = False,
    ) -> CommandRunner:
        """Return a host-executing container for ``DOCKER``; defer to the CPU backend otherwise."""
        if kind is not SandboxKind.DOCKER:
            return super().make_sandbox(
                kind,
                host_workspace=host_workspace,
                log_path=log_path,
                bind_mounts=bind_mounts,
                extra_env=extra_env,
                extra_init_commands=extra_init_commands,
                lifecycle_hooks=lifecycle_hooks,
                attach_accelerator=attach_accelerator,
                ephemeral=ephemeral,
                container_image=container_image,
                auth_files=auth_files,
                resources=resources,
                docker_in_docker=docker_in_docker,
            )
        del bind_mounts, log_path, extra_init_commands, attach_accelerator, ephemeral
        del container_image, auth_files, resources, docker_in_docker
        container = HostExecutedContainer(host_workspace, env=extra_env, inherit_env=True)
        SandboxLifecycle(lifecycle_hooks).before_ready(container)
        return container
