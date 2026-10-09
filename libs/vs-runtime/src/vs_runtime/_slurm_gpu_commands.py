"""Compose the ``slurm-gpu`` run environment's host command broker.

The agent runs in a Docker container on the submit host, without GPU device
nodes. Every GPU process, its own and the framework's trusted gates, runs as a
Slurm job that the host starts. The container asks for one over a Unix socket:
``vibesys-gpu`` runs the agent's command confined in a new job, and
``vibesys-gpu --gate KIND`` runs the framework's planned accuracy or benchmark
gate unconfined in a job of the gate's size.

The broker, its token and the planned commands stay on the host. The
container receives only the socket, the single-file client, and the variables
the client reads.
"""

from __future__ import annotations

import os
import secrets
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vs_agent.api import declare_command_host_resources
from vs_runtime._run_environment import EditorExtras
from vs_sandbox.api import HostResource, HostResourceAccess
from vs_sandbox.api.slurm import (
    COMMAND_BROKER_SOCKET_ENV,
    COMMAND_BROKER_TOKEN_ENV,
    HOST_COMMAND_CLIENT,
    GateKind,
    Gates,
    GpuCommands,
    HostCommandBroker,
    HostJobConfinement,
    RunRoots,
    SlurmGpuConfig,
    SlurmGpuLauncher,
    SrunGateRunner,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vs_project.api import RunResourceRequest
    from vs_sandbox.api import ProjectPathPolicy
    from vs_sandbox.api.slurm import JobConfinement

#: The variable naming the agent's GPU launcher, for prompts and scripts.
GPU_COMMAND_ENV = "VIBESYS_GPU"

_SHEBANG = "#!/usr/bin/env python3\n"


@dataclass(frozen=True, slots=True)
class SlurmGpuCommands:
    """A started broker, and what the container and the gates need to reach it."""

    broker: HostCommandBroker
    launcher: Path
    editor: EditorExtras

    def gate_command(self, kind: GateKind, planned: str | None) -> str | None:
        """Return the command that runs the planned *kind* gate, or ``None`` if unplanned."""
        if planned is None:
            return None
        return shlex.join((str(self.launcher), "--gate", kind.value))


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
    *,
    state_dir: Path,
    workspace: Path,
    worktree_roots: Sequence[Path],
    project_path_policy: ProjectPathPolicy,
    gpus: int,
    planned: Mapping[GateKind, str | None],
    benchmark_output_argument: str | None,
    confinement: JobConfinement | None = None,
) -> SlurmGpuCommands:
    """Start the run's broker and write the container's ``vibesys-gpu`` launcher.

    *planned* maps each gate to the host command the framework planned for it
    (``None`` when the task has none). The job confinement is chosen here,
    explicitly: *confinement*, or by default the host sandbox. The job runs on
    a compute node, where Docker is normally unavailable, whatever the agent
    itself runs in.
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

    state_dir.mkdir(parents=True, exist_ok=True)
    launcher = state_dir / "vibesys-gpu"
    launcher.write_text(_SHEBANG + HOST_COMMAND_CLIENT.read_text())
    launcher.chmod(0o755)

    slurm = SlurmGpuLauncher(config)
    gate_request = config.request(gpus, config.gate_time_minutes)
    commands = {kind: tuple(shlex.split(command)) for kind, command in planned.items() if command}
    broker = HostCommandBroker(
        Path(tempfile.gettempdir()) / f"vsg-{secrets.token_hex(8)}.sock",
        roots=RunRoots((workspace,), tuple(worktree_roots)),
        gpu=GpuCommands(config, confinement, host_env, launcher=slurm),
        gates=Gates(
            SrunGateRunner(slurm, gate_request, commands, env=host_env),
            benchmark_output_argument=benchmark_output_argument,
        ),
    )
    broker.start()
    return SlurmGpuCommands(
        broker=broker,
        launcher=launcher,
        editor=EditorExtras(
            resources=(
                HostResource(broker.socket_path, HostResourceAccess.READ_WRITE, "GPU job broker"),
                HostResource(state_dir, HostResourceAccess.READ_ONLY, "GPU job launcher"),
            ),
            env={
                COMMAND_BROKER_SOCKET_ENV: str(broker.socket_path),
                COMMAND_BROKER_TOKEN_ENV: broker.token,
                GPU_COMMAND_ENV: str(launcher),
                "CUDA_VISIBLE_DEVICES": "",
            },
            same_path_workspace=True,
            attach_accelerator=False,
        ),
    )
