"""Run environment assembly over the existing sandbox implementations.

Layering:

    product composition -> RunEnvironment -> ComputeBackendImpl.make_sandbox -> Sandbox

Product composition prepares run resources before opening this session.

``RunEnvironment`` owns run-level execution policy for a location such as local,
Docker, or Modal.  It decides path exposure, bind mounts, execution constraints,
model-weight handling, prompt-visible paths, sandbox startup, and cleanup.  It
does not execute agent commands directly.

``ComputeBackendImpl.make_sandbox`` is the compute-platform factory.  It knows
how to construct a local or Docker sandbox for CUDA, Metal, or another
compute backend.  The Modal and SkyPilot run environments both request a
Docker sandbox for the local agent editor container; GPU-bound work
dispatches separately, through the candidate's own ``modal run`` entrypoint
or a SkyPilot job.

The concrete sandbox classes are still the command-execution abstraction.  They
run shell commands, read/write files, translate virtual paths, and manage the
container or remote process lifetime at the command layer.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import sys
import tempfile

# lint-waiver: LW-007062 [TC003]; Pydantic resolves this dataclass field annotation at runtime
from collections.abc import Mapping  # noqa: TC003
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from vs_agent.api import (
    DOCKER_PROVIDER_ENV,
    AgentBackend,
    auth_bind_mounts,
    auth_copy_paths,
    auth_env_passthrough,
    auth_env_vars,
    auth_paths,
)
from vs_project.api import RunEnvironmentRecord, RunResourceRequest
from vs_runtime import _boot_trace as boot_trace
from vs_runtime._docker_evaluator_tools import prepare_docker_evaluator_resources
from vs_runtime._objective_document import materialize_objective_document
from vs_runtime._trusted_evaluation import TrustedEvaluationPlan
from vs_runtime._trusted_evaluation_preparation import (
    REMOTE_EVALUATOR_TOOLS_ROOT,
    SANDBOX_EVALUATOR_TOOLS_ROOT,
    TrustedEvaluationCommandPaths,
    TrustedEvaluatorRequirements,
    evaluator_agent_toolchains,
    prepare_trusted_evaluation_plan,
    remote_evaluator_setup_command,
    required_evaluator_tools_root,
)
from vs_sandbox.api import (
    DeviceLease,
    EnvironmentBindMount,
    HostResource,
    HostResourceAccess,
    ProjectPathPolicy,
    SandboxKind,
    SandboxLifecycleHooks,
    SandboxSession,
    deduplicate_host_resources,
    evaluator_helpers,
    host_resource_for_mount,
    start_sandbox,
    stop_sandbox,
)
from vs_sandbox.api.docker_workspace import (
    DockerWorkspaceRepairError,
    docker_project_path_resources,
    remove_docker_workspace_child,
    repair_docker_workspace,
)
from vs_sandbox.api.evaluator_helpers import encode_setup_command
from vs_sandbox.api.evaluator_tools import (
    EvaluatorToolError,
    EvaluatorToolLifecycleHooks,
)
from vs_sandbox.api.skypilot import (
    ResolvedSkyPilotResources,
    SkyPilotBridge,
    SkyPilotJobRunner,
    load_cluster_profiles,
    resolve_profile,
    stable_cluster_name,
)
from vs_sandbox.api.slurm import (
    SlurmCapturePlan,
    SlurmEvaluationPlan,
    SlurmExecutionPolicy,
    SlurmProcessBroker,
    configured_capture_lifecycle,
    load_slurm_policy,
    trusted_profile_command,
    write_slurm_capture_plan,
    write_slurm_evaluation_plan,
)
from vs_sandbox.api.symlink_mounts import (
    collect_symlink_mounts,
    find_mount_root,
    symlink_lifecycle_hooks,
)
from vs_slurm.api import SlurmConfig, SlurmSshTransport, load_slurm_config

_RunEnvironmentName = Literal["local", "docker", "modal", "skypilot", "slurm"]
_RECORDED_ENVIRONMENT_NAMES: tuple[_RunEnvironmentName, ...] = (
    "local",
    "docker",
    "modal",
    "skypilot",
    "slurm",
)
_RUNTIME_OBJECTIVE_CONTAINER_PATH = "/opt/vibesys-runtime/objective.md"
"""The runtime resource declaration's ``agent_path`` for the effective objective.

Used in exactly one place: the ``HostResource`` :func:`_container_mount_plan`
declares for the materialized objective document. Every environment's
:class:`AgentPaths` instead asks the started sandbox's own
:meth:`~vs_sandbox.docker_sandbox.DockerSandbox.agent_path` for that document's
container path, so this constant and the resource declaration are the single
source of truth an environment consults."""
if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_project.api import StateNamespace
    from vs_sandbox.api import ComputeBackendImpl, Sandbox


@dataclass(frozen=True)
class RunEnvironmentSpec:
    """Run-environment selection at the CLI/config boundary.

    Environment-specific knobs stay inside ``options`` and are parsed only by the
    concrete environment selected by ``name``.
    """

    name: str = "local"
    options: Mapping[str, object] = field(default_factory=dict)
    resources: RunResourceRequest | None = None


@dataclass(frozen=True)
class AgentPaths:
    """Command and helper paths as agents should use them in the active environment."""

    objective: str = "OBJECTIVE.md"
    accuracy_command: str | None = None
    benchmark_command: str | None = None
    profiler_support: str | None = None


@dataclass(frozen=True)
class RunEnvironmentView:
    """Run-environment-neutral facts consumed by loops and agent construction."""

    paths: AgentPaths
    prompt_notes: str = ""
    isolated: bool = False
    cli_sandboxed: bool = False
    # Agent clients borrow the run-owned sandbox and bridge.
    share_agent_session: bool = False
    host_device_reselect: bool = True
    # Coarse environment label for diagnostics and adapter selection:
    # ``"local"`` | ``"docker"`` | ``"modal"`` | ``"skypilot"``.
    env_kind: str = "local"
    # Where a profiler must execute to observe the production hot path. Prompt
    # templates branch on this capability rather than on a concrete provider.
    profile_execution: Literal["local", "remote"] = "local"
    supports_parallel_candidate_evaluation: bool = False
    # Optional environment variable understood by the environment-owned
    # evaluator wrapper when the final trusted command should release its
    # deployment lease.
    deployment_release_env_var: str | None = None
    # Extra wall-clock budget for environment-owned setup that wraps a trusted
    # command, such as deploying a fresh service and waiting for readiness.
    framework_setup_timeout_seconds: int = 0
    profiler_mcp_env: tuple[tuple[str, str], ...] = ()
    profiler_mcp_resources: tuple[HostResource, ...] = ()


@dataclass(frozen=True)
class RunEnvironmentPresentation:
    """Product-authored text consumed by environment infrastructure."""

    prompt_notes: str
    runtime_document: str | None = None


@dataclass(frozen=True)
class LocalEnvironmentFacts:
    """Presentation facts for host-local execution."""


@dataclass(frozen=True)
class DockerEnvironmentFacts:
    """Presentation facts for Docker execution."""


@dataclass(frozen=True)
class SlurmEnvironmentFacts:
    """Presentation facts for a local editor with remote trusted execution.

    ``service_command`` is the operator-configured argv (remote interpreter
    already substituted) that trusted jobs use to start the candidate service
    from the candidate root, or empty when the operator configured none.
    """

    service_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkyPilotEnvironmentFacts:
    """Resolved SkyPilot facts needed by product presentation and opening."""

    resources: ResolvedSkyPilotResources
    runtime_container_path: str = "/opt/vibesys-runtime/environment.md"


@dataclass(frozen=True)
class ModalEnvironmentFacts:
    """Resolved Modal facts needed by product presentation and opening."""

    gpu: str
    app_name: str
    reference_path: str
    runtime_container_path: str = "/opt/vibesys-runtime/environment.md"


RunEnvironmentPresentationFacts = (
    LocalEnvironmentFacts
    | DockerEnvironmentFacts
    | SlurmEnvironmentFacts
    | SkyPilotEnvironmentFacts
    | ModalEnvironmentFacts
)


@dataclass(frozen=True)
class RunEnvironmentRequest:
    """Resolved inputs required to open a run environment."""

    log_dir: Path
    workspace: Path
    ref_dir: Path | None
    backend: ComputeBackendImpl
    agent_backend: str | None
    cli_provider: str | None
    run_id: str
    framework_root: Path
    objective: str | None = None
    # Authored run state belongs to git_history_root, including when workspace
    # is a candidate revision that predates the objective's committed document.
    objective_document: Path | None = None
    accuracy_command: str | None = None
    benchmark_command: str | None = None
    benchmark_output_argument: str | None = None
    profile_command: str | None = None
    profile_timeout_seconds: int | None = None
    evaluator_requirements: TrustedEvaluatorRequirements = field(
        default_factory=TrustedEvaluatorRequirements
    )
    profiler_support_path: str | None = None
    profiler_support_name: str | None = None
    # Sibling support directories staged alongside the primary profiler
    # support dir (the shared capture-runtime package, plus any profiler
    # kind's own extra plugin dirs), as (host_source_path, workspace_name)
    # pairs. Meaningless without profiler_support_path/name set.
    profiler_support_extra: tuple[tuple[str, str], ...] = ()
    git_history_root: Path | None = None
    # Host directories the run owns besides ``workspace``, such as the
    # directory holding its candidate worktrees. Agent tools bound once per
    # run (the profiler) may stage any of them to a remote executor.
    run_owned_roots: tuple[Path, ...] = ()
    environment_bind_mounts: tuple[EnvironmentBindMount, ...] = ()
    seeded_workspace_paths: tuple[str, ...] = ()
    log: Callable[[str], None] | None = None
    project_path_policy: ProjectPathPolicy = field(default_factory=ProjectPathPolicy)
    state_namespace: StateNamespace | None = None
    #: Root of the run's dedicated agent CLI homes; ``None`` when the run has
    #: no machine-local state (agents then keep the provider's default home).
    agent_homes_dir: Path | None = None


class _AgentPathSandbox(Protocol):
    """The one lookup ``AgentPaths`` construction needs from a started sandbox.

    Narrower than ``Sandbox``: a host-only sandbox never
    reaches this contract (``LocalEnvironment`` builds host paths directly),
    while every ``SandboxKind.DOCKER`` build
    (:class:`~vs_sandbox.docker_sandbox.DockerSandbox`) satisfies it.
    """

    def agent_path(self, host_path: Path | str) -> str: ...


class RunEnvironmentSession(Protocol):
    """Context-managed sandbox session owned by a run environment."""

    sandbox: Sandbox
    view: RunEnvironmentView

    def __enter__(self) -> RunEnvironmentSession:
        """Enter the session and return its context-managed handle."""
        ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Release session resources when leaving the context."""
        ...

    def close(self) -> None:
        """Stop resources owned by this run session."""
        ...


@dataclass(slots=True)
class _BrokeredRunEnvironmentSession:
    """Own a local agent session and its host-side Slurm transport broker."""

    delegate: RunEnvironmentSession
    broker: SlurmProcessBroker
    _closed: bool = False

    @property
    def sandbox(self) -> Sandbox:
        return self.delegate.sandbox

    @sandbox.setter
    def sandbox(self, value: Sandbox) -> None:
        self.delegate.sandbox = value

    @property
    def view(self) -> RunEnvironmentView:
        return self.delegate.view

    @view.setter
    def view(self, value: RunEnvironmentView) -> None:
        self.delegate.view = value

    def __enter__(self) -> _BrokeredRunEnvironmentSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.broker.close()
        finally:
            self.delegate.close()


class RunEnvironmentResources:
    """Concrete owner of one environment session and its compute-device lease."""

    def __init__(
        self,
        request: RunEnvironmentRequest,
        session: RunEnvironmentSession,
        device: DeviceLease,
        ownership: ExitStack,
        open_session: Callable[[RunEnvironmentRequest], RunEnvironmentSession],
    ) -> None:
        self.request = request
        self.session = session
        self.device = device
        self._ownership = ownership
        self._open_session = open_session
        self._closed = False

    @property
    def view(self) -> RunEnvironmentView:
        """Return environment facts resolved while opening the session."""
        return self.session.view

    def reselect_device(self) -> None:
        """Re-pick the active compute device when the environment permits it."""
        self.device.reselect()

    def open_workspace(self, request: RunEnvironmentRequest) -> RunEnvironmentResources:
        """Open a child workspace session that borrows this run's device lease."""
        return open_workspace_environment_resources(
            request,
            self._open_session,
            device=self.device,
        )

    def open_session(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:
        """Open an independently owned session using this environment's factory."""
        return self._open_session(request)

    def close(self) -> None:
        """Release owned resources in reverse construction order exactly once."""
        if self._closed:
            return
        self._closed = True
        self._ownership.close()


def _close_failed_environment_resources(
    ownership: ExitStack, construction_error: BaseException
) -> None:
    try:
        ownership.close()
    except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-994217 [BLE001]; cleanup must preserve the environment construction failure while reporting teardown failure.
        construction_error.add_note(
            "Additional error while cleaning up environment resources: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )


def open_run_environment_resources(
    request: RunEnvironmentRequest,
    open_session: Callable[[RunEnvironmentRequest], RunEnvironmentSession],
) -> RunEnvironmentResources:
    """Open a root session and own its newly started device lease."""
    ownership = ExitStack()
    try:
        with boot_trace.span("environment_open"):
            session = ownership.enter_context(open_session(request))
        with boot_trace.span("device_monitor_start"):
            device = DeviceLease(
                request.backend,
                log_dir=request.log_dir,
                run_environment_view=session.view,
            )
            ownership.callback(device.close)
            device.start_monitor()
        return RunEnvironmentResources(request, session, device, ownership, open_session)
    except BaseException as construction_error:
        _close_failed_environment_resources(ownership, construction_error)
        raise


def open_workspace_environment_resources(
    request: RunEnvironmentRequest,
    open_session: Callable[[RunEnvironmentRequest], RunEnvironmentSession],
    *,
    device: DeviceLease,
) -> RunEnvironmentResources:
    """Open a workspace session while borrowing the root run's device lease."""
    ownership = ExitStack()
    try:
        session = ownership.enter_context(open_session(request))
        return RunEnvironmentResources(request, session, device, ownership, open_session)
    except BaseException as construction_error:
        _close_failed_environment_resources(ownership, construction_error)
        raise


@dataclass(frozen=True)
class _PreparedRunEnvironment:
    """A resolved environment plan awaiting product-authored presentation."""

    presentation_facts: RunEnvironmentPresentationFacts
    _open: Callable[[RunEnvironmentPresentation], RunEnvironmentSession]

    def open(self, presentation: RunEnvironmentPresentation) -> RunEnvironmentSession:
        """Open the resolved environment with explicit product presentation."""
        return self._open(presentation)


class RunEnvironment(Protocol):
    """Environment policy for run execution and candidate evaluation."""

    isolated: bool
    materialize_local_model_weights: bool
    default_profiler_id: str
    supported_profiler_ids: frozenset[str] | None
    backend_image: str | None
    requires_local_profiler_preflight: bool

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Resolve environment facts without starting owned resources."""
        ...

    def repair_workspace(
        self, workspace: Path, *, backend: ComputeBackendImpl, log: Callable[[str], None]
    ) -> None:
        """Repair ownership or permissions for the workspace when required."""
        ...

    def remove_workspace_child(
        self, workspace: Path, rel_path: str, *, backend: ComputeBackendImpl
    ) -> bool:
        """Remove a workspace-relative child and report whether it is absent."""
        ...


class _NoopWorkspaceRecovery:
    def repair_workspace(
        self,
        workspace: Path,
        *,
        backend: ComputeBackendImpl,
        log: Callable[[str], None],
    ) -> None:
        del workspace, backend, log

    def remove_workspace_child(
        self,
        workspace: Path,
        rel_path: str,
        *,
        backend: ComputeBackendImpl,
    ) -> bool:
        del workspace, rel_path, backend
        return False


class LocalEnvironment(_NoopWorkspaceRecovery):
    """Run agents directly on the host filesystem."""

    isolated: bool = False
    materialize_local_model_weights: bool = True
    default_profiler_id = "nsys"
    supported_profiler_ids: frozenset[str] | None = None
    backend_image: str | None = None
    requires_local_profiler_preflight = True

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Resolve the host-local presentation facts."""
        return _PreparedRunEnvironment(LocalEnvironmentFacts(), partial(self._open, request))

    def _open(
        self, request: RunEnvironmentRequest, presentation: RunEnvironmentPresentation
    ) -> RunEnvironmentSession:
        """Create a host-local sandbox and its agent path view."""
        del presentation
        objective_document = _materialize_effective_objective(request)
        requirements = request.evaluator_requirements
        tools = requirements.tools
        lifecycle_hooks: list[SandboxLifecycleHooks] = []
        if tools:
            lifecycle_hooks.append(
                EvaluatorToolLifecycleHooks(
                    tools,
                    required_evaluator_tools_root(requirements, request.workspace),
                )
            )
        sandbox = request.backend.make_sandbox(
            SandboxKind.LOCAL,
            host_workspace=str(request.workspace),
            log_path=None,
            bind_mounts=[],
            extra_env={},
            extra_init_commands=[],
            lifecycle_hooks=lifecycle_hooks,
        )
        evaluation = _prepare_evaluation_plan(
            request,
            requirements,
            TrustedEvaluationCommandPaths(
                source_project_root=request.workspace,
                runtime_project_root=str(request.workspace),
                python_executable=sys.executable,
                runtime_package_root=(
                    str(requirements.package_root)
                    if requirements.package_root is not None
                    else None
                ),
            ),
        )
        return SandboxSession.borrowed(
            sandbox=sandbox,
            view=RunEnvironmentView(
                paths=AgentPaths(
                    objective=(
                        str(objective_document)
                        if objective_document is not None
                        else "OBJECTIVE.md"
                    ),
                    accuracy_command=evaluation.accuracy_command,
                    benchmark_command=evaluation.benchmark_command,
                    profiler_support=request.profiler_support_path,
                ),
            ),
        )


def _slurm_service_command(policy: SlurmExecutionPolicy) -> tuple[str, ...]:
    service = policy.remote_service()
    return () if service is None else service.command


class SlurmEnvironment(_NoopWorkspaceRecovery):
    """Keep editing local while trusted gates and ROCprof run through Slurm."""

    isolated = False
    materialize_local_model_weights = False
    default_profiler_id = "rocprof"
    supported_profiler_ids: frozenset[str] | None = frozenset({"auto", "none", "rocprof"})
    backend_image: str | None = None
    requires_local_profiler_preflight = False

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser()

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> SlurmEnvironment:
        """Resolve only the external operator configuration path."""
        value = options.get("config_path", "~/.config/vibesys/slurm.toml")
        return cls(Path(str(value)))

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Validate external policy before opening the local editor sandbox."""
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
            # Validate the trusted load before opening resources owned by the delegate.
            trusted_profile_command(
                config,
                policy,
                _command_argv(request.profile_command),
                profiler_tree=profiler_tree,
                workload_timeout_seconds=request.profile_timeout_seconds,
            )
        delegate = (
            LocalEnvironment().prepare(request).open(RunEnvironmentPresentation(prompt_notes=""))
        )
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
        cluster_state_root = request.log_dir / "slurm-cluster"
        cluster_state_root.mkdir(parents=True, exist_ok=True)
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
                cluster_state_root=cluster_state_root,
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
                cluster_state_root=cluster_state_root,
                profile_command=profile,
                profile_timeout_seconds=request.profile_timeout_seconds,
                support_paths=support_paths,
            ),
        )
        prefix = (
            sys.executable,
            "-m",
            "vs_sandbox.slurm_command",
            "--plan",
            str(evaluator_plan_path),
        )
        broker: SlurmProcessBroker | None = None
        broker_env: tuple[tuple[str, str], ...] = ()
        broker_resources: tuple[HostResource, ...] = ()
        if isinstance(config.transport, SlurmSshTransport):
            broker = SlurmProcessBroker(
                config,
                Path(tempfile.gettempdir()) / f"vss-{secrets.token_hex(8)}.sock",
                local_roots=(
                    request.workspace,
                    cluster_state_root,
                    *request.run_owned_roots,
                    *support_paths.values(),
                ),
            )
            broker.start()
            broker_env = (
                ("VIBESYS_SLURM_BROKER_SOCKET", str(broker.socket_path)),
                ("VIBESYS_SLURM_BROKER_TOKEN", broker.token),
            )
            broker_resources = (
                HostResource(
                    broker.socket_path,
                    HostResourceAccess.READ_WRITE,
                    "host-owned Slurm transport broker",
                ),
            )
        delegate.view = replace(
            delegate.view,
            paths=replace(
                delegate.view.paths,
                accuracy_command=shlex.join((*prefix, "accuracy")) if accuracy else None,
                benchmark_command=shlex.join((*prefix, "benchmark")) if benchmark else None,
            ),
            prompt_notes=presentation.prompt_notes,
            env_kind="slurm",
            profile_execution="remote",
            supports_parallel_candidate_evaluation=True,
            framework_setup_timeout_seconds=config.job_timeout_seconds,
            profiler_mcp_env=(
                ("VIBESYS_SLURM_CONFIG", str(self.config_path)),
                ("VIBESYS_SLURM_EVALUATOR_PLAN", str(capture_plan_path)),
                *broker_env,
            ),
            profiler_mcp_resources=(
                HostResource(
                    cluster_state_root,
                    HostResourceAccess.READ_WRITE,
                    "Slurm cluster operation state and transfers",
                ),
                HostResource(
                    self.config_path,
                    HostResourceAccess.READ_ONLY,
                    "Slurm profiler configuration",
                ),
                HostResource(
                    capture_plan_path,
                    HostResourceAccess.READ_ONLY,
                    "Slurm profiler plan",
                ),
                HostResource(
                    request.framework_root / "libs",
                    HostResourceAccess.READ_ONLY,
                    "Slurm adapter libraries",
                ),
                *(
                    HostResource(
                        path,
                        HostResourceAccess.READ_ONLY,
                        f"Slurm support tree {name}",
                    )
                    for name, path in sorted(support_paths.items())
                ),
                *broker_resources,
            ),
        )
        return _BrokeredRunEnvironmentSession(delegate, broker) if broker is not None else delegate


@dataclass(frozen=True)
class DockerEnvironmentConfig:
    """Optional image override for the Docker run environment."""

    image: str | None = None


class DockerEnvironment:
    """Run agents and evaluator commands in a Docker sandbox."""

    isolated = True
    requires_local_profiler_preflight = True
    materialize_local_model_weights = True
    default_profiler_id = "nsys"
    supported_profiler_ids: frozenset[str] | None = None

    def __init__(self, config: DockerEnvironmentConfig) -> None:
        """Configure Docker execution from its image settings."""
        self.config = config
        self.backend_image = config.image

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> DockerEnvironment:
        """Build Docker environment configuration from CLI options."""
        image = options.get("image")
        return cls(DockerEnvironmentConfig(image=str(image) if image else None))

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Resolve Docker presentation facts."""
        return _PreparedRunEnvironment(DockerEnvironmentFacts(), partial(self._open, request))

    def _open(
        self, request: RunEnvironmentRequest, presentation: RunEnvironmentPresentation
    ) -> RunEnvironmentSession:
        """Start the Docker sandbox and resolve candidate-facing paths."""
        image_helpers = import_module("vs_agent.api.images")
        requirements = request.evaluator_requirements
        # The task image, when a task has a Dockerfile, is built by the
        # headless entrypoint and arrives here as the backend image; only the
        # agent layer is applied on top of it.
        container_image = image_helpers.agent_image(
            _docker_backend_image(request),
            toolchains=evaluator_agent_toolchains(requirements),
        )
        resources, docker_symlinks = _container_mount_plan(request)
        resources += list(
            prepare_docker_evaluator_resources(
                requirements,
                request.workspace,
                backend=request.backend,
                log_dir=request.log_dir,
                container_image=container_image,
            )
        )
        resolved_cli = _cli_container_env(request)
        cli_provider_env: dict[str, str] = {}
        auth_files: list[tuple[str, str]] = []
        if resolved_cli is not None:
            provider, cli_provider_env = resolved_cli
            auth_files = auth_copy_paths(provider)
        cli_provider_env.setdefault("UV_CACHE_DIR", "/workspace/.cache/uv")
        # The agent image's baked toolchain root is read-only (agent.Dockerfile
        # chmods /opt/cargo a+rX, deliberately: an agent cannot apt/cargo
        # install mid-round). A Cargo invocation that needs a crate the image
        # did not prebuild -- an evaluator's own trusted-runner build, most
        # commonly -- still has to create its registry cache somewhere, so
        # CARGO_HOME is redirected onto the writable, bind-mounted workspace.
        # This is unrelated to which crates a candidate build needs: one with
        # no external dependencies never touches the registry at all.
        cli_provider_env.setdefault("CARGO_HOME", "/workspace/.cache/cargo")
        if request.git_history_root is not None:
            cli_provider_env.setdefault("VIBESYS_GIT_HISTORY", "/opt/vibesys-history")
        resources = deduplicate_host_resources(resources)
        lifecycle_hooks = symlink_lifecycle_hooks(docker_symlinks)

        sandbox = request.backend.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(request.workspace),
            log_path=request.log_dir / "docker.log",
            bind_mounts=[],
            resources=resources,
            extra_env=cli_provider_env,
            auth_files=auth_files,
            lifecycle_hooks=lifecycle_hooks,
            container_image=container_image,
        )
        log: Callable[[str], None] = request.log or (lambda _: None)
        log(f"[docker] starting container with image {container_image}")
        return SandboxSession.start(
            sandbox=sandbox,
            view=RunEnvironmentView(
                paths=_isolated_paths(
                    request,
                    cast("_AgentPathSandbox", sandbox),
                    evaluator_tools_root=SANDBOX_EVALUATOR_TOOLS_ROOT,
                ),
                prompt_notes=presentation.prompt_notes,
                isolated=True,
                cli_sandboxed=True,
                env_kind="docker",
            ),
        )

    def repair_workspace(
        self, workspace: Path, *, backend: ComputeBackendImpl, log: Callable[[str], None]
    ) -> None:
        """Chown workspace files back to the host user after Docker writes."""
        try:
            repair_docker_workspace(workspace, image=getattr(backend, "image", "ubuntu:latest"))
        except DockerWorkspaceRepairError as exc:
            log(f"[warn] {exc}")

    def remove_workspace_child(
        self, workspace: Path, rel_path: str, *, backend: ComputeBackendImpl
    ) -> bool:
        """Remove a workspace-relative child inside the Docker sandbox."""
        return remove_docker_workspace_child(
            workspace,
            rel_path,
            image=getattr(backend, "image", "ubuntu:latest"),
        )


#: The Modal client a candidate's ``modal run`` needs inside the editor
#: container; baked into that environment's agent image.
_MODAL_EDITOR_PIP_EXTRAS: tuple[str, ...] = ("modal>=0.66",)


@dataclass(frozen=True)
class ModalEnvironmentConfig:
    """Configuration for Modal-backed candidate evaluations."""

    image: str | None = None
    gpu: str = "H100!"
    model_volume: str | None = None
    app: str = "vibesys"
    entrypoint: str | None = None


@dataclass(frozen=True)
class SkyPilotEnvironmentConfig:
    """Operator selection for SkyPilot-backed remote evaluation."""

    image: str | None
    profile: str
    profiles_file: Path
    executable: str = "sky"
    resources: RunResourceRequest | None = None


@dataclass
class _SkyPilotRunEnvironmentSession:
    sandbox: Sandbox
    view: RunEnvironmentView
    bridge: SkyPilotBridge
    _closed: bool = False

    def __enter__(self) -> _SkyPilotRunEnvironmentSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            stop_sandbox(self.sandbox)
        finally:
            self.bridge.close()


class SkyPilotEnvironment(DockerEnvironment):
    """CPU-only Docker editor with host-mediated SkyPilot evaluation."""

    config: SkyPilotEnvironmentConfig
    requires_local_profiler_preflight = True
    materialize_local_model_weights = False
    default_profiler_id = "none"
    supported_profiler_ids: frozenset[str] | None = frozenset({"auto", "none"})

    def __init__(self, config: SkyPilotEnvironmentConfig) -> None:
        """Create an environment from operator-owned settings."""
        self.config = config
        self.backend_image = config.image

    @classmethod
    def from_options(
        cls,
        options: Mapping[str, object],
        resources: RunResourceRequest | None = None,
    ) -> SkyPilotEnvironment:
        """Validate environment-specific launch options."""
        profile = options.get("profile")
        if not isinstance(profile, str) or not profile:
            message = "SkyPilot requires a non-empty cluster profile"
            raise ValueError(message)
        raw_path = options.get("profiles_file")
        profiles_file = (
            Path(str(raw_path)).expanduser()
            if raw_path
            else Path("~/.config/vibesys/clusters.toml").expanduser()
        )
        return cls(
            SkyPilotEnvironmentConfig(
                image=str(options["image"]) if options.get("image") else None,
                profile=profile,
                profiles_file=profiles_file,
                executable=str(options.get("executable") or "sky"),
                resources=resources,
            )
        )

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Resolve and validate SkyPilot resources without starting them."""
        if self.config.resources is None:
            message = "SkyPilot requires portable run resources"
            raise ValueError(message)
        if request.state_namespace is None:
            message = "SkyPilot requires a machine-local state namespace"
            raise ValueError(message)
        state_namespace = request.state_namespace
        profiles = load_cluster_profiles(self.config.profiles_file)
        resources = resolve_profile(profiles, self.config.profile, self.config.resources)
        facts = SkyPilotEnvironmentFacts(resources=resources)
        return _PreparedRunEnvironment(
            facts,
            partial(
                self._open_skypilot,
                request,
                resources,
                stable_cluster_name(request.run_id, resources),
                state_namespace,
            ),
        )

    def _open_skypilot(
        self,
        request: RunEnvironmentRequest,
        cluster_resources: ResolvedSkyPilotResources,
        cluster_name: str,
        state_namespace: StateNamespace,
        presentation: RunEnvironmentPresentation,
    ) -> RunEnvironmentSession:
        """Open the bridge and CPU-only local editor container.

        The editor container starts from the same agent image the plain
        Docker path builds, pushed to and pulled back from a registry the
        same way :class:`ModalEnvironment` does (see its ``open`` docstring
        for why: this backend's Docker daemon cannot be assumed to already
        have the image locally). It is unrelated to the ``image_id`` the
        actual accelerator job later runs on: that image is an
        operator-declared field of the cluster profile
        (``SkyPilotProfile.remote_runtime_image``), chosen for the
        accuracy/benchmark command's own runtime needs (CUDA/ROCm, etc.), not
        for running agent CLIs, and this change leaves it untouched.
        """
        runtime_presentation = _required_runtime_document(presentation, "SkyPilot")
        requirements = request.evaluator_requirements
        evaluation = _prepare_evaluation_plan(
            request,
            requirements,
            TrustedEvaluationCommandPaths(
                source_project_root=request.workspace,
                runtime_project_root=".",
                python_executable="python3",
                runtime_package_root=(
                    ".vibesys-evaluator-package" if requirements.package_root is not None else None
                ),
                runtime_tools_root=REMOTE_EVALUATOR_TOOLS_ROOT,
            ),
        )
        commands: dict[str, tuple[str, ...]] = {}
        for kind, command_text in (
            ("accuracy", evaluation.accuracy_command),
            ("benchmark", evaluation.benchmark_command),
        ):
            command = shlex.split(command_text) if command_text is not None else None
            if command is not None:
                commands[kind] = tuple(command)

        log = request.log or _noop_log
        bridge = SkyPilotBridge(
            runner=SkyPilotJobRunner(executable=self.config.executable),
            cluster_name=cluster_name,
            resources=cluster_resources,
            workspace=request.workspace,
            evaluator_package_root=requirements.package_root,
            hidden_paths=request.project_path_policy.hidden_paths,
            commands=commands,
            framework_setup_command=remote_evaluator_setup_command(requirements),
            benchmark_output_argument=request.benchmark_output_argument,
            state_namespace=state_namespace,
            socket_path=request.log_dir / "skypilot-bridge.sock",
            log=log,
        )
        try:
            bridge.start()

            image_helpers = import_module("vs_agent.api.images")

            container_image = image_helpers.agent_image(
                _docker_backend_image(request),
                toolchains=evaluator_agent_toolchains(requirements),
            )
            container_image = _ensure_pushed_for_remote_backend(
                container_image,
                ensure_pushed=image_helpers.ensure_pushed,
                backend_label="SkyPilot",
            )

            resources, docker_symlinks = _container_mount_plan(request)
            cli_provider_env, auth_files = _cli_provider_env_and_auth_files(request)
            cli_provider_env.setdefault("UV_CACHE_DIR", "/workspace/.cache/uv")
            helper_source = evaluator_helpers.SKYPILOT_EVALUATOR_HELPER
            helper_path = "/opt/vibesys-skypilot-evaluator.py"
            socket_path = "/opt/vibesys-skypilot/bridge.sock"
            caller_state_path = "/opt/vibesys-skypilot/caller-state"
            caller_state = state_namespace.external_directory("caller")
            cli_provider_env["VIBESYS_SKYPILOT_CALLER_STATE"] = caller_state_path
            resources.extend(
                host_resource_for_mount(host, container, read_only=read_only)
                for host, container, read_only in (
                    (str(helper_source), helper_path, True),
                    (str(bridge.socket_path), socket_path, False),
                    (str(caller_state), caller_state_path, False),
                )
            )
            runtime_document = request.log_dir / "runtime-environment.md"
            runtime_document.write_text(runtime_presentation)
            runtime_path = "/opt/vibesys-runtime/environment.md"
            resources.append(
                host_resource_for_mount(str(runtime_document), runtime_path, read_only=True)
            )
            sandbox = request.backend.make_sandbox(
                SandboxKind.DOCKER,
                host_workspace=str(request.workspace),
                log_path=request.log_dir / "docker.log",
                bind_mounts=[],
                resources=deduplicate_host_resources(resources),
                extra_env=cli_provider_env,
                auth_files=auth_files,
                lifecycle_hooks=symlink_lifecycle_hooks(docker_symlinks),
                container_image=container_image,
                attach_accelerator=False,
            )
            start_sandbox(sandbox)
        except Exception:
            bridge.close()
            raise

        agent_path_sandbox = cast("_AgentPathSandbox", sandbox)
        objective_document = _materialize_effective_objective(request)
        prefix = f"python {helper_path} --socket {socket_path}"
        return _SkyPilotRunEnvironmentSession(
            sandbox=sandbox,
            bridge=bridge,
            view=RunEnvironmentView(
                paths=AgentPaths(
                    objective=(
                        agent_path_sandbox.agent_path(objective_document)
                        if objective_document is not None
                        else "OBJECTIVE.md"
                    ),
                    accuracy_command=(f"{prefix} accuracy" if "accuracy" in commands else None),
                    benchmark_command=(f"{prefix} benchmark" if "benchmark" in commands else None),
                    profiler_support=None,
                ),
                prompt_notes=presentation.prompt_notes,
                isolated=True,
                cli_sandboxed=True,
                share_agent_session=True,
                host_device_reselect=False,
                env_kind="skypilot",
                profile_execution="remote",
                supports_parallel_candidate_evaluation=False,
            ),
        )


class ModalEnvironment(_NoopWorkspaceRecovery):
    """Run candidate evaluations as Modal deployments."""

    isolated = True
    requires_local_profiler_preflight = True
    materialize_local_model_weights = False
    default_profiler_id = "torch"
    supported_profiler_ids: frozenset[str] | None = frozenset({"auto", "torch", "none"})

    def __init__(self, config: ModalEnvironmentConfig) -> None:
        """Configure Modal execution from its deployment settings."""
        self.config = config
        self.model_volume: str | None = config.model_volume
        self.backend_image = config.image

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> ModalEnvironment:
        """Build Modal environment configuration from CLI options."""
        return cls(
            ModalEnvironmentConfig(
                image=str(options["image"]) if options.get("image") else None,
                gpu=str(options.get("gpu") or "H100!"),
                model_volume=(
                    str(options["model_volume"]) if options.get("model_volume") else None
                ),
                app=str(options.get("app") or "vibesys"),
                entrypoint=(str(options["entrypoint"]) if options.get("entrypoint") else None),
            )
        )

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Resolve Modal presentation facts without starting resources."""
        app_name = _modal_app_name(request.run_id, fallback=self.config.app)
        facts = ModalEnvironmentFacts(
            gpu=self.config.gpu,
            app_name=app_name,
            reference_path=_reference_container_path(request).removeprefix("/workspace/"),
        )
        return _PreparedRunEnvironment(
            facts,
            partial(self._open, request, app_name),
        )

    def _open(
        self,
        request: RunEnvironmentRequest,
        app_name: str,
        presentation: RunEnvironmentPresentation,
    ) -> RunEnvironmentSession:
        """Open the Modal-via-Docker run environment.

        Architecture (refactor April 2026): the agent (codex CLI) runs inside
        a *local* Docker container that does file editing only. GPU-bound
        execution dispatches to Modal via the candidate's declared ``modal run``
        entrypoint; we mount the host's ``~/.modal.toml`` into the container
        so those calls authenticate.

        We retain the host-side Modal Volume bootstrap (model + optional
        draft) so the implementer's ``modal.Volume.from_name(...)`` calls
        resolve. The previous "long-lived Modal sandbox running codex inside"
        architecture is gone: it caused HOME-leak auth bugs,
        codex-vs-model-weight memory contention, and per-run sandbox
        cold-start overhead that this design eliminates.

        The container starts from the same agent image the plain Docker path
        builds (:func:`~vs_agent.api.images.agent_image`), pushed to and
        pulled back from a registry (:func:`~vs_agent.api.images.ensure_pushed`),
        since this backend's Docker daemon is not guaranteed to already have
        it locally the way the local ``--docker`` path's is. Nothing installs
        anything at container start any more, including the Modal Python SDK
        an earlier revision ``pip install``ed here: that install already ran
        through ``extra_init_commands``, which ``DockerSandbox`` (the sandbox
        class every ``SandboxKind.DOCKER`` construction here actually builds)
        has ignored ever since the agent-image work landed, so removing it is
        deleting dead code, not taking away a behavior that ran. A candidate
        whose declared ``modal run`` entrypoint needs the ``modal`` package
        still needs something to install it; see the accompanying report for
        the gap this surfaces.
        """
        runtime_presentation = _required_runtime_document(presentation, "Modal")
        # Host-side: ensure Modal Volumes exist for the model + optional
        # draft.  These run before the Docker container starts and are
        # idempotent (skip-if-ready sentinel).
        self._ensure_model_volume(request)
        self._ensure_draft_volume(request)

        image_helpers = import_module("vs_agent.api.images")

        requirements = request.evaluator_requirements
        container_image = image_helpers.agent_image(
            _docker_backend_image(request),
            toolchains=evaluator_agent_toolchains(requirements),
            pip_extras=_MODAL_EDITOR_PIP_EXTRAS,
        )
        container_image = _ensure_pushed_for_remote_backend(
            container_image,
            ensure_pushed=image_helpers.ensure_pushed,
            backend_label="Modal",
        )

        resources, docker_symlinks = _container_mount_plan(request)
        cli_provider_env, auth_files = _cli_provider_env_and_auth_files(request)
        cli_provider_env.setdefault("UV_CACHE_DIR", "/workspace/.cache/uv")
        if request.git_history_root is not None:
            cli_provider_env.setdefault("VIBESYS_GIT_HISTORY", "/opt/vibesys-history")
        cli_provider_env["VIBESYS_MODAL_APP_NAME"] = app_name
        runtime_document = request.log_dir / "runtime-environment.md"
        runtime_document.write_text(runtime_presentation)
        runtime_container_path = "/opt/vibesys-runtime/environment.md"
        resources.append(
            host_resource_for_mount(str(runtime_document), runtime_container_path, read_only=True)
        )
        evaluator_helper = evaluator_helpers.MODAL_EVALUATOR_HELPER
        evaluator_container_path = "/opt/vibesys-modal-evaluator.py"
        resources.append(
            host_resource_for_mount(str(evaluator_helper), evaluator_container_path, read_only=True)
        )

        # Mount host Modal auth so `modal run` inside the container
        # authenticates as the host user. The Modal SDK reads them from the
        # HOME of the user the container runs as, the agent image's
        # non-root ``agent`` user, not root. Deferred: the Docker sandbox
        # module imports the agent stack, which this module must not load.
        agent_home = import_module("vs_sandbox.api").AGENT_HOME

        modal_auth = Path.home() / ".modal.toml"
        if modal_auth.exists():
            resources.append(
                host_resource_for_mount(
                    str(modal_auth), f"{agent_home}/.modal.toml", read_only=True
                )
            )
        modal_config_dir = Path.home() / ".modal"
        if modal_config_dir.is_dir():
            resources.append(
                host_resource_for_mount(
                    str(modal_config_dir), f"{agent_home}/.modal", read_only=True
                )
            )

        resources = deduplicate_host_resources(resources)
        lifecycle_hooks = symlink_lifecycle_hooks(docker_symlinks)

        sandbox = request.backend.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(request.workspace),
            log_path=request.log_dir / "docker.log",
            bind_mounts=[],
            resources=resources,
            extra_env=cli_provider_env,
            auth_files=auth_files,
            lifecycle_hooks=lifecycle_hooks,
            container_image=container_image,
            attach_accelerator=False,
        )
        log: Callable[[str], None] = request.log or (lambda _: None)
        log(
            "[modal] starting local Docker editor; GPU work will dispatch "
            "to Modal via the candidate's declared `modal run` entrypoint"
        )
        setup_timeout_seconds = 1200
        evaluator_arguments = ["python", evaluator_container_path]
        if self.config.entrypoint is not None:
            evaluator_arguments.extend(("--entrypoint", self.config.entrypoint))
        evaluator_arguments.extend(("--readiness-timeout-seconds", str(setup_timeout_seconds)))
        remote_setup = remote_evaluator_setup_command(requirements, preserve_bootstrap=True)
        if remote_setup is not None:
            evaluator_arguments.extend(
                ("--setup-command-base64", encode_setup_command(("sh", "-c", remote_setup)))
            )
        if requirements.package_root is not None:
            evaluator_arguments.extend(
                ("--evaluator-package-root", "/opt/vibesys-evaluator-package")
            )
        evaluator_prefix = f"{shlex.join(evaluator_arguments)} --"
        agent_path_sandbox = cast("_AgentPathSandbox", sandbox)
        objective_document = _materialize_effective_objective(request)
        evaluation = _prepare_evaluation_plan(
            request,
            requirements,
            TrustedEvaluationCommandPaths(
                source_project_root=request.workspace,
                runtime_project_root="/workspace",
                python_executable="python3",
                runtime_package_root=(
                    ".vibesys-evaluator-package" if requirements.package_root is not None else None
                ),
                runtime_tools_root=REMOTE_EVALUATOR_TOOLS_ROOT,
            ),
        )
        return SandboxSession.start(
            sandbox=sandbox,
            view=RunEnvironmentView(
                paths=AgentPaths(
                    objective=(
                        agent_path_sandbox.agent_path(objective_document)
                        if objective_document is not None
                        else "OBJECTIVE.md"
                    ),
                    accuracy_command=_prefix_command(
                        evaluator_prefix,
                        evaluation.accuracy_command,
                    ),
                    benchmark_command=_prefix_command(
                        evaluator_prefix,
                        evaluation.benchmark_command,
                    ),
                    profiler_support=(
                        request.profiler_support_name if request.profiler_support_path else None
                    ),
                ),
                prompt_notes=presentation.prompt_notes,
                isolated=True,
                cli_sandboxed=True,
                host_device_reselect=False,
                env_kind="modal",
                profile_execution="remote",
                supports_parallel_candidate_evaluation=True,
                deployment_release_env_var="VIBESYS_RELEASE_MODAL_DEPLOYMENT",
                framework_setup_timeout_seconds=setup_timeout_seconds,
            ),
        )

    def _ensure_model_volume(self, request: RunEnvironmentRequest) -> None:
        if self.model_volume or request.ref_dir is None:
            return
        meta_path = request.ref_dir / "meta.json"
        if not meta_path.exists():
            return
        ensure_model_volume = import_module("vs_sandbox.api").ensure_model_volume

        meta = json.loads(meta_path.read_text())
        model_id = meta.get("model_id")
        if not model_id:
            message = (
                f"meta.json at {meta_path} missing required 'model_id' field "
                "(needed for Modal auto-upload)"
            )
            raise ValueError(message)
        hf_available = bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
        local_model = request.ref_dir / "model"
        local_path = None
        if not hf_available and local_model.exists() and local_model.resolve().is_dir():
            local_path = str(local_model.resolve())
        self.model_volume = ensure_model_volume(
            model_id,
            revision=meta.get("revision"),
            local_path=local_path,
            log=request.log or print,
        )

    def _ensure_draft_volume(
        self,
        request: RunEnvironmentRequest,
    ) -> str | None:
        """Auto-provision a Modal Volume for an auxiliary draft model.

        EAGLE3-style speculative decoding wants a draft model alongside the
        target weights.  When ``draft_meta.json`` sits next to ``meta.json``,
        upload it to its own Modal Volume and return the name so the sandbox
        can mount it read-only at ``/draft_model``.
        """
        if request.ref_dir is None:
            return None
        draft_meta_path = request.ref_dir / "draft_meta.json"
        if not draft_meta_path.exists():
            return None
        ensure_model_volume = import_module("vs_sandbox.api").ensure_model_volume

        draft_meta = json.loads(draft_meta_path.read_text())
        draft_model_id = draft_meta.get("model_id")
        if not draft_model_id:
            message = f"draft_meta.json at {draft_meta_path} missing required 'model_id' field"
            raise ValueError(message)
        hf_available = bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
        local_draft = request.ref_dir / "draft_model"
        local_path = None
        if not hf_available and local_draft.exists() and local_draft.resolve().is_dir():
            local_path = str(local_draft.resolve())
        return ensure_model_volume(
            draft_model_id,
            revision=draft_meta.get("revision"),
            local_path=local_path,
            log=request.log or print,
        )


def run_environment_record(spec: RunEnvironmentSpec) -> RunEnvironmentRecord:
    """Project a CLI-built spec onto the record persisted with the run.

    Only operator-selected options are recorded, under the spec's own option
    names. The candidate's Modal entrypoint is deliberately excluded: it is
    declared by the input bundle and re-derived on every launch, so recording
    it would make a legitimate task edit look like a resume mismatch.
    """
    return RunEnvironmentRecord(
        name=_recorded_environment_name(spec.name),
        image=_recorded_option(spec, "image"),
        gpu=_recorded_option(spec, "gpu"),
        model_volume=_recorded_option(spec, "model_volume"),
        app=_recorded_option(spec, "app"),
        config_path=_recorded_option(spec, "config_path"),
        resources=spec.resources,
    )


def _recorded_environment_name(name: str) -> _RunEnvironmentName:
    """Validate a spec name against the environments the run record can hold."""
    for recorded in _RECORDED_ENVIRONMENT_NAMES:
        if name == recorded:
            return recorded
    message = f"unknown run environment: {name!r}"
    raise ValueError(message)


def _recorded_option(spec: RunEnvironmentSpec, key: str) -> str | None:
    value = spec.options.get(key)
    return str(value) if value else None


def build_run_environment(spec: RunEnvironmentSpec) -> RunEnvironment:
    """Construct the implementation selected by a run environment spec."""
    if spec.name == "local":
        return LocalEnvironment()
    if spec.name == "docker":
        return DockerEnvironment.from_options(spec.options)
    if spec.name == "modal":
        return ModalEnvironment.from_options(spec.options)
    if spec.name == "skypilot":
        return SkyPilotEnvironment.from_options(spec.options, spec.resources)
    if spec.name == "slurm":
        return SlurmEnvironment.from_options(spec.options)
    message = f"unknown run environment: {spec.name!r}"
    raise ValueError(message)


def validate_run_environment_profile(
    environment: RunEnvironment, profile_command: tuple[str, ...] | None
) -> None:
    """Validate an enabled profiler's workload without provisioning resources.

    Configured Slurm services require a trusted profile command. Other
    environments and Slurm jobs without a service allow an absent command.
    Invalid operator policy or workload requirements raise ``ValueError``.
    """
    if isinstance(environment, SlurmEnvironment):
        configured_capture_lifecycle(
            load_slurm_config(environment.config_path),
            load_slurm_policy(environment.config_path),
            profile_command,
        )


def make_run_environment_spec(  # noqa: PLR0913  # lint-waiver: LW-009086 [PLR0913]; the compatibility builder accepts each independent CLI environment option.
    *,
    use_docker: bool = False,
    docker_image: str | None = None,
    use_modal: bool = False,
    modal_gpu: str = "H100!",
    modal_model_volume: str | None = None,
    modal_app: str = "vibesys",
    modal_entrypoint: str | None = None,
    use_skypilot: bool = False,
    cluster_profile: str | None = None,
    cluster_profiles_file: Path | None = None,
    skypilot_executable: str = "sky",
    resources: RunResourceRequest | None = None,
) -> RunEnvironmentSpec:
    """Build a spec from the current CLI compatibility flags.

    Modal mode (April 2026 refactor) runs the agent in a *local Docker
    container* and dispatches GPU work via the candidate's ``modal run`` entrypoint,
    so the legacy long-lived-Modal-sandbox knobs (timeout / idle_timeout)
    no longer apply here — they live on the implementer's per-function
    ``@app.function(timeout=...)`` / ``@app.cls(container_idle_timeout=...)``
    decorators instead.
    """
    if sum((use_docker, use_modal, use_skypilot)) > 1:
        message = "--docker, --modal, and --skypilot are mutually exclusive"
        raise ValueError(message)
    if use_skypilot:
        if not cluster_profile:
            message = "--skypilot requires --cluster-profile"
            raise ValueError(message)
        if resources is None:
            message = "--skypilot requires input [resources]"
            raise ValueError(message)
        return RunEnvironmentSpec(
            name="skypilot",
            options={
                "image": docker_image,
                "profile": cluster_profile,
                "profiles_file": cluster_profiles_file,
                "executable": skypilot_executable,
            },
            resources=resources,
        )
    if use_modal:
        options: dict[str, object] = {
            "image": docker_image,
            "gpu": modal_gpu,
            "model_volume": modal_model_volume,
            "app": modal_app,
        }
        if modal_entrypoint is not None:
            options["entrypoint"] = modal_entrypoint
        return RunEnvironmentSpec(
            name="modal",
            options=options,
            resources=resources,
        )
    if use_docker:
        return RunEnvironmentSpec(
            name="docker", options={"image": docker_image}, resources=resources
        )
    return RunEnvironmentSpec(resources=resources)


def _modal_app_name(run_id: str, fallback: str) -> str:
    """Derive a Modal app name unique to this run.

    Two concurrent runs must not share a ``modal.App(name=...)``. The persisted
    run ID is location-independent and stable when a project is moved or
    cloned, so it is the only input to the namespace.
    """
    candidate = run_id or fallback or "vibesys"
    sanitized = "".join(c if c.isalnum() or c == "-" else "-" for c in candidate.lower())
    sanitized = "-".join(part for part in sanitized.split("-") if part)
    name = f"vibesys-{sanitized}" if sanitized else "vibesys"
    return name[:63].rstrip("-") or "vibesys"


def _materialize_effective_objective(request: RunEnvironmentRequest) -> Path | None:
    """Select and materialize the effective objective for this run.

    Operator constraints are composed at the CLI boundary. VibeSys decides
    whether an objective exists and which authored document it represents;
    runtime infrastructure validates or writes the selected document.
    """
    if request.objective is None:
        return None
    return materialize_objective_document(
        request.objective,
        workspace=request.git_history_root or request.workspace,
        authored_document=request.objective_document,
        destination=request.log_dir / "effective-objective.md",
    )


def _isolated_paths(
    request: RunEnvironmentRequest,
    sandbox: _AgentPathSandbox,
    *,
    evaluator_tools_root: Path | None = None,
) -> AgentPaths:
    """Build agent-facing paths for an already-started container sandbox.

    Every path the agent is told about is asked of *sandbox* rather than
    hardcoded: :meth:`~vs_sandbox.docker_sandbox.DockerSandbox.agent_path`
    is the one table mapping a host path to where the container sees it,
    built from the same resource list :func:`_container_mount_plan` declared.
    """
    objective_document = _materialize_effective_objective(request)
    requirements = request.evaluator_requirements
    evaluation = _prepare_evaluation_plan(
        request,
        requirements,
        TrustedEvaluationCommandPaths(
            source_project_root=request.workspace,
            runtime_project_root="/workspace",
            python_executable="python3",
            runtime_package_root=(
                sandbox.agent_path(requirements.package_root)
                if requirements.package_root is not None
                else None
            ),
            runtime_tools_root=evaluator_tools_root,
        ),
    )
    return AgentPaths(
        objective=(
            sandbox.agent_path(objective_document)
            if objective_document is not None
            else "OBJECTIVE.md"
        ),
        accuracy_command=evaluation.accuracy_command,
        benchmark_command=evaluation.benchmark_command,
        profiler_support=(request.profiler_support_name if request.profiler_support_path else None),
    )


def _prefix_command(prefix: str, command: str | None) -> str | None:
    if not command:
        return None
    return f"{prefix} {command}"


def _command_argv(command: str | None) -> tuple[str, ...] | None:
    """Parse one framework-rendered command before persisting a trusted plan."""
    return tuple(shlex.split(command)) if command is not None else None


def _noop_log(message: str) -> None:
    del message


def _required_runtime_document(
    presentation: RunEnvironmentPresentation, environment_name: str
) -> str:
    document = presentation.runtime_document
    if document is None:
        message = f"{environment_name} requires an explicit runtime presentation document"
        raise ValueError(message)
    return document


def _prepare_evaluation_plan(
    request: RunEnvironmentRequest,
    requirements: TrustedEvaluatorRequirements,
    paths: TrustedEvaluationCommandPaths,
) -> TrustedEvaluationPlan:
    """Bind authored commands to one environment through the runtime contract."""
    return prepare_trusted_evaluation_plan(
        TrustedEvaluationPlan(
            accuracy_command=request.accuracy_command,
            benchmark_command=request.benchmark_command,
            profile_command=request.profile_command,
        ),
        requirements,
        paths,
    )


def _container_mount_plan(
    request: RunEnvironmentRequest,
    *,
    include_cli_provider_mounts: bool = True,
) -> tuple[list[HostResource], list[tuple[str, str]]]:
    """Build the host resources + setup symlinks for a sandbox.

    Returns the resource list the sandbox enforces and exposes through
    :meth:`~vs_sandbox.docker_sandbox.DockerSandbox.agent_path` (workspace
    read-write mapping aside, which the sandbox itself always provides),
    and the setup symlinks a lifecycle hook must create once the container is
    up.

    ``include_cli_provider_mounts`` controls whether CLI auth state and the
    full project tree are added under ``/opt/vibesys-auth`` and
    ``/opt/vibesys``. Defaults to True; both supported environments (local
    Docker and the Modal-via-Docker mode) bind-mount these read-only and copy
    auth state into the container's writable layer during setup.
    """
    bind_mounts: list[tuple[str, str, bool]] = []
    symlinks: list[tuple[str, str]] = []
    ref_dir = request.ref_dir

    skip_environment_mount_symlinks = {
        mount.host_path.name for mount in request.environment_bind_mounts
    }

    if ref_dir is not None:
        reference_container_path = _reference_container_path(request)
        collect_symlink_mounts(
            ref_dir,
            reference_container_path,
            bind_mounts=bind_mounts,
            symlinks=symlinks,
            skip=skip_environment_mount_symlinks,
        )
        if ref_dir.parent != ref_dir:
            collect_symlink_mounts(
                ref_dir.parent,
                str(Path(reference_container_path).parent),
                bind_mounts=bind_mounts,
                symlinks=symlinks,
                skip=skip_environment_mount_symlinks,
            )

    objective_document = _materialize_effective_objective(request)
    if objective_document is not None:
        bind_mounts.append((str(objective_document), _RUNTIME_OBJECTIVE_CONTAINER_PATH, True))
    if request.git_history_root is not None:
        bind_mounts.append((str(request.git_history_root), "/opt/vibesys-history", True))
    for mount in request.environment_bind_mounts:
        resolved = mount.host_path.resolve()
        host_path = find_mount_root(resolved)
        if host_path == resolved:
            bind_mounts.append((str(host_path), mount.container_path, mount.read_only))
        else:
            rel = resolved.relative_to(host_path)
            mount_name = mount.container_path.strip("/").replace("/", "_") or "environment_mount"
            ancestor_mount = f"/workspace/_mounts/{mount_name}"
            bind_mounts.append((str(host_path), ancestor_mount, mount.read_only))
            symlinks.append((mount.container_path, f"{ancestor_mount}/{rel}"))

    if request.profiler_support_path and request.profiler_support_name:
        bind_mounts.append(
            (
                request.profiler_support_path,
                f"/workspace/{request.profiler_support_name}",
                True,
            )
        )
        bind_mounts.extend(
            (extra_path, f"/workspace/{extra_name}", True)
            for extra_path, extra_name in request.profiler_support_extra
        )

    if request.evaluator_requirements.package_root is not None:
        bind_mounts.append(
            (
                str(request.evaluator_requirements.package_root),
                "/opt/vibesys-evaluator-package",
                True,
            )
        )

    resources = [
        host_resource_for_mount(host, container, read_only=read_only)
        for host, container, read_only in bind_mounts
    ]
    resources.extend(
        docker_project_path_resources(
            request.project_path_policy,
            request.workspace,
            mask_root=request.log_dir / "sandbox-hidden",
        )
    )

    if (
        include_cli_provider_mounts
        and (request.agent_backend or AgentBackend.CLI) == AgentBackend.CLI
        and request.cli_provider
    ):
        resources.extend(
            host_resource_for_mount(host, container, read_only=read_only)
            for host, container, read_only in auth_bind_mounts(request.cli_provider)
        )
        resources.append(
            host_resource_for_mount(str(request.framework_root), "/opt/vibesys", read_only=True)
        )

    return resources, symlinks


def _reference_container_path(request: RunEnvironmentRequest) -> str:
    """Return the reference path inside an isolated workspace.

    Normal project references retain their repository-relative location. An
    external reference directory uses the legacy ``/workspace/reference``
    location while its external symlink targets are mounted separately.
    """
    if request.ref_dir is None:
        return "/workspace/reference"
    try:
        relative = request.ref_dir.resolve().relative_to(request.workspace.resolve())
    except ValueError:
        return "/workspace/reference"
    return f"/workspace/{relative.as_posix()}"


def _cli_container_env(request: RunEnvironmentRequest) -> tuple[str, dict[str, str]] | None:
    """Return ``(provider, container env)`` when a CLI provider needs a container.

    Every containerized environment (Docker, Modal, SkyPilot) now starts from
    a prebuilt agent image and installs nothing at container start, so this
    is the whole of what a CLI provider needs from the run request: the
    auth-presence check and the auth env passthrough. What used to be the
    shell-command half of this (:func:`vs_agent.cli_docker
    .docker_init_commands`, run through ``extra_init_commands``) is gone; see
    :func:`_cli_provider_env_and_auth_files` for the staged-file counterpart
    Modal and SkyPilot pass through ``auth_files`` instead, matching the
    plain Docker path.

    Returns ``None`` when the run is not a containerized CLI agent (a
    different agent backend, or no CLI provider selected).

    Raises:
        ValueError: if *request.cli_provider* has neither a staged auth file
            nor a usable auth environment variable on this host.
    """
    effective_agent = request.agent_backend or AgentBackend.CLI
    if effective_agent != "cli" or not request.cli_provider:
        return None
    provider = request.cli_provider
    auth_env = auth_env_passthrough(provider)
    staged_auth = [spec for spec in auth_paths(provider) if spec.host_path.exists()]
    if not staged_auth and not auth_env:
        checked_files = (
            ", ".join(str(spec.host_path) for spec in auth_paths(provider)) or "<none registered>"
        )
        checked_env = ", ".join(auth_env_vars(provider)) or "<none registered>"
        message = (
            f"no {provider!r} CLI authentication is available for the container: "
            f"none of the host files exist ({checked_files}) and none of the "
            f"environment variables are set ({checked_env}). Authenticate the "
            f"{provider} CLI on this host, or export one of those variables, "
            "before running in an isolated environment."
        )
        raise ValueError(message)
    env = dict(DOCKER_PROVIDER_ENV.get(provider, {}))
    # Container processes inherit only what ``docker run -e`` sets; the editor
    # container has no other view of the host environment.
    env.update(auth_env)
    return provider, env


def _cli_provider_env_and_auth_files(
    request: RunEnvironmentRequest,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Return the container env and staged auth copies for a CLI provider, if any.

    The shared counterpart to :meth:`DockerEnvironment.open`'s own inline
    version of this: every environment that starts a container from the
    prebuilt agent image copies auth the same way (via
    :func:`vs_agent.cli_docker.auth_copy_paths`, handed to the sandbox
    as ``auth_files`` so it copies them in at start), rather than running
    shell commands built from a provider's install recipe.
    """
    resolved_cli = _cli_container_env(request)
    if resolved_cli is None:
        return {}, []
    provider, cli_provider_env = resolved_cli
    return cli_provider_env, auth_copy_paths(provider)


def _ensure_pushed_for_remote_backend(
    image_id: str,
    *,
    ensure_pushed: Callable[[str], str],
    backend_label: str,
) -> str:
    """Push and verify *image_id*, naming it and *backend_label* on failure.

    ``ensure_pushed`` (:func:`vs_agent.api.images.ensure_pushed`) already
    raises :class:`~vs_agent.api.images.ImagePushError` naming the image
    and what went wrong; this only adds which run environment could not
    start because of it, so the operator does not have to guess whether a
    Modal or a SkyPilot launch is the one that failed to reach the registry.
    """
    image_push_error = import_module("vs_agent.api.images").ImagePushError

    try:
        return ensure_pushed(image_id)
    except image_push_error as exc:
        raise image_push_error.run_environment_push_failed(image_id, backend_label, exc) from exc


def _docker_backend_image(request: RunEnvironmentRequest) -> str:
    """Return the compute backend's base image, the ``agent_image`` build input.

    Used both to build the agent image every Docker run starts from and, when
    the run also needs cargo-git evaluator tools, to key their host-side
    build cache.
    """
    image = getattr(request.backend, "image", None)
    if not isinstance(image, str) or not image:
        raise EvaluatorToolError.docker_image_unconfigured()
    return image
