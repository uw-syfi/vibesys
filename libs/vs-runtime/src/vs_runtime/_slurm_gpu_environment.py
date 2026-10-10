"""The ``slurm-gpu`` run environment: a Docker editor whose GPU work runs as Slurm jobs."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, cast

from vs_runtime._brokered_session import BrokeredRunEnvironmentSession
from vs_runtime._container_runtime_policy import reject_docker_in_docker
from vs_runtime._host_command_bridge import planned_gates
from vs_runtime._run_environment import (
    AgentPaths,
    DockerEnvironment,
    DockerEnvironmentConfig,
    RunEnvironmentPresentation,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    RunEnvironmentView,
    SlurmGpuEnvironmentFacts,
    _AgentPathSandbox,
    _host_evaluation_plan,
    _make_host_sandbox,
    _materialize_effective_objective,
    _NoopWorkspaceRecovery,
    _PreparedRunEnvironment,
)
from vs_runtime._slurm_gpu_commands import gate_gpus, start_slurm_gpu_commands
from vs_sandbox.api import SandboxSession
from vs_sandbox.api.slurm import GateKind, JobConfinement, SlurmGpuConfig, load_slurm_gpu_config

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import RunResourceRequest


class SlurmGpuEnvironment(_NoopWorkspaceRecovery):
    """Edit in a local Docker container and run every GPU process as a Slurm job.

    The agent never runs on the host. The host owns a broker whose socket is
    mounted into the container; the agent's GPU commands and the framework's
    trusted gates are requests to it. Brokered jobs run on compute nodes,
    where Docker is normally unavailable, so the broker confines each one
    with the host sandbox rather than a container.
    """

    # What the workspace materialization consults: jobs run on nodes that share
    # the workspace, so external symlinks are copied in rather than mounted.
    isolated = False
    materialize_local_model_weights = True
    default_profiler_id = "nsys"
    supported_profiler_ids: frozenset[str] | None = None
    backend_image: str | None = None
    requires_local_profiler_preflight = True

    def __init__(
        self,
        config_path: Path,
        resources: RunResourceRequest | None = None,
        docker: DockerEnvironmentConfig | None = None,
        job_confinement: JobConfinement | None = None,
    ) -> None:
        """Bind the operator limits, the task's resources, and the injection seams.

        *docker* configures the editor container. *job_confinement* is how a
        brokered job is confined; the default is the host sandbox.
        """
        self.config_path = config_path.expanduser()
        self.resources = resources
        self._docker = DockerEnvironment(docker or DockerEnvironmentConfig())
        self._job_confinement = job_confinement

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Validate the operator limits before opening the editor container."""
        reject_docker_in_docker(docker_in_docker=request.docker_in_docker, environment="slurm-gpu")
        config = load_slurm_gpu_config(self.config_path)
        gpus = gate_gpus(config, self.resources)
        facts = SlurmGpuEnvironmentFacts(
            str(request.log_dir / "slurm-gpu" / "vibesys-gpu"),
            config.max_gpus,
            config.max_time_minutes,
            gpus,
            planned_gates(request.accuracy_command, request.benchmark_command),
        )
        return _PreparedRunEnvironment(facts, partial(self._open, request, config, gpus))

    def _open(
        self,
        request: RunEnvironmentRequest,
        config: SlurmGpuConfig,
        gpus: int,
        presentation: RunEnvironmentPresentation,
    ) -> RunEnvironmentSession:
        # The gates run on the host, so their tools are installed there.
        if request.evaluator_requirements.tools:
            _make_host_sandbox(request, ephemeral=True)
        plan = _host_evaluation_plan(request)
        commands = start_slurm_gpu_commands(
            config,
            state_dir=request.log_dir / "slurm-gpu",
            workspace=request.workspace,
            worktree_roots=request.run_owned_roots,
            project_path_policy=request.project_path_policy,
            gpus=gpus,
            planned={
                GateKind.ACCURACY: plan.accuracy_command,
                GateKind.BENCHMARK: plan.benchmark_command,
            },
            benchmark_output_argument=request.benchmark_output_argument,
            confinement=self._job_confinement,
        )
        try:
            sandbox = self._docker.build_editor(request, commands.editor)
            objective = _materialize_effective_objective(request)
            paths = AgentPaths(
                objective=(
                    cast("_AgentPathSandbox", sandbox).agent_path(objective)
                    if objective is not None
                    else "OBJECTIVE.md"
                ),
                accuracy_command=commands.gate_command(GateKind.ACCURACY, plan.accuracy_command),
                benchmark_command=commands.gate_command(GateKind.BENCHMARK, plan.benchmark_command),
                profiler_support=(
                    request.profiler_support_name if request.profiler_support_path else None
                ),
            )
            session = SandboxSession.start(
                sandbox,
                RunEnvironmentView(
                    paths=paths,
                    prompt_notes=presentation.prompt_notes,
                    isolated=True,
                    cli_sandboxed=True,
                    host_device_reselect=False,
                    env_kind="slurm-gpu",
                    # Never exercised with one gate broker per candidate. Removed
                    # with the slurm and slurm-gpu unification (D260).
                    parallel_candidate_blocker=(
                        "it has not been verified to run one gate broker per candidate"
                    ),
                ),
            )
        except BaseException:
            commands.broker.close()
            raise
        return BrokeredRunEnvironmentSession(session, (commands.broker,))
