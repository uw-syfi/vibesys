"""The ``slurm`` run environment: a Docker editor whose trusted work runs on a cluster."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vs_runtime._agent_gpu_commands import (
    AGENT_GPU_LAUNCHER,
    agent_gpu_commands,
    agent_gpu_env,
)
from vs_runtime._brokered_session import BrokeredRunEnvironmentSession, HostBroker
from vs_runtime._container_runtime_policy import reject_docker_in_docker
from vs_runtime._host_command_bridge import (
    bridge_editor_extras,
    bridged_agent_paths,
    new_broker_socket_path,
    planned_gates,
    write_client_launcher,
)
from vs_runtime._run_environment import (
    AgentGpuFacts,
    DockerEnvironment,
    DockerEnvironmentConfig,
    RunEnvironmentPresentation,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    RunEnvironmentView,
    SlurmEnvironmentFacts,
    _command_argv,
    _make_host_sandbox,
    _NoopWorkspaceRecovery,
    _prepare_evaluation_plan,
    _PreparedRunEnvironment,
)
from vs_runtime._trusted_evaluation_preparation import (
    REMOTE_EVALUATOR_TOOLS_ROOT,
    TrustedEvaluationCommandPaths,
)
from vs_sandbox.api import HostResource, HostResourceAccess, SandboxSession
from vs_sandbox.api.slurm import (
    DEFAULT_WRAPPER,
    AgentGpuConfig,
    Gates,
    GpuCommands,
    HostCommandBroker,
    RunRoots,
    SlurmCapturePlan,
    SlurmCommandGateRunner,
    SlurmEvaluationPlan,
    SlurmExecutionPolicy,
    SlurmProcessBroker,
    agent_gpu_capability,
    configured_capture_lifecycle,
    load_slurm_policy,
    trusted_profile_command,
    write_slurm_capture_plan,
    write_slurm_evaluation_plan,
)
from vs_slurm.api import SlurmConfig, SlurmSshTransport, load_slurm_config

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vs_agent.api.images import DockerBuildRunner
    from vs_sandbox.api.slurm import JobConfinement

_GATE_LAUNCHER = "vibesys-gate"
_REMOTE_PROFILER_IDS = frozenset({"auto", "none", "rocprof"})


def _slurm_service_command(policy: SlurmExecutionPolicy) -> tuple[str, ...]:
    service = policy.remote_service()
    return () if service is None else service.command


@dataclass(frozen=True, slots=True)
class _ClusterPlans:
    """The evaluation and capture plans the run wrote, and the paths they name."""

    evaluator: Path
    #: ``None`` when profiling is the agent's own GPU command, not a remote capture.
    capture: Path | None
    state_root: Path
    support_paths: Mapping[str, Path]
    has_accuracy: bool
    has_benchmark: bool


class SlurmEnvironment(_NoopWorkspaceRecovery):
    """Edit in a local Docker container while trusted gates and ROCprof run through Slurm.

    The agent never runs on the host. The host owns two brokers whose sockets
    the container reaches: one runs the trusted accuracy and benchmark gates
    (``vs_sandbox.slurm_command``, which stages to the cluster), and, for an
    SSH cluster, one carries the profiler server's transport.

    The operator may also let the agent run its own GPU commands as Slurm jobs
    (``[vibesys.agent_gpu]`` in the operator file, local transport only). The
    same gate broker then offers them, and the agent profiles through its own
    commands instead of through a remote capture.
    """

    isolated = False
    backend_image: str | None = None

    def __init__(
        self,
        config_path: Path,
        docker: DockerEnvironmentConfig | None = None,
        gate_wrapper: Sequence[str] = DEFAULT_WRAPPER,
        job_confinement: JobConfinement | None = None,
    ) -> None:
        """Bind the operator configuration and the injection seams.

        *docker* configures the editor container. *gate_wrapper* is the program
        that runs one trusted gate against the cluster; tests substitute a fake
        that stands in for it. *job_confinement* is how an agent GPU job is
        confined; the default is the host sandbox.
        """
        self.config_path = config_path.expanduser()
        self._docker = DockerEnvironment(docker or DockerEnvironmentConfig())
        self._gate_wrapper = tuple(gate_wrapper)
        self._job_confinement = job_confinement

    def _agent_gpu(self) -> AgentGpuConfig | None:
        """Return the operator's agent GPU limits, or ``None`` when the capability is off."""
        return agent_gpu_capability(
            load_slurm_config(self.config_path), load_slurm_policy(self.config_path)
        )

    # What the run consults before it prepares the environment. The profiler
    # follows the capability: the agent's own GPU commands run where the agent's
    # jobs do, while without them profiling is a remote capture.

    @property
    def materialize_local_model_weights(self) -> bool:
        """Whether external symlinks are copied in: agent jobs read the workspace directly."""
        return self._agent_gpu() is not None

    @property
    def default_profiler_id(self) -> str:
        """The profiler a run uses by default: the agent's own, or the remote ROCprof capture."""
        return "rocprof" if self._agent_gpu() is None else "nsys"

    @property
    def supported_profiler_ids(self) -> frozenset[str] | None:
        """The profilers the run may select; any, when the agent runs its own GPU commands."""
        return _REMOTE_PROFILER_IDS if self._agent_gpu() is None else None

    @property
    def requires_local_profiler_preflight(self) -> bool:
        """Whether the profiler's tool must exist on this host: only the agent's own profiler."""
        return self._agent_gpu() is not None

    def validate_profile(self, profile_command: tuple[str, ...] | None) -> None:
        """Validate the remote capture's workload; the agent's own profiling has none."""
        if self._agent_gpu() is None:
            configured_capture_lifecycle(
                load_slurm_config(self.config_path),
                load_slurm_policy(self.config_path),
                profile_command,
            )

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> SlurmEnvironment:
        """Resolve the operator configuration path.

        ``build_runner`` is the unrecorded injection seam for the agent image build.
        """
        value = options.get("config_path", "~/.config/vibesys/slurm.toml")
        build_runner = options.get("build_runner")
        return cls(
            Path(str(value)),
            docker=DockerEnvironmentConfig(
                build_runner=cast("DockerBuildRunner | None", build_runner)
            ),
        )

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Validate external policy before opening the editor container."""
        reject_docker_in_docker(docker_in_docker=request.docker_in_docker, environment="slurm")
        config = load_slurm_config(self.config_path)
        policy = load_slurm_policy(self.config_path)
        agent_gpu = agent_gpu_capability(config, policy)
        return _PreparedRunEnvironment(
            SlurmEnvironmentFacts(
                service_command=_slurm_service_command(policy),
                gate_client=_GATE_LAUNCHER if agent_gpu is None else AGENT_GPU_LAUNCHER,
                gates=planned_gates(request.accuracy_command, request.benchmark_command),
                agent_gpu=(
                    None
                    if agent_gpu is None
                    else AgentGpuFacts(agent_gpu.max_gpus, agent_gpu.max_time_minutes)
                ),
            ),
            partial(self._open, request, config, policy, agent_gpu),
        )

    def _open(  # lint-waiver: LW-610011 [PLR0913]; the prepared plan binds the run's request and its three loaded operator facts once, before the presentation arrives.
        # > A parameter object would only restate these arguments at the single
        # > call site in prepare.
        self,
        request: RunEnvironmentRequest,
        config: SlurmConfig,
        policy: SlurmExecutionPolicy,
        agent_gpu: AgentGpuConfig | None,
        presentation: RunEnvironmentPresentation,
    ) -> RunEnvironmentSession:
        # Without the agent's own GPU commands, profiling is a remote capture
        # the cluster runs; with them, the agent profiles through its commands.
        remote_profiling = agent_gpu is None
        profiler_tree = request.profiler_support_name if remote_profiling else None
        if profiler_tree is not None and request.profiler_support_path is not None:
            # Validate the trusted load before opening resources owned by the editor.
            trusted_profile_command(
                config,
                policy,
                _command_argv(request.profile_command),
                profiler_tree=profiler_tree,
                workload_timeout_seconds=request.profile_timeout_seconds,
            )
        # The gates run on the host, so their tools are installed there.
        if request.evaluator_requirements.tools:
            _make_host_sandbox(request, ephemeral=True)
        plans = self._write_plans(
            request, config, policy, profiler_tree, remote_profiling=remote_profiling
        )
        gpu = (
            None
            if agent_gpu is None
            else agent_gpu_commands(
                agent_gpu,
                workspace=request.workspace,
                project_path_policy=request.project_path_policy,
                confinement=self._job_confinement,
            )
        )
        brokers: list[HostBroker] = []
        try:
            transport_env, transport_resources = _start_transport_broker(
                config, request, plans, brokers
            )
            gates = self._start_gate_broker(request, plans, gpu)
            brokers.append(gates)
            launcher = write_client_launcher(
                request.log_dir / "slurm-gate",
                _GATE_LAUNCHER if gpu is None else AGENT_GPU_LAUNCHER,
            )
            sandbox = self._docker.build_editor(
                request,
                bridge_editor_extras(
                    gates, launcher, env=None if gpu is None else agent_gpu_env(launcher)
                ),
            )
            profiler_mcp = _remote_profiler_mcp(
                self.config_path, plans, transport_env, transport_resources
            )
            session = SandboxSession.start(
                sandbox,
                RunEnvironmentView(
                    paths=bridged_agent_paths(
                        sandbox,
                        request,
                        launcher,
                        accuracy=plans.has_accuracy,
                        benchmark=plans.has_benchmark,
                    ),
                    prompt_notes=presentation.prompt_notes,
                    isolated=True,
                    cli_sandboxed=True,
                    host_device_reselect=False,
                    env_kind="slurm",
                    profile_execution="remote" if remote_profiling else "local",
                    # Never exercised with one GPU broker per candidate.
                    parallel_candidate_blocker=(
                        None
                        if gpu is None
                        else "it has not been verified to run one GPU broker per candidate"
                    ),
                    framework_setup_timeout_seconds=config.job_timeout_seconds,
                    profiler_mcp_env=profiler_mcp[0],
                    profiler_mcp_resources=profiler_mcp[1],
                ),
            )
        except BaseException:
            for broker in reversed(brokers):
                broker.close()
            raise
        return BrokeredRunEnvironmentSession(session, tuple(brokers))

    def _write_plans(
        self,
        request: RunEnvironmentRequest,
        config: SlurmConfig,
        policy: SlurmExecutionPolicy,
        profiler_tree: str | None,
        *,
        remote_profiling: bool,
    ) -> _ClusterPlans:
        requirements = request.evaluator_requirements
        remote = _prepare_evaluation_plan(
            request,
            requirements,
            TrustedEvaluationCommandPaths(
                source_project_root=request.workspace,
                runtime_project_root=".",
                python_executable=policy.remote_python,
                runtime_package_root=(
                    ".vibesys-evaluator-package" if requirements.package_root is not None else None
                ),
                runtime_tools_root=REMOTE_EVALUATOR_TOOLS_ROOT,
            ),
        )
        support_paths = {
            name: Path(path)
            for name, path in (
                (".vibesys-evaluator-package", requirements.package_root),
                (".vibesys-evaluator-tools", requirements.tools_root),
                *(
                    (
                        (request.profiler_support_name, request.profiler_support_path),
                        *((name, path) for path, name in request.profiler_support_extra),
                    )
                    if remote_profiling
                    else ()
                ),
            )
            if name is not None and path is not None
        }
        state_root = request.log_dir / "slurm-cluster"
        state_root.mkdir(parents=True, exist_ok=True)
        evaluator_plan_path = request.log_dir / "slurm-evaluation-plan.json"
        capture_plan_path = (
            request.log_dir / "slurm-capture-plan.json" if remote_profiling else None
        )
        raw_accuracy = _command_argv(remote.accuracy_command)
        raw_benchmark = _command_argv(remote.benchmark_command)
        raw_profile = _command_argv(remote.profile_command)
        profile = policy.remote_argv(raw_profile) if raw_profile is not None else None
        accuracy = policy.remote_argv(raw_accuracy) if raw_accuracy is not None else None
        benchmark = policy.remote_argv(raw_benchmark) if raw_benchmark is not None else None
        write_slurm_evaluation_plan(
            evaluator_plan_path,
            SlurmEvaluationPlan(
                config_path=self.config_path,
                cluster_state_root=state_root,
                accuracy_command=accuracy,
                benchmark_command=benchmark,
                benchmark_output_argument=request.benchmark_output_argument,
                support_paths=support_paths,
                profile_command=(
                    trusted_profile_command(
                        config,
                        policy,
                        profile,
                        profiler_tree=profiler_tree,
                        workload_timeout_seconds=request.profile_timeout_seconds,
                    )
                    if profiler_tree is not None and profiler_tree in support_paths
                    else None
                ),
            ),
        )
        if capture_plan_path is not None:
            write_slurm_capture_plan(
                capture_plan_path,
                SlurmCapturePlan(
                    cluster_state_root=state_root,
                    profile_command=profile,
                    profile_timeout_seconds=request.profile_timeout_seconds,
                    support_paths=support_paths,
                ),
            )
        return _ClusterPlans(
            evaluator=evaluator_plan_path,
            capture=capture_plan_path,
            state_root=state_root,
            support_paths=support_paths,
            has_accuracy=accuracy is not None,
            has_benchmark=benchmark is not None,
        )

    def _start_gate_broker(
        self, request: RunEnvironmentRequest, plans: _ClusterPlans, gpu: GpuCommands | None
    ) -> HostCommandBroker:
        broker = HostCommandBroker(
            new_broker_socket_path(),
            roots=RunRoots((request.workspace,), tuple(request.run_owned_roots)),
            gpu=gpu,
            gates=Gates(
                SlurmCommandGateRunner(
                    plans.evaluator, env=dict(os.environ), wrapper=self._gate_wrapper
                ),
                benchmark_output_argument=request.benchmark_output_argument,
            ),
        )
        broker.start()
        return broker


def _start_transport_broker(
    config: SlurmConfig,
    request: RunEnvironmentRequest,
    plans: _ClusterPlans,
    brokers: list[HostBroker],
) -> tuple[tuple[tuple[str, str], ...], tuple[HostResource, ...]]:
    """Start the profiler's SSH transport broker; return the env naming it and its mount."""
    if not isinstance(config.transport, SlurmSshTransport):
        return (), ()
    broker = SlurmProcessBroker(
        config,
        new_broker_socket_path(),
        local_roots=(
            request.workspace,
            plans.state_root,
            *request.run_owned_roots,
            *plans.support_paths.values(),
        ),
    )
    broker.start()
    brokers.append(broker)
    return (
        (
            ("VIBESYS_SLURM_BROKER_SOCKET", str(broker.socket_path)),
            ("VIBESYS_SLURM_BROKER_TOKEN", broker.token),
        ),
        (
            HostResource(
                broker.socket_path,
                HostResourceAccess.READ_WRITE,
                "host-owned Slurm transport broker",
            ),
        ),
    )


def _remote_profiler_mcp(
    config_path: Path,
    plans: _ClusterPlans,
    transport_env: tuple[tuple[str, str], ...],
    transport_resources: tuple[HostResource, ...],
) -> tuple[tuple[tuple[str, str], ...], tuple[HostResource, ...]]:
    """Return the environment and mounts of the remote capture's profiler server, if any."""
    if plans.capture is None:
        return (), ()
    return (
        (
            ("VIBESYS_SLURM_CONFIG", str(config_path)),
            ("VIBESYS_SLURM_EVALUATOR_PLAN", str(plans.capture)),
            *transport_env,
        ),
        (
            HostResource(
                plans.state_root,
                HostResourceAccess.READ_WRITE,
                "Slurm cluster operation state and transfers",
            ),
            HostResource(config_path, HostResourceAccess.READ_ONLY, "Slurm profiler configuration"),
            HostResource(plans.capture, HostResourceAccess.READ_ONLY, "Slurm profiler plan"),
            *(
                HostResource(path, HostResourceAccess.READ_ONLY, f"Slurm support tree {name}")
                for name, path in sorted(plans.support_paths.items())
            ),
            *transport_resources,
        ),
    )
