"""The ``slurm`` run environment: a Docker editor whose trusted work runs on a cluster."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vs_runtime._brokered_session import BrokeredRunEnvironmentSession, HostBroker
from vs_runtime._container_paths import CONTAINER_FRAMEWORK_ROOT
from vs_runtime._container_runtime_policy import reject_docker_in_docker
from vs_runtime._host_command_bridge import (
    bridge_editor_extras,
    new_broker_socket_path,
    write_client_launcher,
)
from vs_runtime._run_environment import (
    AgentPaths,
    DockerEnvironment,
    DockerEnvironmentConfig,
    RunEnvironmentPresentation,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    RunEnvironmentView,
    SlurmEnvironmentFacts,
    _AgentPathSandbox,
    _command_argv,
    _make_host_sandbox,
    _materialize_effective_objective,
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
    GateKind,
    Gates,
    HostCommandBroker,
    RunRoots,
    SlurmCapturePlan,
    SlurmCommandGateRunner,
    SlurmEvaluationPlan,
    SlurmExecutionPolicy,
    SlurmProcessBroker,
    load_slurm_policy,
    trusted_profile_command,
    write_slurm_capture_plan,
    write_slurm_evaluation_plan,
)
from vs_slurm.api import SlurmConfig, SlurmSshTransport, load_slurm_config

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vs_agent.api.images import DockerBuildRunner

#: The framework's packages a profiler server imports, in the container, from
#: the read-only ``libs`` mount: the Slurm adapter and what it needs.
PROFILER_PYTHON_PACKAGES: tuple[str, ...] = (
    "vs-async-ops",
    "vs-evaluation",
    "vs-evaluator-protocol",
    "vs-project",
    "vs-sandbox",
    "vs-slurm",
)
_GATE_LAUNCHER = "vibesys-gate"


def _slurm_service_command(policy: SlurmExecutionPolicy) -> tuple[str, ...]:
    service = policy.remote_service()
    return () if service is None else service.command


def profiler_python_path(framework_root: Path) -> str:
    """Return the ``PYTHONPATH`` that lets a container's Python import the Slurm adapter.

    The profiler server runs in the container, with whatever Python its image
    provides and ``pydantic`` (which its MCP library needs). The framework
    packages it imports sit in the read-only ``libs`` mount, whose host path is
    also its container path.
    """
    sources = (framework_root / "libs" / name / "src" for name in PROFILER_PYTHON_PACKAGES)
    return os.pathsep.join((CONTAINER_FRAMEWORK_ROOT, *(str(path) for path in sources)))


@dataclass(frozen=True, slots=True)
class _ClusterPlans:
    """The evaluation and capture plans the run wrote, and the paths they name."""

    evaluator: Path
    capture: Path
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
    """

    isolated = False
    materialize_local_model_weights = False
    default_profiler_id = "rocprof"
    supported_profiler_ids: frozenset[str] | None = frozenset({"auto", "none", "rocprof"})
    backend_image: str | None = None
    requires_local_profiler_preflight = False

    def __init__(
        self,
        config_path: Path,
        docker: DockerEnvironmentConfig | None = None,
        gate_wrapper: Sequence[str] = DEFAULT_WRAPPER,
    ) -> None:
        """Bind the operator configuration and the injection seams.

        *docker* configures the editor container. *gate_wrapper* is the program
        that runs one trusted gate against the cluster; tests substitute a fake
        that stands in for it.
        """
        self.config_path = config_path.expanduser()
        self._docker = DockerEnvironment(docker or DockerEnvironmentConfig())
        self._gate_wrapper = tuple(gate_wrapper)

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
        return _PreparedRunEnvironment(
            SlurmEnvironmentFacts(service_command=_slurm_service_command(policy)),
            partial(self._open, request, config, policy),
        )

    def _open(
        self,
        request: RunEnvironmentRequest,
        config: SlurmConfig,
        policy: SlurmExecutionPolicy,
        presentation: RunEnvironmentPresentation,
    ) -> RunEnvironmentSession:
        profiler_tree = request.profiler_support_name
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
        plans = self._write_plans(request, config, policy, profiler_tree)
        brokers: list[HostBroker] = []
        try:
            transport_env, transport_resources = _start_transport_broker(
                config, request, plans, brokers
            )
            gates = self._start_gate_broker(request, plans)
            brokers.append(gates)
            launcher = write_client_launcher(request.log_dir / "slurm-gate", _GATE_LAUNCHER)
            sandbox = self._docker.build_editor(request, bridge_editor_extras(gates, launcher))
            objective = _materialize_effective_objective(request)
            session = SandboxSession.start(
                sandbox,
                RunEnvironmentView(
                    paths=AgentPaths(
                        objective=(
                            cast("_AgentPathSandbox", sandbox).agent_path(objective)
                            if objective is not None
                            else "OBJECTIVE.md"
                        ),
                        accuracy_command=(
                            shlex.join((str(launcher), "--gate", GateKind.ACCURACY.value))
                            if plans.has_accuracy
                            else None
                        ),
                        benchmark_command=(
                            shlex.join((str(launcher), "--gate", GateKind.BENCHMARK.value))
                            if plans.has_benchmark
                            else None
                        ),
                        profiler_support=(
                            request.profiler_support_name if request.profiler_support_path else None
                        ),
                    ),
                    prompt_notes=presentation.prompt_notes,
                    isolated=True,
                    cli_sandboxed=True,
                    host_device_reselect=False,
                    env_kind="slurm",
                    profile_execution="remote",
                    framework_setup_timeout_seconds=config.job_timeout_seconds,
                    profiler_mcp_env=(
                        ("VIBESYS_SLURM_CONFIG", str(self.config_path)),
                        ("VIBESYS_SLURM_EVALUATOR_PLAN", str(plans.capture)),
                        ("PYTHONPATH", profiler_python_path(request.framework_root)),
                        *transport_env,
                    ),
                    profiler_mcp_resources=(
                        *_profiler_resources(self.config_path, request, plans),
                        *transport_resources,
                    ),
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
                (request.profiler_support_name, request.profiler_support_path),
                *((name, path) for path, name in request.profiler_support_extra),
            )
            if name is not None and path is not None
        }
        state_root = request.log_dir / "slurm-cluster"
        state_root.mkdir(parents=True, exist_ok=True)
        evaluator_plan_path = request.log_dir / "slurm-evaluation-plan.json"
        capture_plan_path = request.log_dir / "slurm-capture-plan.json"
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
        self, request: RunEnvironmentRequest, plans: _ClusterPlans
    ) -> HostCommandBroker:
        broker = HostCommandBroker(
            new_broker_socket_path(),
            roots=RunRoots((request.workspace,), tuple(request.run_owned_roots)),
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


def _profiler_resources(
    config_path: Path, request: RunEnvironmentRequest, plans: _ClusterPlans
) -> tuple[HostResource, ...]:
    return (
        HostResource(
            plans.state_root,
            HostResourceAccess.READ_WRITE,
            "Slurm cluster operation state and transfers",
        ),
        HostResource(config_path, HostResourceAccess.READ_ONLY, "Slurm profiler configuration"),
        HostResource(plans.capture, HostResourceAccess.READ_ONLY, "Slurm profiler plan"),
        HostResource(
            request.framework_root / "libs", HostResourceAccess.READ_ONLY, "Slurm adapter libraries"
        ),
        *(
            HostResource(path, HostResourceAccess.READ_ONLY, f"Slurm support tree {name}")
            for name, path in sorted(plans.support_paths.items())
        ),
    )
