"""The agent's own GPU commands, run as Slurm jobs: what the broker offers and the container sees.

This is the one implementation of the capability. The ``slurm`` environment
enables it from ``[vibesys.agent_gpu]`` (the deprecated ``slurm-gpu`` alias
translates its old file into that table). The agent's container binds no GPU and runs
``vibesys-gpu --gpus N --time MINUTES -- COMMAND...``; the host broker checks
the request against the operator's limits and runs the command in a new Slurm
job under the job confinement.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from vs_agent.api import declare_command_host_resources
from vs_sandbox.api.slurm import AgentGpuLauncher, GpuCommands, HostJobConfinement

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.api import ProjectPathPolicy
    from vs_sandbox.api.slurm import AgentGpuConfig, JobConfinement

#: The client's name in the container, whose ``PATH`` finds it.
AGENT_GPU_LAUNCHER = "vibesys-gpu"
#: The variable naming the agent's GPU launcher, for prompts and scripts.
GPU_COMMAND_ENV = "VIBESYS_GPU"


def agent_gpu_commands(
    config: AgentGpuConfig,
    *,
    workspace: Path,
    project_path_policy: ProjectPathPolicy,
    confinement: JobConfinement | None = None,
    launcher: AgentGpuLauncher | None = None,
) -> GpuCommands:
    """Return what the broker needs to offer the ``gpu`` operation under *config*.

    The job confinement is chosen here, explicitly: *confinement*, or by default
    the host sandbox. The job runs on a compute node, where Docker is normally
    unavailable, whatever the agent itself runs in. *launcher* is the ``srun``
    launcher, shared with a caller that runs its own jobs through it.
    """
    host_env = dict(os.environ)
    confinement = confinement or HostJobConfinement(
        env=host_env,
        resources=declare_command_host_resources(host_env),
        project_path_policy=project_path_policy,
    )
    # Fail closed now, not at the first GPU command: a host that cannot confine
    # a job must not start a run whose agent can only use GPUs through jobs.
    confinement.wrap(workspace, [])
    return GpuCommands(config, confinement, host_env, launcher=launcher or AgentGpuLauncher(config))


def agent_gpu_env(launcher: Path) -> dict[str, str]:
    """Return the container variables for the agent's GPU launcher: its path, and no device."""
    return {GPU_COMMAND_ENV: str(launcher), "CUDA_VISIBLE_DEVICES": ""}
