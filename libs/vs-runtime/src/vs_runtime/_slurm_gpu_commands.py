"""Compose the ``slurm-gpu`` run environment's host command broker.

The agent's GPU commands are the shared capability in
:mod:`vs_runtime._agent_gpu_commands`. What is specific to ``slurm-gpu`` is
that the framework's trusted gates also run as local ``srun`` jobs
(``vibesys-gpu --gate KIND``), sized by ``gate_gpus`` and ``gate_time_minutes``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_runtime._agent_gpu_commands import (
    AGENT_GPU_LAUNCHER,
    agent_gpu_commands,
    agent_gpu_env,
)
from vs_runtime._host_command_bridge import (
    bridge_editor_extras,
    new_broker_socket_path,
    write_client_launcher,
)
from vs_sandbox.api.slurm import (
    GateKind,
    Gates,
    HostCommandBroker,
    RunRoots,
    SlurmGpuConfig,
    SlurmGpuLauncher,
    SrunGateRunner,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from vs_project.api import RunResourceRequest
    from vs_runtime._run_environment import EditorExtras
    from vs_sandbox.api import ProjectPathPolicy
    from vs_sandbox.api.slurm import JobConfinement


@dataclass(frozen=True, slots=True)
class SlurmGpuCommands:
    """A started broker, and what the container needs to reach it."""

    broker: HostCommandBroker
    launcher: Path
    editor: EditorExtras


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
    (``None`` when the task has none).
    """
    slurm = SlurmGpuLauncher(config)
    gpu = agent_gpu_commands(
        config,
        workspace=workspace,
        project_path_policy=project_path_policy,
        confinement=confinement,
        launcher=slurm,
    )
    launcher = write_client_launcher(state_dir, AGENT_GPU_LAUNCHER)
    gate_request = config.request(gpus, config.gate_time_minutes)
    commands = {kind: tuple(shlex.split(command)) for kind, command in planned.items() if command}
    broker = HostCommandBroker(
        new_broker_socket_path(),
        roots=RunRoots((workspace,), tuple(worktree_roots)),
        gpu=gpu,
        gates=Gates(
            SrunGateRunner(slurm, gate_request, commands, env=gpu.host_env),
            benchmark_output_argument=benchmark_output_argument,
        ),
    )
    broker.start()
    return SlurmGpuCommands(
        broker=broker,
        launcher=launcher,
        editor=bridge_editor_extras(broker, launcher, env=agent_gpu_env(launcher)),
    )
