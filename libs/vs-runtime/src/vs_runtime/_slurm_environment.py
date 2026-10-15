"""The ``slurm`` run environment: a Docker editor whose trusted work runs on a cluster."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from vs_runtime._agent_gpu_commands import (
    AGENT_GPU_LAUNCHER,
    agent_gpu_commands,
    agent_gpu_env,
    agent_gpu_launcher,
)
from vs_runtime._brokered_session import BrokeredRunEnvironmentSession, HostBroker
from vs_runtime._container_runtime_policy import reject_docker_in_docker
from vs_runtime._host_command_bridge import (
    agent_paths,
    bridge_editor_extras,
    cluster_editor_extras,
    new_broker_socket_path,
    write_client_launcher,
)
from vs_runtime._job_tools import TOOLKIT_BIN_PATTERNS, find_job_tool, off_path_tools
from vs_runtime._run_environment import (
    AgentGpuFacts,
    ClusterEvaluationFacts,
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
from vs_runtime._tool_location import ToolLocation, ToolSite, assumed_tool
from vs_runtime._trusted_evaluation_preparation import (
    REMOTE_EVALUATOR_TOOLS_ROOT,
    TrustedEvaluationCommandPaths,
)
from vs_sandbox.api import HostResource, HostResourceAccess, SandboxSession
from vs_sandbox.api.slurm import (
    AgentGpuConfig,
    BrokerTransport,
    GpuCommands,
    HostCommandBroker,
    RunRoots,
    SlurmCapturePlan,
    SlurmEvaluationPlan,
    SlurmExecutionPolicy,
    SlurmOperatorSettings,
    SlurmProcessBroker,
    configured_capture_lifecycle,
    load_slurm_gpu_alias_settings,
    load_slurm_operator_settings,
    trusted_profile_command,
    write_slurm_capture_plan,
    write_slurm_operator_toml,
)
from vs_slurm.api import (
    SlurmConfig,
    slurm_transport_broker_available,
    slurm_transport_broker_policy,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_agent.api.images import DockerBuildRunner
    from vs_project.api import RunResourceRequest
    from vs_runtime._run_environment import _OrphanReaper
    from vs_sandbox.api import DockerCli
    from vs_sandbox.api.slurm import JobConfinement

_REMOTE_PROFILER_IDS = frozenset({"auto", "none", "rocprof"})
_NO_PROFILER_IDS = frozenset({"auto", "none"})


def _slurm_service_command(policy: SlurmExecutionPolicy) -> tuple[str, ...]:
    service = policy.remote_service()
    return () if service is None else service.command


class _Mode(Protocol):
    """How the agent profiles and reaches GPUs, derived once from the operator settings.

    Every choice that depends on whether the agent runs its own GPU commands
    (which profilers it may select, whether the run offers parallel candidates)
    is read from here, so none re-tests the capability.
    """

    @property
    def gpu_client(self) -> str:
        """The program the agent runs for its own GPU commands, or empty without them."""
        ...

    @property
    def agent_gpu_facts(self) -> AgentGpuFacts | None:
        """The limits the agent is told, or ``None`` without the capability."""
        ...

    @property
    def remote_capture(self) -> bool:
        """Whether profiling is an ad hoc capture the cluster runs."""
        ...

    @property
    def materialize_local_model_weights(self) -> bool:
        """Whether external symlinks are copied in (agent jobs read the workspace directly)."""
        ...

    @property
    def default_profiler_id(self) -> str:
        """The profiler a run uses by default."""
        ...

    @property
    def supported_profiler_ids(self) -> frozenset[str] | None:
        """The profilers a run may select, or ``None`` for any."""
        ...

    @property
    def requires_local_profiler_preflight(self) -> bool:
        """Whether the profiler's tool must exist on this host."""
        ...

    def locate_tool(self, executable: str, site: ToolSite) -> ToolLocation:
        """Where this mode runs *executable* at *site*, or why it cannot."""
        ...

    def gpu_commands(
        self, request: RunEnvironmentRequest, confinement: JobConfinement | None
    ) -> GpuCommands | None:
        """What the host command broker offers as GPU commands, if anything."""
        ...

    def editor_env(self, launcher: Path) -> dict[str, str] | None:
        """The container variables the client launcher needs, if any."""
        ...


@dataclass(frozen=True, slots=True)
class _ClusterOnly:
    """The agent has no host commands; a brokerable transport enables remote capture.

    The capture's profiler server reaches the cluster through a host transport
    broker. A transport whose authorization policy cannot safely be brokered
    offers no remote capture.
    """

    remote_capture: bool
    gpu_client = ""
    agent_gpu_facts = None
    materialize_local_model_weights = False
    requires_local_profiler_preflight = False

    @property
    def default_profiler_id(self) -> str:
        return "rocprof" if self.remote_capture else "none"

    @property
    def supported_profiler_ids(self) -> frozenset[str]:
        return _REMOTE_PROFILER_IDS if self.remote_capture else _NO_PROFILER_IDS

    def locate_tool(self, executable: str, site: ToolSite) -> ToolLocation:
        if site is ToolSite.AGENT_SHELL:
            return ToolLocation.missing(
                f"`{executable}` would run in the editor container, but this mode runs "
                "profiling on the cluster, not in the container"
            )
        return assumed_tool(executable)

    def gpu_commands(
        self, request: RunEnvironmentRequest, confinement: JobConfinement | None
    ) -> None:
        del request, confinement

    def editor_env(self, launcher: Path) -> None:
        del launcher


@dataclass(frozen=True, slots=True)
class _AgentGpuMode:
    """The agent runs its own GPU commands as Slurm jobs and profiles with them."""

    config: AgentGpuConfig
    #: How this host reaches Slurm: the local transport's shell.
    shell_command: tuple[str, ...]
    #: The ``PATH`` a job inherits (the host's), and where the CUDA tools live besides it.
    job_path: str = field(default_factory=lambda: os.environ.get("PATH", ""))
    toolkit_patterns: tuple[str, ...] = TOOLKIT_BIN_PATTERNS
    gpu_client = AGENT_GPU_LAUNCHER
    remote_capture = False
    materialize_local_model_weights = True
    default_profiler_id = "nsys"
    supported_profiler_ids = None
    requires_local_profiler_preflight = True

    @property
    def agent_gpu_facts(self) -> AgentGpuFacts:
        return AgentGpuFacts(
            self.config.max_gpus,
            self.config.max_time_minutes,
            self.config.read_only_paths,
            tool_paths=off_path_tools(
                _JOB_PROFILER_TOOLS, path_env=self.job_path, toolkit_patterns=self.toolkit_patterns
            ),
        )

    def locate_tool(self, executable: str, site: ToolSite) -> ToolLocation:
        if site is ToolSite.COMMAND_ENVIRONMENT:
            return assumed_tool(executable)
        if site is ToolSite.AGENT_SHELL:
            return ToolLocation.missing(
                f"`{executable}` would run in the editor container, which ships no "
                "profiling tools, and the workload runs in Slurm jobs, not in the container"
            )
        command = find_job_tool(
            executable, path_env=self.job_path, toolkit_patterns=self.toolkit_patterns
        )
        if command is None:
            # The compute nodes' toolkit may live where this host cannot see it (a login
            # node without CUDA), so an absent tool is the cluster's to provide, not a
            # reason to refuse; the prompt then offers no path and the bare name stands.
            return assumed_tool(executable)
        return ToolLocation.found(command)

    def gpu_commands(
        self, request: RunEnvironmentRequest, confinement: JobConfinement | None
    ) -> GpuCommands:
        return agent_gpu_commands(
            self.config,
            workspace=request.workspace,
            project_path_policy=request.project_path_policy,
            launcher=agent_gpu_launcher(self.config, self.shell_command, request.system),
            confinement=confinement,
        )

    def editor_env(self, launcher: Path) -> dict[str, str]:
        return agent_gpu_env(launcher)


#: The profilers whose directories the agent is told when a job's PATH lacks them.
_JOB_PROFILER_TOOLS = ("ncu", "nsys")


def _mode(settings: SlurmOperatorSettings) -> _Mode:
    """Derive the run's mode from the settings, once."""
    shell_command = settings.agent_gpu_shell_command
    if settings.agent_gpu is not None and shell_command is not None:
        return _AgentGpuMode(settings.agent_gpu, shell_command)
    return _ClusterOnly(remote_capture=slurm_transport_broker_available(settings.config))


@dataclass(frozen=True, slots=True)
class _ClusterPlans:
    """The evaluation and capture plans the run wrote, and the paths they name."""

    #: ``None`` when profiling is the agent's own GPU command, not a remote capture.
    capture: Path | None
    #: The evaluation plan as written, for consumers of the environment's facts.
    plan: SlurmEvaluationPlan
    #: The operator settings file the plans name: the run's own rendering of its settings.
    config_path: Path
    state_root: Path
    support_paths: Mapping[str, Path]


class SlurmEnvironment(_NoopWorkspaceRecovery):
    """Edit in a local Docker container while trusted evaluation and ROCprof run through Slurm.

    The agent never runs on the host. Trusted accuracy and benchmark work is not
    brokered to the container: the framework runs it through the cluster executor, and
    the agent requests it through the evaluation tool. For an SSH or connector cluster
    the host owns one broker whose socket the container reaches, which carries the
    profiler server's transport.

    The operator may also let the agent run its own GPU commands as Slurm jobs
    (``[vibesys.agent_gpu]`` in the operator file, local transport only). A second
    broker then offers them, and the agent profiles through its own commands instead
    of through a remote capture.
    """

    isolated = False
    backend_image: str | None = None

    def __init__(
        self,
        settings: SlurmOperatorSettings,
        docker: DockerEnvironmentConfig | None = None,
        job_confinement: JobConfinement | None = None,
        deprecation: str | None = None,
    ) -> None:
        """Bind the operator settings, parsed once, and the injection seams.

        *settings* is the operator file as ``load_slurm_operator_settings`` read
        it (or the translation of the deprecated ``slurm-gpu`` file). Every
        later read in the run uses it, so the facts cannot change mid-run.
        *docker* configures the editor container. *job_confinement* is how an agent GPU job is
        confined; the default is the host sandbox.
        """
        self._settings = settings
        self._mode = _mode(settings)
        self._docker = DockerEnvironment(docker or DockerEnvironmentConfig())
        self._job_confinement = job_confinement
        self._deprecation = deprecation

    @property
    def deprecation(self) -> str | None:
        """The notice for a deprecated selection, or ``None``.

        The environment is built once per run but opened many times (once per
        candidate session), so the notice is read here by whoever launches the
        run and reports it once, not logged by every open.
        """
        return self._deprecation

    # What the run consults before it prepares the environment. The profiler
    # follows the mode: the agent's own GPU commands run where the agent's
    # jobs do, while without them profiling is a remote capture, and where
    # there is no way to run one, none is offered.

    @property
    def materialize_local_model_weights(self) -> bool:
        """Whether external symlinks are copied in: agent jobs read the workspace directly."""
        return self._mode.materialize_local_model_weights

    @property
    def default_profiler_id(self) -> str:
        """The profiler a run uses by default: the agent's own, the remote capture, or none."""
        return self._mode.default_profiler_id

    @property
    def supported_profiler_ids(self) -> frozenset[str] | None:
        """The profilers the run may select; any, when the agent runs its own GPU commands."""
        return self._mode.supported_profiler_ids

    @property
    def requires_local_profiler_preflight(self) -> bool:
        """Whether the profiler's tool must exist on this host: only the agent's own profiler."""
        return self._mode.requires_local_profiler_preflight

    def locate_tool(self, executable: str, site: ToolSite) -> ToolLocation:
        """Where this run runs *executable* at *site*: the mode decides, from the operator file."""
        return self._mode.locate_tool(executable, site)

    def validate_profile(self, profile_command: tuple[str, ...] | None) -> None:
        """Validate the remote capture's workload; the agent's own profiling has none."""
        if self._mode.remote_capture:
            configured_capture_lifecycle(
                self._settings.config, self._settings.policy, profile_command
            )

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> SlurmEnvironment:
        """Parse the operator file named by the options, once.

        ``build_runner``, ``docker`` and ``orphan_reaper`` are unrecorded injection
        seams for the agent image build, the Docker CLI and recovery.
        """
        value = options.get("config_path", "~/.config/vibesys/slurm.toml")
        build_runner = options.get("build_runner")
        return cls(
            load_slurm_operator_settings(Path(str(value))),
            docker=DockerEnvironmentConfig(
                docker=cast("DockerCli | None", options.get("docker")),
                build_runner=cast("DockerBuildRunner | None", build_runner),
                orphan_reaper=cast("_OrphanReaper | None", options.get("orphan_reaper")),
            ),
        )

    @classmethod
    def from_slurm_gpu(
        cls,
        config_path: Path,
        resources: RunResourceRequest | None,
        *,
        docker: DockerEnvironmentConfig | None = None,
        job_confinement: JobConfinement | None = None,
    ) -> SlurmEnvironment:
        """Open the deprecated ``slurm-gpu`` selection: its file, translated once.

        The file is read and translated to ``slurm`` settings now, so a bad or
        untranslatable file fails here, naming the key. Running a prepared
        environment carries one deprecation notice (:attr:`deprecation`) for the
        run's launch to report; opening, which a run does many times, never logs it.
        """
        if resources is not None and resources.nodes != 1:
            message = "the slurm-gpu run environment runs single-node jobs only"
            raise ValueError(message)
        path = config_path.expanduser()
        settings = load_slurm_gpu_alias_settings(
            path, task_gpus=None if resources is None else resources.accelerators_per_node
        )
        stage_root = settings.config.remote_workspace_root
        return cls(
            settings,
            docker=docker,
            job_confinement=job_confinement,
            deprecation=(
                f"--run-environment slurm-gpu is deprecated: use --run-environment slurm with "
                f'[slurm.transport] kind = "local" and [vibesys.agent_gpu] in a slurm.toml. '
                f"Translating {path}; trusted gates now run as sbatch jobs staged into "
                f"{stage_root} (set remote_workspace_root in a slurm.toml to choose another "
                f"directory shared with the compute nodes)"
            ),
        )

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Validate external policy before opening the editor container."""
        reject_docker_in_docker(docker_in_docker=request.docker_in_docker, environment="slurm")
        return _PreparedRunEnvironment(
            SlurmEnvironmentFacts(
                service_command=_slurm_service_command(self._settings.policy),
                gpu_client=self._mode.gpu_client,
                agent_gpu=self._mode.agent_gpu_facts,
            ),
            partial(self._open, request),
        )

    def _open(
        self,
        request: RunEnvironmentRequest,
        presentation: RunEnvironmentPresentation,
    ) -> RunEnvironmentSession:
        config, policy, mode = self._settings.config, self._settings.policy, self._mode
        remote_profiling = mode.remote_capture
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
        # The trusted evaluation runs on the host, so its tools are installed there.
        if request.evaluator_requirements.tools:
            _make_host_sandbox(request, ephemeral=True)
        plans = self._write_plans(
            request, config, policy, profiler_tree, remote_profiling=remote_profiling
        )
        gpu = mode.gpu_commands(request, self._job_confinement)
        brokers: list[HostBroker] = []
        try:
            transport_env, transport_resources = _start_transport_broker(
                config, request, plans, brokers
            )
            if gpu is None:
                # Nothing for a broker to answer: the agent has no host command at all.
                extras = cluster_editor_extras()
            else:
                commands = self._start_command_broker(request, gpu)
                brokers.append(commands)
                launcher = write_client_launcher(request.log_dir / "slurm-gpu", mode.gpu_client)
                extras = bridge_editor_extras(commands, launcher, env=mode.editor_env(launcher))
            sandbox = self._docker.build_editor(request, extras)
            profiler_mcp = _remote_profiler_mcp(plans, transport_env, transport_resources)
            session = SandboxSession.start(
                sandbox,
                RunEnvironmentView(
                    paths=agent_paths(sandbox, request),
                    prompt_notes=presentation.prompt_notes,
                    isolated=True,
                    cli_sandboxed=True,
                    host_device_reselect=False,
                    env_kind="slurm",
                    profile_execution="remote" if remote_profiling else "local",
                    editor_has_gpu=False,
                    framework_setup_timeout_seconds=config.job_timeout_seconds,
                    profiler_mcp_env=profiler_mcp[0],
                    profiler_mcp_resources=profiler_mcp[1],
                    cluster_evaluation=ClusterEvaluationFacts(self._settings, plans.plan),
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
        config_path = request.log_dir / "slurm-operator.toml"
        write_slurm_operator_toml(self._settings, config_path)
        capture_plan_path = (
            request.log_dir / "slurm-capture-plan.json" if remote_profiling else None
        )
        raw_accuracy = _command_argv(remote.accuracy_command)
        raw_benchmark = _command_argv(remote.benchmark_command)
        raw_profile = _command_argv(remote.profile_command)
        profile = policy.remote_argv(raw_profile) if raw_profile is not None else None
        accuracy = policy.remote_argv(raw_accuracy) if raw_accuracy is not None else None
        benchmark = policy.remote_argv(raw_benchmark) if raw_benchmark is not None else None
        evaluation_plan = SlurmEvaluationPlan(
            config_path=config_path,
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
            capture=capture_plan_path,
            plan=evaluation_plan,
            config_path=config_path,
            state_root=state_root,
            support_paths=support_paths,
        )

    def _start_command_broker(
        self, request: RunEnvironmentRequest, gpu: GpuCommands
    ) -> HostCommandBroker:
        """Start the host broker the container reaches for the agent's own GPU commands."""
        broker = HostCommandBroker(
            new_broker_socket_path(),
            # Only this session's own workspace: with parallel candidates each session has
            # its own broker, and none may run a command in another candidate's worktree.
            roots=RunRoots((request.workspace,)),
            transport=BrokerTransport(request.system.network, request.system.threads),
            gpu=gpu,
        )
        broker.start()
        return broker


def _start_transport_broker(
    config: SlurmConfig,
    request: RunEnvironmentRequest,
    plans: _ClusterPlans,
    brokers: list[HostBroker],
) -> tuple[tuple[tuple[str, str], ...], tuple[HostResource, ...]]:
    """Start the profiler's transport broker; return the env naming it and its mount."""
    if plans.capture is None:
        return (), ()
    roots = (
        request.workspace,
        plans.state_root,
        *request.run_owned_roots,
        *plans.support_paths.values(),
    )
    policy = slurm_transport_broker_policy(config, local_roots=roots)
    broker = SlurmProcessBroker(
        policy,
        new_broker_socket_path(),
        processes=request.system.slurm_processes,
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
    plans: _ClusterPlans,
    transport_env: tuple[tuple[str, str], ...],
    transport_resources: tuple[HostResource, ...],
) -> tuple[tuple[tuple[str, str], ...], tuple[HostResource, ...]]:
    """Return the environment and mounts of the remote capture's profiler server, if any."""
    if plans.capture is None:
        return (), ()
    return (
        (
            ("VIBESYS_SLURM_CONFIG", str(plans.config_path)),
            ("VIBESYS_SLURM_EVALUATOR_PLAN", str(plans.capture)),
            *transport_env,
        ),
        (
            HostResource(
                plans.state_root,
                HostResourceAccess.READ_WRITE,
                "Slurm cluster operation state and transfers",
            ),
            HostResource(
                plans.config_path, HostResourceAccess.READ_ONLY, "Slurm profiler configuration"
            ),
            HostResource(plans.capture, HostResourceAccess.READ_ONLY, "Slurm profiler plan"),
            *(
                HostResource(path, HostResourceAccess.READ_ONLY, f"Slurm support tree {name}")
                for name, path in sorted(plans.support_paths.items())
            ),
            *transport_resources,
        ),
    )
