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
from pathlib import Path
from typing import TYPE_CHECKING

from vs_agent.api import declare_command_host_resources
from vs_sandbox.api import HostResource, HostResourceAccess
from vs_sandbox.api.slurm import (
    AgentGpuLauncher,
    GpuCommands,
    HostJobConfinement,
    check_read_only_links,
)
from vs_sim.api import ProcessesProbe

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_runtime._environment_system import EnvironmentSystem
    from vs_sandbox.api import ProjectPathPolicy
    from vs_sandbox.api.slurm import AgentGpuConfig, GpuLauncher, JobConfinement

#: The client's name in the container, whose ``PATH`` finds it.
AGENT_GPU_LAUNCHER = "vibesys-gpu"
#: The variable naming the agent's GPU launcher, for prompts and scripts.
GPU_COMMAND_ENV = "VIBESYS_GPU"


def agent_gpu_commands(
    config: AgentGpuConfig,
    *,
    workspace: Path,
    project_path_policy: ProjectPathPolicy,
    launcher: GpuLauncher,
    confinement: JobConfinement | None = None,
) -> GpuCommands:
    """Return what the broker needs to offer the ``gpu`` operation under *config*.

    The job confinement is chosen here, explicitly: *confinement*, or by default
    the host sandbox. The job runs on a compute node, where Docker is normally
    unavailable, whatever the agent itself runs in. *launcher* starts each job
    (:func:`agent_gpu_launcher`).
    """
    host_env = dict(os.environ)
    if confinement is None:
        resources = declare_command_host_resources(
            host_env, additional=_operator_read_only_resources(config)
        )
        check_read_only_links(
            "vibesys.agent_gpu.read_only_paths",
            config.read_only_paths,
            workspace=workspace,
            resources=resources,
        )
        confinement = HostJobConfinement(
            env=host_env, resources=resources, project_path_policy=project_path_policy
        )
    # Fail closed now, not at the first GPU command: a host that cannot confine
    # a job must not start a run whose agent can only use GPUs through jobs.
    confinement.wrap(workspace, [])
    return GpuCommands(config, confinement, host_env, launcher=launcher)


def agent_gpu_launcher(
    config: AgentGpuConfig, shell_command: Sequence[str], system: EnvironmentSystem
) -> AgentGpuLauncher:
    """Return the launcher that runs the agent's GPU commands as Slurm jobs on *system*.

    *shell_command* is how this host reaches Slurm (the local transport's shell):
    ``srun`` and ``scancel`` run through it, so the operator names the Slurm
    binaries in one place. ``srun`` starts through *system*'s launcher, and
    ``scancel`` and the scheduler report through its Slurm processes.
    """
    return AgentGpuLauncher(
        config,
        shell_command,
        probe=ProcessesProbe(system.slurm_processes),
        processes=system.launcher,
        threads=system.threads,
    )


def _operator_read_only_resources(config: AgentGpuConfig) -> tuple[HostResource, ...]:
    """Return the operator's ``read_only_paths`` as read-only job resources.

    A path that does not exist on this host is an error naming the key: the
    confinement would otherwise skip it silently and the job would fail later
    with a missing interpreter, the symptom this setting exists to remove.
    """
    missing = [path for path in config.read_only_paths if not Path(path).exists()]
    if missing:
        message = f"vibesys.agent_gpu.read_only_paths names paths that do not exist: {missing}"
        raise ValueError(message)
    return tuple(
        HostResource(Path(path), HostResourceAccess.READ_ONLY, "operator agent_gpu.read_only_paths")
        for path in config.read_only_paths
    )


def agent_gpu_env(launcher: Path) -> dict[str, str]:
    """Return the container variables for the agent's GPU launcher: its path, and no device."""
    return {GPU_COMMAND_ENV: str(launcher), "CUDA_VISIBLE_DEVICES": ""}
