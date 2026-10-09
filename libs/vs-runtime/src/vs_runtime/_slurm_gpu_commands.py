"""Compose the ``slurm-gpu`` run environment's GPU command broker.

The agent runs on the submit host without GPU device nodes. Every GPU process,
its own and the framework's trusted gates, goes through
``vs_sandbox.slurm_gpu_client``: agent commands through a host broker that
re-confines them inside the job, trusted gates directly with ``srun``.
"""

from __future__ import annotations

import os
import secrets
import shlex
import sys
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from vs_agent.api import declare_command_host_resources
from vs_sandbox.api import (
    SANDBOX_GPU_DEVICES_ENV,
    HostResource,
    HostResourceAccess,
    build_host_sandbox,
)
from vs_sandbox.api.slurm import (
    GPU_BROKER_SOCKET_ENV,
    GPU_BROKER_TOKEN_ENV,
    SlurmGpuBroker,
    SlurmGpuConfig,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_project.api import RunResourceRequest
    from vs_sandbox.api import ProjectPathPolicy

#: The variable naming the agent's GPU launcher, for prompts and scripts.
GPU_COMMAND_ENV = "VIBESYS_GPU"


@dataclass(frozen=True, slots=True)
class SlurmGpuCommands:
    """A started broker and what the agent and the trusted gates need to use it."""

    broker: SlurmGpuBroker
    launcher: Path
    agent_env: tuple[tuple[str, str], ...]
    agent_host_resources: tuple[HostResource, ...]
    gate_prefix: tuple[str, ...]

    def gate_command(self, command: str | None) -> str | None:
        """Route a trusted gate command through ``srun`` on the gate allocation."""
        if command is None:
            return None
        return shlex.join((*self.gate_prefix, "--", *shlex.split(command)))


def gate_gpus(config: SlurmGpuConfig, resources: RunResourceRequest | None) -> int:
    """Return the trusted gates' GPU count: the task's request, else the operator default."""
    if resources is None:
        return config.gate_gpus
    if resources.nodes != 1:
        message = "the slurm-gpu run environment runs single-node jobs only"
        raise ValueError(message)
    return config.request(resources.accelerators_per_node, None).gpus


def start_slurm_gpu_commands(  # noqa: PLR0913  # lint-waiver: LW-610007 [PLR0913]; composition passes independent run facts once.
    # > A parameter object would only restate these keywords at the single call
    # > site in the run environment.
    config: SlurmGpuConfig,
    config_path: Path,
    *,
    state_dir: Path,
    workspace: Path,
    worktree_roots: Sequence[Path],
    project_path_policy: ProjectPathPolicy,
    gpus: int,
) -> SlurmGpuCommands:
    """Start the run's broker and write the agent's ``vibesys-gpu`` launcher."""
    state_dir.mkdir(parents=True, exist_ok=True)
    launcher = state_dir / "vibesys-gpu"
    launcher.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m vs_sandbox.slurm_gpu_client "$@"\n'
    )
    launcher.chmod(0o755)
    env = dict(os.environ)
    resources = declare_command_host_resources(env)

    @cache
    def confinement(root: Path) -> list[str]:
        sandbox = build_host_sandbox(
            root,
            env=env,
            resources=resources,
            project_path_policy=project_path_policy,
            require_enforcement=True,
        )
        if sandbox is None:  # require_enforcement raises instead
            message = "GPU commands require host confinement"
            raise RuntimeError(message)
        # Everything before the placeholder argv is the confinement prefix.
        return sandbox.wrap([])

    broker = SlurmGpuBroker(
        config,
        Path(tempfile.gettempdir()) / f"vsg-{secrets.token_hex(8)}.sock",
        workspaces=(workspace,),
        worktree_roots=worktree_roots,
        wrap=lambda root, argv: [*confinement(root), *argv],
    )
    broker.start()
    return SlurmGpuCommands(
        broker=broker,
        launcher=launcher,
        agent_env=(
            (GPU_BROKER_SOCKET_ENV, str(broker.socket_path)),
            (GPU_BROKER_TOKEN_ENV, broker.token),
            (GPU_COMMAND_ENV, str(launcher)),
            (SANDBOX_GPU_DEVICES_ENV, "none"),
            ("CUDA_VISIBLE_DEVICES", ""),
        ),
        agent_host_resources=(
            HostResource(broker.socket_path, HostResourceAccess.READ_WRITE, "GPU job broker"),
            HostResource(state_dir, HostResourceAccess.READ_ONLY, "GPU job launcher"),
        ),
        gate_prefix=(
            sys.executable,
            "-m",
            "vs_sandbox.slurm_gpu_client",
            "--config",
            str(config_path),
            "--gpus",
            str(gpus),
            "--time",
            str(config.gate_time_minutes),
        ),
    )
