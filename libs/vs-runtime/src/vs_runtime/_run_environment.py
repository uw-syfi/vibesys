"""Run environment assembly over the existing sandbox implementations.

Layering:

    product composition -> RunEnvironment -> ComputeBackendImpl.make_sandbox -> CommandRunner

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

import os
import shlex
import shutil
import sys

# lint-waiver: LW-007062 [TC003]; Pydantic resolves this dataclass field annotation at runtime
from collections.abc import Mapping  # noqa: TC003
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from vs_agent.api import (
    auth_copy_paths,
    reap_orphaned_agents,
)

# lint-waiver: LW-014514 [TC001]; Pydantic resolves this dataclass field annotation at runtime
from vs_project.api import RunResourceRequest  # noqa: TC001
from vs_runtime import _boot_trace as boot_trace
from vs_runtime._agent_tool_caches import agent_tool_cache_env
from vs_runtime._cli_container_env import cli_container_env, cli_provider_env_and_auth_files
from vs_runtime._command_environment_session import command_environment_session
from vs_runtime._container_mount_plan import (
    CANDIDATE_GIT_LAYOUT,
    container_mount_plan,
    git_metadata_mounts,
    git_metadata_writable,
    materialize_effective_objective,
    reference_container_path,
    sandbox_workspace_root,
)
from vs_runtime._container_runtime_policy import (
    DOCKER_IN_DOCKER_NOTICE,
    RefusesDockerInDocker,
    attaches_accelerator,
    reject_docker_in_docker,
)
from vs_runtime._docker_agent_work_reaper import DockerAgentWorkReaper
from vs_runtime._docker_evaluator_tools import (
    DockerToolBuilder,
    prepare_docker_evaluator_resources,
)
from vs_runtime._environment_startup import (
    EnvironmentStartup,
    EnvironmentStartupSink,
    EnvironmentStartupStage,
    StartupOutputOpener,
    ToolBuildOutputOpener,
    WorkspaceRootProbeSink,
    startup_stage,
)
from vs_runtime._image_features import image_features
from vs_runtime._marked_stage_work_reaper import MarkedStageWorkReaper, marked_with_the_tag
from vs_runtime._model_artifacts import read_model_metadata
from vs_runtime._objective_document import (
    outside_objective,
)
from vs_runtime._project_run import CaptureRecoverySource, NoCaptureRecovery
from vs_runtime._run_environment_facts import (
    ClusterEvaluationFacts,
    DockerEnvironmentFacts,
    HostEnvironmentFacts,
    ModalEnvironmentFacts,
    RunEnvironmentPresentationFacts,
)
from vs_runtime._step_clear import AGENT_STEP_KINDS, StepClearUnsupportedError
from vs_runtime._tool_location import ToolLocation, ToolSite, assumed_tool
from vs_runtime._trusted_evaluation import TrustedEvaluationPlan
from vs_runtime._trusted_evaluation_preparation import (
    REMOTE_EVALUATOR_GO_MODULES_ROOT,
    REMOTE_EVALUATOR_TOOLS_ROOT,
    SANDBOX_EVALUATOR_GO_MODULES_ROOT,
    SANDBOX_EVALUATOR_TOOLS_ROOT,
    EvaluatorGoModuleLifecycleHooks,
    TrustedEvaluationCommandPaths,
    TrustedEvaluatorRequirements,
    evaluator_agent_toolchains,
    prepare_trusted_evaluation_plan,
    remote_evaluator_setup_command,
    required_evaluator_go_module_cache_root,
    required_evaluator_tools_root,
)
from vs_runtime._workspace_files import write_for_agent
from vs_runtime._writer_reconciliation import (
    UnsupportedWriterReconciler,
    docker_writer_reconciler,
)
from vs_sandbox.api import (
    CONTAINER_RUNTIME_TOOLCHAIN,
    STEP_TAG_ENV,
    DeviceLease,
    EnvironmentBindMount,
    HostResource,
    ProjectPathPolicy,
    SandboxKind,
    SandboxLifecycleHooks,
    SandboxSession,
    SubprocessDockerCli,
    build_host_sandbox,
    deduplicate_host_resources,
    evaluator_helpers,
    host_resource_for_mount,
    require_sysbox_runtime,
    step_tag_env,
)
from vs_sandbox.api.docker_workspace import (
    DockerWorkspaceRepairError,
    remove_docker_workspace_child,
    repair_docker_workspace,
)
from vs_sandbox.api.evaluator_helpers import encode_setup_command
from vs_sandbox.api.evaluator_tools import (
    EvaluatorToolError,
    EvaluatorToolLifecycleHooks,
)
from vs_sandbox.api.symlink_mounts import (
    symlink_lifecycle_hooks,
)
from vs_sim.api import (
    DISCARD,
    CloseOnce,
    OsProcessTable,
    ProcessTableUnavailableError,
    ReleaseStack,
)

_RUNTIME_DOCUMENT_NAME = "runtime-environment.md"
if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from vs_agent.api.images import BuildProgressSink, DockerBuildRunner, ImageBuildProgress
    from vs_evaluation.api import ProfileField
    from vs_project.api import StateNamespace
    from vs_proto.api import replay as writer_pb
    from vs_runtime._environment_system import EnvironmentSystem
    from vs_runtime._local_job_step_runner import StageWorkReaper
    from vs_sandbox.api import (
        CaptureCommandRunner,
        CommandRunner,
        ComputeBackendImpl,
        DockerCli,
        FeatureRef,
        WorkspaceSandbox,
    )
    from vs_sim.api import BlockingRunner, Clock, OutputSink, ProcessTable


@dataclass(frozen=True)
class RunEnvironmentSpec:
    """Run-environment selection at the CLI/config boundary.

    Environment-specific knobs stay inside ``options`` and are parsed only by the
    concrete environment selected by ``name``.
    """

    name: str = "docker"
    options: Mapping[str, object] = field(default_factory=dict)
    resources: RunResourceRequest | None = None
    #: The task Dockerfile the ``image`` option was built from, or ``None``
    #: when the image is the operator's or the backend's. Not recorded: the
    #: run record holds the task image's reference.
    task_dockerfile: Path | None = None
    #: The immutable ID the task image resolved to at launch. Not recorded;
    #: when set, the environment runs it in place of the ``image`` option, so
    #: a later launch pruning the task image's tag cannot change this run.
    image_id: str | None = None


@dataclass(frozen=True)
class AgentPaths:
    """Command and helper paths as agents should use them in the active environment."""

    objective: str = "OBJECTIVE.md"
    accuracy_command: str | None = None
    benchmark_command: str | None = None
    profiler_support: str | None = None


class ParallelCandidateObstacle(StrEnum):
    """The closed set of reasons a session cannot open one session per candidate."""

    NO_OWN_CONTAINER = "no_own_container"
    BORROWED_SESSION = "borrowed_session"


class RunEnvironmentSessionRole(StrEnum):
    """The lifecycle role of one environment session within a run.

    A run opens exactly one root session. Candidate and auxiliary workspaces
    are children: they reuse run-scoped preparation and must not repeat it.
    """

    ROOT = "root"
    CHILD = "child"


@dataclass(frozen=True)
class RunEnvironmentView:
    """Run-environment-neutral facts consumed by loops and agent construction."""

    paths: AgentPaths
    #: Whether the agent can write the Git metadata of the workspace this session mounts,
    #: as that session's mount plan bound it. Required: a missing fact must not read as true.
    workspace_git_writable: bool
    prompt_notes: str = ""
    isolated: bool = False
    cli_sandboxed: bool = False
    # Agent clients borrow the run-owned sandbox and bridge.
    share_agent_session: bool = False
    host_device_reselect: bool = True
    # Coarse environment label for diagnostics and adapter selection:
    # ``"host"`` | ``"docker"`` | ``"modal"`` | ``"skypilot"`` | ``"slurm"``.
    env_kind: str = "host"
    # Where a profiler must execute to observe the production hot path. Prompt
    # templates branch on this capability rather than on a concrete provider.
    profile_execution: Literal["local", "remote"] = "local"
    # Whether the container the agent and its tool servers run in has a GPU driver. A
    # run environment whose GPU work happens elsewhere (Slurm jobs) sets it False, so
    # tools that need the driver in the editor are withheld rather than offered to fail.
    editor_has_gpu: bool = True
    # Optional environment variable understood by the environment-owned
    # evaluator wrapper when the final trusted command should release its
    # deployment lease.
    deployment_release_env_var: str | None = None
    # Extra wall-clock budget for environment-owned setup that wraps a trusted
    # command, such as deploying a fresh service and waiting for readiness.
    framework_setup_timeout_seconds: int = 0
    profiler_mcp_env: tuple[tuple[str, str], ...] = ()
    profiler_mcp_resources: tuple[HostResource, ...] = ()
    agent_host_resources: tuple[HostResource, ...] = ()
    # Set when trusted evaluation runs as Slurm jobs; ``None`` otherwise.
    cluster_evaluation: ClusterEvaluationFacts | None = None

    @property
    def captures_profile(self) -> bool:
        """Whether the run can capture a profile: the one fact every profile offer reads."""
        return self.cluster_evaluation is not None and self.cluster_evaluation.captures_profile

    @property
    def dedicated_measurement_resources(self) -> bool:
        """Whether the trusted executor runs each measurement on hosts it holds alone.

        Only a cluster that submits every evaluation job exclusively declares it; an executor
        that evaluates beside the agent (the local one) never does.
        """
        return (
            self.cluster_evaluation is not None
            and self.cluster_evaluation.dedicated_measurement_resources
        )

    @property
    def supported_profile_fields(self) -> tuple[ProfileField, ...]:
        """The measurement fields this run's capture can supply; empty when it captures none."""
        if self.cluster_evaluation is None:
            return ()
        return self.cluster_evaluation.supported_profile_fields

    @property
    def parallel_candidate_obstacle(self) -> ParallelCandidateObstacle | None:
        """Why this session cannot open an isolated session per candidate, or ``None``.

        A candidate session is a second session of the same environment, so it
        has its own sandbox exactly when the agent's CLI runs in a container
        this session starts (``cli_sandboxed``) and agent clients do not borrow
        the run-owned sandbox and bridge (``share_agent_session``). Nothing
        declares the answer: it follows from those two facts.
        """
        if not self.cli_sandboxed:
            return ParallelCandidateObstacle.NO_OWN_CONTAINER
        if self.share_agent_session:
            return ParallelCandidateObstacle.BORROWED_SESSION
        return None

    @property
    def supports_parallel_candidate_evaluation(self) -> bool:
        """Whether each candidate can get its own worktree and container session."""
        return self.parallel_candidate_obstacle is None

    @property
    def agent_git_writable(self) -> bool:
        """Whether agents can commit where they write candidates: the fact prompts state.

        A session that opens a session per candidate has its agents write in those
        candidates, linked worktrees bound by the same rule as this session's mounts.
        Otherwise they write in this session's own workspace. Prompts are rendered for
        the run, not for one session, so they read this and never the root's own fact.
        """
        if self.supports_parallel_candidate_evaluation:
            return git_metadata_writable(CANDIDATE_GIT_LAYOUT)
        return self.workspace_git_writable


@dataclass(frozen=True)
class RunEnvironmentPresentation:
    """Product-authored text consumed by environment infrastructure."""

    prompt_notes: str
    runtime_document: str | None = None


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
    clock: Clock
    """Measures the agent image build budget."""
    system: EnvironmentSystem
    """The host's threads, sockets and processes the environment's brokers and tools run on."""
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
    # run (the profiler) may stage any of them to a remote executor. Only the
    # candidate session's request clears them, so its brokers never reach a
    # sibling candidate. A root may legitimately own none; ``session_role`` is
    # the lifecycle discriminator.
    run_owned_roots: tuple[Path, ...] = ()
    environment_bind_mounts: tuple[EnvironmentBindMount, ...] = ()
    seeded_workspace_paths: tuple[str, ...] = ()
    log: Callable[[str], None] | None = None
    project_path_policy: ProjectPathPolicy = field(default_factory=ProjectPathPolicy)
    state_namespace: StateNamespace | None = None
    session_role: RunEnvironmentSessionRole = RunEnvironmentSessionRole.ROOT
    """Whether this request opens the run's one root session or a child workspace."""
    #: Root of the run's dedicated agent CLI homes; ``None`` when the run has
    #: no machine-local state (agents then keep the provider's default home).
    agent_homes_dir: Path | None = None
    #: The task declared ``[environment] docker_in_docker = true``: its
    #: candidate is a container topology, so only the Docker environment can
    #: run it (a sandbox container with a Docker daemon of its own under
    #: Sysbox). Every other environment rejects the request.
    docker_in_docker: bool = False
    #: Where a container environment reports its startup stages and image
    #: build progress while it opens; ``None`` reports nothing.
    startup: EnvironmentStartupSink | None = None
    #: Opens the stream each startup command (the agent image build, its
    #: registry push, the container start) writes its raw output to, one per
    #: command, closed when the command ends; ``None`` discards the output.
    startup_output: StartupOutputOpener | None = None
    #: Opens the stream each evaluator tool build (a cache miss of the task's
    #: declared tools) writes its raw output to, closed when the build ends;
    #: ``None`` discards the output.
    tool_build_output: ToolBuildOutputOpener | None = None
    #: The step this session belongs to, set for the throwaway candidate of a local evaluation:
    #: every command it runs carries the step's mark (`vs_sandbox.api.step_tag_env`), so the
    #: work a dead process left is found by the tag alone.
    step_tag: str | None = None
    #: Where a local Slurm root's probe, which runs beside the run, reports that its
    #: job waits in the queue and how it ended; ``None`` reports nothing.
    workspace_root_probe: WorkspaceRootProbeSink | None = None


class _AgentPathSandbox(Protocol):
    """The one lookup ``AgentPaths`` construction needs from a started sandbox.

    Narrower than ``CommandRunner``: a host-only sandbox never
    reaches this contract (``HostEnvironment`` builds host paths directly),
    while every ``SandboxKind.DOCKER`` build
    (:class:`~vs_sandbox.docker_sandbox.DockerSandbox`) satisfies it.
    """

    def agent_path(self, host_path: Path | str) -> str: ...


class RunEnvironmentSession(Protocol):
    """Context-managed sandbox session owned by a run environment."""

    sandbox: CommandRunner
    view: RunEnvironmentView

    @property
    def capture_commands(self) -> CaptureCommandRunner:
        """Runner for profile-attribution capture; usually the editor runner."""
        ...

    def __enter__(self) -> RunEnvironmentSession:
        """Enter the session and return its context-managed handle."""
        ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Release session resources when leaving the context."""
        ...

    def close(self) -> None:
        """Stop resources owned by this run session."""
        ...


class RunEnvironmentResources:
    """Concrete owner of one environment session and its compute-device lease."""

    def __init__(
        self,
        request: RunEnvironmentRequest,
        session: RunEnvironmentSession,
        device: DeviceLease,
        ownership: ReleaseStack,
        open_session: Callable[[RunEnvironmentRequest], RunEnvironmentSession],
    ) -> None:
        self.request = request
        self.session = session
        self.device = device
        self._ownership = ownership
        self._open_session = open_session
        self._close = CloseOnce()

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
        """Release owned resources in reverse construction order; a failed step runs again next call."""
        self._close.run(self._ownership.close)


def _close_failed_environment_resources(
    ownership: ReleaseStack, construction_error: BaseException
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
    *,
    clock: Clock,
) -> RunEnvironmentResources:
    """Open a root session and own its newly started device lease; ``clock`` counts epoch seconds."""
    ownership = ReleaseStack()
    try:
        with boot_trace.span("environment_open"):
            session = ownership.enter_context(open_session(request))
        with boot_trace.span("device_monitor_start"):
            device = DeviceLease(
                request.backend,
                log_dir=request.log_dir,
                clock=clock,
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
    ownership = ReleaseStack()
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


class RunEnvironment(CaptureRecoverySource, Protocol):
    """Environment policy for run execution and candidate evaluation."""

    # Read-only members: an environment may fix them or derive them from its
    # operator configuration.

    @property
    def isolated(self) -> bool:
        """Whether candidates run in isolated worktrees of their own."""
        ...

    @property
    def uses_docker(self) -> bool:
        """Whether a run here starts local Docker containers (the agent's, or an editor's)."""
        ...

    @property
    def materialize_local_model_weights(self) -> bool:
        """Whether external model-weight symlinks are copied into the workspace."""
        ...

    @property
    def default_profiler_id(self) -> str:
        """The profiler a run uses when none is requested."""
        ...

    @property
    def supported_profiler_ids(self) -> frozenset[str] | None:
        """The profilers a run may select, or ``None`` when any is allowed."""
        ...

    @property
    def backend_image(self) -> str | None:
        """The image the environment pins for the compute backend, if any."""
        ...

    @property
    def requires_local_profiler_preflight(self) -> bool:
        """Whether the profiler's tool must exist on this host."""
        ...

    def locate_tool(self, executable: str, site: ToolSite) -> ToolLocation:
        """Where this environment runs *executable* at *site*, or why it cannot."""
        ...

    def attaches_backend_accelerator(self, *, docker_in_docker: bool) -> bool:
        """Whether a run here gives its agent the compute backend's accelerator on this host.

        Such a run needs that accelerator usable here: it checks it before its image
        build. *docker_in_docker* is the task's ``[environment] docker_in_docker``.
        """
        ...

    def requires_sysbox_runtime(self, *, docker_in_docker: bool) -> bool:
        """Whether a run here needs the host's Docker daemon to register Sysbox."""
        ...

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

    def reconcile_writers(
        self,
        manifest: writer_pb.WriterManifest,
        *,
        system: EnvironmentSystem,
        log: Callable[[str], None],
    ) -> writer_pb.WriterReconciliation:
        """Return environment-issued death proof, or explicit Pending/Unknown/Unsupported."""
        ...

    def reap_orphans(
        self, run_id: str, *, system: EnvironmentSystem, log: Callable[[str], None]
    ) -> None:
        """End the agent processes and containers a dead host left for *run_id*.

        Called when a run resumes, before anything else touches its workspace.
        Raises if an orphan could not be ended: the run must not resume beside
        an agent that may still be writing.
        """
        ...

    def agent_work_reaper(
        self,
        run_id: str,
        *,
        system: EnvironmentSystem,
        blocking: BlockingRunner,
        local_namespace: Callable[[str], StateNamespace],
        log: Callable[[str], None],
    ) -> StageWorkReaper:
        """The reaper that ends the agent processes of one step of *run_id*, by the step's tag.

        ``reap_orphans`` ends every agent of the run; this ends one step's and leaves the
        rest. Raises ``StepClearUnsupportedError`` when the environment has no way to find
        what a step left, so the run refuses to start instead of waiting on a ``clear``
        that can never answer.
        """
        ...


class _NoopWorkspaceRecovery(NoCaptureRecovery):
    def reconcile_writers(
        self,
        manifest: writer_pb.WriterManifest,
        *,
        system: EnvironmentSystem,
        log: Callable[[str], None],
    ) -> writer_pb.WriterReconciliation:
        del system, log
        name = manifest.backing.environment if manifest.backing is not None else "unknown"
        return UnsupportedWriterReconciler(name).reconcile_writers(manifest)

    def reap_orphans(
        self, run_id: str, *, system: EnvironmentSystem, log: Callable[[str], None]
    ) -> None:
        del run_id, system, log

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


def _nothing(value: str) -> bool:
    del value
    return False


class HostEnvironment(RefusesDockerInDocker, _NoopWorkspaceRecovery):
    """Run the agent on the host, refusing to start unless it is confined.

    Selected for backends whose accelerator no container can reach (Metal on
    macOS).  Confinement is the host sandbox with enforcement required, so an
    unavailable sandbox raises ``SandboxUnavailableError`` and never degrades to
    an unconfined agent.  The agent driver requires enforcement again when it
    launches, because this environment is not containerized.
    """

    isolated: bool = False
    uses_docker: bool = False
    materialize_local_model_weights: bool = True
    default_profiler_id = "nsys"
    supported_profiler_ids: frozenset[str] | None = None
    backend_image: str | None = None
    requires_local_profiler_preflight = True

    def attaches_backend_accelerator(self, *, docker_in_docker: bool) -> bool:
        """No container: the host agent reaches the accelerator itself."""
        del docker_in_docker
        return False

    def __init__(
        self,
        build_sandbox: Callable[..., WorkspaceSandbox | None] = build_host_sandbox,
        process_table: ProcessTable | None = None,
    ) -> None:
        """Take the host-sandbox builder and the process table, seams for substituting the platform's."""
        self._build_sandbox = build_sandbox
        self._process_table = OsProcessTable() if process_table is None else process_table

    def agent_work_reaper(
        self,
        run_id: str,
        *,
        system: EnvironmentSystem,
        blocking: BlockingRunner,
        local_namespace: Callable[[str], StateNamespace],
        log: Callable[[str], None],
    ) -> StageWorkReaper:
        """Find one step's agent processes by the tag in their environment.

        The host has no container to remove, so the process table is all there is. A host that
        cannot list the environments of its processes cannot clear an agent, and refuses here.
        """
        del run_id, system, blocking, local_namespace, log
        try:
            self._process_table.marked(STEP_TAG_ENV, _nothing)
        except ProcessTableUnavailableError as error:
            reason = "the host cannot list the environments of its processes"
            raise StepClearUnsupportedError("host", AGENT_STEP_KINDS, reason) from error
        return MarkedStageWorkReaper(self._process_table, marked_with_the_tag)

    def locate_tool(self, executable: str, site: ToolSite) -> ToolLocation:
        """Find *executable* on the host's ``PATH``: the agent and its workload both run there."""
        del site
        if os.sep in executable:
            # A path names a file in the run's workspace, which this environment does not own.
            return ToolLocation.found(executable)
        found = shutil.which(executable)
        if found is None:
            return ToolLocation.missing(f"`{executable}` is not on the host's PATH")
        return ToolLocation.found(found)

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> HostEnvironment:
        """Build the environment; ``build_sandbox`` is the unrecorded injection seam."""
        build_sandbox = options.get("build_sandbox")
        if build_sandbox is None:
            return cls()
        return cls(cast("Callable[..., WorkspaceSandbox | None]", build_sandbox))

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Fail with ``SandboxUnavailableError`` when host confinement is unavailable."""
        reject_docker_in_docker(docker_in_docker=request.docker_in_docker, environment="host")
        self._build_sandbox(request.workspace, env={}, require_enforcement=True)
        return _PreparedRunEnvironment(HostEnvironmentFacts(), partial(self._open, request))

    def _open(
        self, request: RunEnvironmentRequest, presentation: RunEnvironmentPresentation
    ) -> RunEnvironmentSession:
        """Create a host sandbox and its agent path view."""
        del presentation
        materialize_effective_objective(request)
        document, grants = outside_objective(request.objective, request.log_dir)
        evaluation = _host_evaluation_plan(request)
        return command_environment_session(
            SandboxSession.borrowed(
                sandbox=_make_host_sandbox(request),
                view=RunEnvironmentView(
                    paths=AgentPaths(
                        objective=(str(document) if document is not None else "OBJECTIVE.md"),
                        accuracy_command=evaluation.accuracy_command,
                        benchmark_command=evaluation.benchmark_command,
                        profiler_support=request.profiler_support_path,
                    ),
                    # The host sandbox binds a linked worktree's metadata read-only too.
                    workspace_git_writable=git_metadata_mounts(request.workspace).agent_writable,
                    agent_host_resources=grants,
                ),
            )
        )


def _make_host_sandbox(request: RunEnvironmentRequest, *, ephemeral: bool = False) -> CommandRunner:
    """Create the host command runner, installing the task's evaluator tools on the host.

    Building the runner runs its lifecycle hooks, so an *ephemeral* runner
    that is never used still leaves the tools installed.
    """
    requirements = request.evaluator_requirements
    hooks: list[SandboxLifecycleHooks] = []
    if requirements.go_module is not None:
        required_evaluator_go_module_cache_root(requirements, request.workspace)
        hooks.append(EvaluatorGoModuleLifecycleHooks(requirements))
    if requirements.tools:
        hooks.append(
            EvaluatorToolLifecycleHooks(
                requirements.tools,
                required_evaluator_tools_root(requirements, request.workspace),
                search_path=request.system.host_tool_search_path(),
            )
        )
    return request.backend.make_sandbox(
        SandboxKind.LOCAL,
        host_workspace=str(request.workspace),
        log_path=None,
        bind_mounts=[],
        extra_env=_step_mark(request),
        lifecycle_hooks=hooks,
        ephemeral=ephemeral,
    )


def _host_evaluation_plan(request: RunEnvironmentRequest) -> TrustedEvaluationPlan:
    """Bind the task's authored commands to the host that runs them.

    The commands name host paths and the running interpreter: they execute on
    the host (or on a cluster node that shares its filesystem), not in the
    agent's container.
    """
    requirements = request.evaluator_requirements
    return _prepare_evaluation_plan(
        request,
        requirements,
        TrustedEvaluationCommandPaths(
            source_project_root=request.workspace,
            runtime_project_root=str(request.workspace),
            python_executable=sys.executable,
            runtime_package_root=(
                str(requirements.package_root) if requirements.package_root is not None else None
            ),
            runtime_go_module_cache_root=(
                str(required_evaluator_go_module_cache_root(requirements, request.workspace))
                if requirements.go_module is not None
                else None
            ),
        ),
    )


def _report_startup(
    request: RunEnvironmentRequest,
    stage: EnvironmentStartupStage,
    progress: ImageBuildProgress | None = None,
) -> None:
    if request.startup is not None:
        request.startup(EnvironmentStartup(stage=stage, progress=progress))


def _image_build_progress(request: RunEnvironmentRequest) -> BuildProgressSink | None:
    """Where the image build's step observations go: the request's startup sink, when it has one."""
    if request.startup is None:
        return None
    return partial(_report_startup, request, EnvironmentStartupStage.BUILDING_IMAGE)


_CONTAINER_START_COMMAND = "docker run"
_AGENT_IMAGE_PUSH_COMMAND = "docker push agent image"


def _agent_image_build_command(base_image: str) -> str:
    """Describe the agent image build on *base_image* for its output stream."""
    return f"docker build agent image on {base_image}"


def _startup_output(
    request: RunEnvironmentRequest, stage: EnvironmentStartupStage, command: str
) -> AbstractContextManager[OutputSink]:
    """Open the stream *command*'s output is written to, or discard it when none is asked for."""
    if request.startup_output is None:
        return nullcontext(DISCARD)
    return request.startup_output(stage, command)


def _building_image(request: RunEnvironmentRequest) -> AbstractContextManager[None]:
    """Report the image build as a stage: running for the block, finished after it."""
    return startup_stage(
        request.startup, EnvironmentStartupStage.BUILDING_IMAGE, clock=request.clock
    )


class _Identified(Protocol):
    @property
    def id(self) -> str: ...


@contextmanager
def _starting_container(
    request: RunEnvironmentRequest, sandbox: _Identified
) -> Iterator[OutputSink]:
    """Report the container start as a stage, and open the stream its ``docker run`` writes to.

    Once the container is up, one narration line names it (the sandbox's id) and
    the session's role, so two container starts of one run never read as one
    event repeated: every container of a run starts exactly once and is told once.
    """
    stage = EnvironmentStartupStage.STARTING_CONTAINER
    with (
        startup_stage(request.startup, stage, clock=request.clock),
        _startup_output(request, stage, _CONTAINER_START_COMMAND) as output,
    ):
        yield output
    if request.log is not None:
        request.log(f"[docker] container {sandbox.id} started ({request.session_role})")


def _step_mark(request: RunEnvironmentRequest) -> dict[str, str]:
    """The environment that marks the session's commands as its step's, or none."""
    return {} if request.step_tag is None else step_tag_env(request.step_tag)


def _reporting_tool_builds(request: RunEnvironmentRequest) -> ToolBuildOutputOpener | None:
    """The request's tool-build opener, each build it opens reported as a startup stage.

    The opener is entered once per build that runs and never for a cache hit, so
    the stage is reported exactly when a build ran.
    """
    if request.startup is None:
        return request.tool_build_output
    opener = request.tool_build_output

    @contextmanager
    def open_build(tools: tuple[str, ...]) -> Iterator[OutputSink]:
        stage = EnvironmentStartupStage.BUILDING_TOOLS
        with startup_stage(request.startup, stage, clock=request.clock):
            if opener is None:
                yield DISCARD
            else:
                with opener(tools) as output:
                    yield output

    return open_build


class _OrphanReaper(Protocol):
    """End the agent processes and containers a dead run host left behind."""

    def __call__(
        self,
        run_id: str,
        *,
        docker: DockerCli,
        log: Callable[[str], None],
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class DockerEnvironmentConfig:
    """Optional image override and Docker CLI seam for the Docker run environment.

    ``docker`` is the CLI the environment probes the host daemon with (before
    any image build, for a task that needs a container runtime) and
    ``build_runner`` is what runs the agent image's ``docker build``. The
    unrecorded ``orphan_reaper`` effect lets tests keep recovery off the host.
    ``None`` selects the production effect for each seam.

    ``image_build_timeout_seconds`` is the budget for the whole agent image
    build (every Docker command together); ``None`` selects the image
    library's default. A caller that runs under its own
    limit, such as a test tier, sets it below that limit so a stalled build
    ends in an error carrying the build output instead of in the caller's
    timeout.

    ``image_cache_from`` supplies Docker images with inline build-cache metadata
    to the image builder. It is explicit build configuration, independent of
    the installed image's identity and the caller's process environment.
    """

    image: str | None = None
    docker: DockerCli | None = None
    build_runner: DockerBuildRunner | None = None
    orphan_reaper: _OrphanReaper | None = None
    image_build_timeout_seconds: float | None = None
    image_cache_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditorExtras:
    """What an environment adds to the Docker editor container it opens.

    *resources* are extra mounts (a host broker's socket, say) and *env* extra
    container variables. *same_path_workspace* mounts the workspace at its host
    path, for an agent that shares paths with a host-owned broker.
    *attach_accelerator* is ``False`` for an editor that holds no GPU while a
    remote scheduler runs the GPU work.
    """

    resources: tuple[HostResource, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    same_path_workspace: bool = False
    attach_accelerator: bool = True


@dataclass(frozen=True)
class BuiltEditor:
    """The built, unstarted editor command runner and the Git fact its mount plan implies."""

    runner: CommandRunner
    #: From the same plan that mounted the workspace's Git metadata.
    agent_git_writable: bool


class DockerEnvironment(NoCaptureRecovery):
    """Run agents and evaluator commands in a Docker sandbox."""

    isolated = True
    uses_docker = True
    requires_local_profiler_preflight = True
    materialize_local_model_weights = True
    default_profiler_id = "nsys"
    supported_profiler_ids: frozenset[str] | None = None

    def _docker_settings(self) -> DockerEnvironmentConfig:
        """The Docker client and reaping settings; an environment with another config says its own."""
        return self.config

    def __init__(self, config: DockerEnvironmentConfig) -> None:
        """Configure Docker execution from its image settings."""
        self.config = config
        self.backend_image = config.image

    def locate_tool(self, executable: str, site: ToolSite) -> ToolLocation:
        """Assume the run image provides *executable*: the container is both sites."""
        del site
        return assumed_tool(executable)

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> DockerEnvironment:
        """Build Docker configuration from CLI options.

        ``build_runner`` is the unrecorded injection seam for the agent image build,
        ``docker`` the one for the host's ``docker`` client, and ``orphan_reaper``
        the one for process confinement while recovering the previous host.
        """
        image = options.get("image")
        build_runner = options.get("build_runner")
        return cls(
            DockerEnvironmentConfig(
                image=str(image) if image else None,
                docker=cast("DockerCli | None", options.get("docker")),
                build_runner=cast("DockerBuildRunner | None", build_runner),
                orphan_reaper=cast("_OrphanReaper | None", options.get("orphan_reaper")),
            )
        )

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Resolve Docker presentation facts."""
        return _PreparedRunEnvironment(DockerEnvironmentFacts(), partial(self._open, request))

    def _open(
        self, request: RunEnvironmentRequest, presentation: RunEnvironmentPresentation
    ) -> RunEnvironmentSession:
        """Start the Docker sandbox and resolve candidate-facing paths."""
        editor = self.build_editor(request)
        sandbox = editor.runner
        view = RunEnvironmentView(
            paths=_isolated_paths(
                request,
                cast("_AgentPathSandbox", sandbox),
                evaluator_tools_root=SANDBOX_EVALUATOR_TOOLS_ROOT,
            ),
            prompt_notes=presentation.prompt_notes,
            isolated=True,
            cli_sandboxed=True,
            env_kind="docker",
            workspace_git_writable=editor.agent_git_writable,
        )
        with _starting_container(request, sandbox) as output:
            return command_environment_session(
                SandboxSession.start(sandbox=sandbox, view=view, output=output)
            )

    def attaches_backend_accelerator(self, *, docker_in_docker: bool) -> bool:
        """The editor container gets the accelerator, unless Docker-in-Docker forbids it."""
        return attaches_accelerator(docker_in_docker=docker_in_docker)

    def requires_sysbox_runtime(self, *, docker_in_docker: bool) -> bool:
        """A docker-in-docker sandbox runs under the host daemon's Sysbox."""
        return docker_in_docker

    def build_editor(
        self,
        request: RunEnvironmentRequest,
        extras: EditorExtras | None = None,
    ) -> BuiltEditor:
        """Build, without starting, the Docker sandbox the agent edits in.

        Every environment whose agent runs in Docker opens its editor here, so
        the image, the CLI credentials, and the mounts come from one place.
        *extras* is what an environment adds to the plain Docker editor. The
        caller starts it inside :func:`_starting_container`.
        """
        extras = extras or EditorExtras()
        image_helpers = import_module("vs_agent.api.images")
        requirements = request.evaluator_requirements
        log: Callable[[str], None] = request.log or (lambda _: None)
        toolchains: set[str] = set(evaluator_agent_toolchains(requirements))
        attach_accelerator = extras.attach_accelerator and self.attaches_backend_accelerator(
            docker_in_docker=request.docker_in_docker
        )
        if attach_accelerator:
            # Fail before the image build when the backend's accelerator is not
            # usable on this host; nothing falls back to running without it.
            request.backend.ensure_accelerator_available()
        if request.docker_in_docker:
            # Fail before the image build when the host has no Sysbox; nothing
            # falls back to the host socket.
            require_sysbox_runtime(self._docker(request.system))
            toolchains.add(CONTAINER_RUNTIME_TOOLCHAIN)
            log(DOCKER_IN_DOCKER_NOTICE)
        # The task image, when a task has a Dockerfile, is built by the
        # headless entrypoint and arrives here as the backend image; only the
        # agent layer is applied on top of it.
        base_image = _docker_backend_image(request)
        on_progress = _image_build_progress(request)
        with (
            _building_image(request),
            _startup_output(
                request,
                EnvironmentStartupStage.BUILDING_IMAGE,
                _agent_image_build_command(base_image),
            ) as output,
        ):
            settings = self._docker_settings()
            container_image = image_helpers.agent_image(
                base_image,
                features=image_features(toolchains),
                command_runner=settings.build_runner,
                cache_from=settings.image_cache_from,
                timeout=(
                    image_helpers.DEFAULT_IMAGE_BUILD_TIMEOUT_SECONDS
                    if settings.image_build_timeout_seconds is None
                    else settings.image_build_timeout_seconds
                ),
                on_progress=on_progress,
                output=output,
                clock=request.clock,
            )
        plan = container_mount_plan(request, same_path=extras.same_path_workspace)
        docker_symlinks = plan.symlinks
        resources = [*plan.resources, *extras.resources]
        resources += list(
            prepare_docker_evaluator_resources(
                requirements,
                request.workspace,
                builder=DockerToolBuilder(
                    backend=request.backend,
                    log_dir=request.log_dir,
                    image=container_image,
                    output=_reporting_tool_builds(request),
                ),
            )
        )
        resolved_cli = _cli_container_env(request)
        cli_provider_env: dict[str, str] = {}
        auth_files: list[tuple[str, str]] = []
        if resolved_cli is not None:
            provider, cli_provider_env = resolved_cli
            auth_files = auth_copy_paths(provider)
        # The agent image's baked toolchain root is read-only (the vibesys-rust
        # Feature chmods /opt/cargo a+rX, deliberately: an agent cannot apt/cargo
        # install mid-round), so a Cargo invocation that needs a crate the image
        # did not prebuild still has to create its registry cache somewhere
        # writable. That and uv's cache are the agent home's, never the workspace's.
        for name, value in agent_tool_cache_env().items():
            cli_provider_env.setdefault(name, value)
        if request.git_history_root is not None:
            cli_provider_env.setdefault("VIBESYS_GIT_HISTORY", "/opt/vibesys-history")
        cli_provider_env.update(extras.env)
        cli_provider_env.update(_step_mark(request))
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
            docker_in_docker=request.docker_in_docker,
            same_path_workspace=extras.same_path_workspace,
            attach_accelerator=attach_accelerator,
            run_id=request.run_id,
        )
        return BuiltEditor(sandbox, agent_git_writable=plan.agent_git_writable)

    def reconcile_writers(
        self,
        manifest: writer_pb.WriterManifest,
        *,
        system: EnvironmentSystem,
        log: Callable[[str], None],
    ) -> writer_pb.WriterReconciliation:
        """Reap recorded domains and verify death without opening writable resources."""
        return docker_writer_reconciler(self._docker(system), log).reconcile_writers(manifest)

    def reap_orphans(
        self, run_id: str, *, system: EnvironmentSystem, log: Callable[[str], None]
    ) -> None:
        """End the agents and containers a previous host process left for *run_id*."""
        settings = self._docker_settings()
        reaper = (
            settings.orphan_reaper if settings.orphan_reaper is not None else reap_orphaned_agents
        )
        reaper(run_id, docker=self._docker(system), log=log)

    def agent_work_reaper(
        self,
        run_id: str,
        *,
        system: EnvironmentSystem,
        blocking: BlockingRunner,
        local_namespace: Callable[[str], StateNamespace],
        log: Callable[[str], None],
    ) -> StageWorkReaper:
        """End one step's agent processes in the run's containers, which stay."""
        del local_namespace
        return DockerAgentWorkReaper(run_id, self._docker(system), blocking, log)

    def _docker(self, system: EnvironmentSystem) -> DockerCli:
        """The configured ``docker`` client, or the host's own over *system*'s processes."""
        settings = self._docker_settings()
        if settings.docker is not None:
            return settings.docker
        return SubprocessDockerCli(processes=system.processes, launcher=system.launcher)

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


class ModalEnvironment(RefusesDockerInDocker, _NoopWorkspaceRecovery):
    """Run candidate evaluations as Modal deployments."""

    isolated = True
    uses_docker = True
    requires_local_profiler_preflight = True
    materialize_local_model_weights = False
    default_profiler_id = "torch"
    supported_profiler_ids: frozenset[str] | None = frozenset({"auto", "torch", "none"})

    def attaches_backend_accelerator(self, *, docker_in_docker: bool) -> bool:
        """The local editor is CPU-only; the Modal deployment holds the accelerator."""
        del docker_in_docker
        return False

    def reap_orphans(
        self, run_id: str, *, system: EnvironmentSystem, log: Callable[[str], None]
    ) -> None:
        """End the agents a dead host left in the run's local agent container."""
        self._docker.reap_orphans(run_id, system=system, log=log)

    def agent_work_reaper(
        self,
        run_id: str,
        *,
        system: EnvironmentSystem,
        blocking: BlockingRunner,
        local_namespace: Callable[[str], StateNamespace],
        log: Callable[[str], None],
    ) -> StageWorkReaper:
        """End one step's agent processes in the run's local agent container.

        The agent runs in a local Docker container labelled with the run. What it starts on
        Modal (the candidate's ``modal run``) is a client process in that container and carries
        the step's tag with it. Modal has no per-step handle: its app is named per run.
        """
        return self._docker.agent_work_reaper(
            run_id, system=system, blocking=blocking, local_namespace=local_namespace, log=log
        )

    def __init__(self, config: ModalEnvironmentConfig) -> None:
        """Configure Modal execution from its deployment settings."""
        self.config = config
        self.model_volume: str | None = config.model_volume
        self.backend_image = config.image
        self._docker = DockerEnvironment(DockerEnvironmentConfig())

    def locate_tool(self, executable: str, site: ToolSite) -> ToolLocation:
        """Assume the Modal image provides *executable* where the deployment runs."""
        del site
        return assumed_tool(executable)

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
        reject_docker_in_docker(docker_in_docker=request.docker_in_docker, environment="modal")
        app_name = _modal_app_name(request.run_id, fallback=self.config.app)
        facts = ModalEnvironmentFacts(
            gpu=self.config.gpu,
            app_name=app_name,
            reference_path=reference_container_path(request).removeprefix("/workspace/"),
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
        it locally the way the local Docker path's is. Nothing installs
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

        requirements = request.evaluator_requirements
        container_image = _build_and_push_agent_image(
            request,
            image_features(evaluator_agent_toolchains(requirements), _MODAL_EDITOR_PIP_EXTRAS),
            backend_label="Modal",
        )

        plan = container_mount_plan(request)
        resources, docker_symlinks = plan.resources, plan.symlinks
        cli_provider_env, auth_files = _cli_provider_env_and_auth_files(request)
        for name, value in agent_tool_cache_env().items():
            cli_provider_env.setdefault(name, value)
        if request.git_history_root is not None:
            cli_provider_env.setdefault("VIBESYS_GIT_HISTORY", "/opt/vibesys-history")
        cli_provider_env["VIBESYS_MODAL_APP_NAME"] = app_name
        runtime_document = _write_runtime_document(request.log_dir, runtime_presentation)
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
            run_id=request.run_id,
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
        objective_document = materialize_effective_objective(request)
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
                runtime_go_module_cache_root=(
                    str(REMOTE_EVALUATOR_GO_MODULES_ROOT)
                    if requirements.go_module is not None
                    else None
                ),
            ),
        )
        with _starting_container(request, sandbox) as output:
            return command_environment_session(
                SandboxSession.start(
                    sandbox=sandbox,
                    output=output,
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
                                request.profiler_support_name
                                if request.profiler_support_path
                                else None
                            ),
                        ),
                        prompt_notes=presentation.prompt_notes,
                        isolated=True,
                        cli_sandboxed=True,
                        host_device_reselect=False,
                        env_kind="modal",
                        profile_execution="remote",
                        workspace_git_writable=plan.agent_git_writable,
                        deployment_release_env_var="VIBESYS_RELEASE_MODAL_DEPLOYMENT",
                        framework_setup_timeout_seconds=setup_timeout_seconds,
                    ),
                )
            )

    def _ensure_model_volume(self, request: RunEnvironmentRequest) -> None:
        if self.model_volume or request.ref_dir is None:
            return
        meta_path = request.ref_dir / "meta.json"
        if not meta_path.exists():
            return
        ensure_model_volume = import_module("vs_sandbox.api").ensure_model_volume

        meta = read_model_metadata(meta_path)
        hf_available = bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
        local_model = request.ref_dir / "model"
        local_path = None
        if not hf_available and local_model.exists() and local_model.resolve().is_dir():
            local_path = str(local_model.resolve())
        self.model_volume = ensure_model_volume(
            meta.model_id,
            revision=meta.revision,
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

        draft_meta = read_model_metadata(draft_meta_path)
        hf_available = bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
        local_draft = request.ref_dir / "draft_model"
        local_path = None
        if not hf_available and local_draft.exists() and local_draft.resolve().is_dir():
            local_path = str(local_draft.resolve())
        return ensure_model_volume(
            draft_meta.model_id,
            revision=draft_meta.revision,
            local_path=local_path,
            log=request.log or print,
        )


def _write_runtime_document(log_dir: Path, presentation: str) -> Path:
    """Write the runtime environment document the agent's container mounts read-only."""
    log_dir.mkdir(parents=True, exist_ok=True)
    write_for_agent(log_dir, _RUNTIME_DOCUMENT_NAME, presentation.encode("utf-8"))
    return log_dir / _RUNTIME_DOCUMENT_NAME


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
    built from the same resource list :func:`container_mount_plan` declared.
    """
    objective_document = materialize_effective_objective(request)
    requirements = request.evaluator_requirements
    evaluation = _prepare_evaluation_plan(
        request,
        requirements,
        TrustedEvaluationCommandPaths(
            source_project_root=request.workspace,
            runtime_project_root=sandbox_workspace_root(request),
            python_executable="python3",
            runtime_package_root=(
                sandbox.agent_path(requirements.package_root)
                if requirements.package_root is not None
                else None
            ),
            runtime_tools_root=evaluator_tools_root,
            runtime_go_module_cache_root=(
                str(SANDBOX_EVALUATOR_GO_MODULES_ROOT)
                if requirements.go_module is not None
                else None
            ),
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


def _cli_container_env(request: RunEnvironmentRequest) -> tuple[str, dict[str, str]] | None:
    """Return ``(provider, container env)`` when a CLI provider needs a container."""
    return cli_container_env(request.agent_backend, request.cli_provider)


def _cli_provider_env_and_auth_files(
    request: RunEnvironmentRequest,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Return the container env and staged auth copies for a CLI provider, if any."""
    return cli_provider_env_and_auth_files(request.agent_backend, request.cli_provider)


def _build_and_push_agent_image(
    request: RunEnvironmentRequest, features: tuple[FeatureRef, ...], *, backend_label: str
) -> str:
    """Build the agent image on the backend's base and push it for a remote Docker daemon.

    The build and the push each write to a stream of their own, both under the
    image-building stage. Returns the pullable digest reference.
    """
    image_helpers = import_module("vs_agent.api.images")
    base_image = _docker_backend_image(request)
    on_progress = _image_build_progress(request)
    building = EnvironmentStartupStage.BUILDING_IMAGE
    with _building_image(request):
        with _startup_output(request, building, _agent_image_build_command(base_image)) as output:
            image_id = image_helpers.agent_image(
                base_image,
                features=features,
                on_progress=on_progress,
                output=output,
                clock=request.clock,
            )
        with _startup_output(request, building, _AGENT_IMAGE_PUSH_COMMAND) as output:
            return _ensure_pushed_for_remote_backend(
                image_id,
                ensure_pushed=partial(image_helpers.ensure_pushed, output=output),
                backend_label=backend_label,
            )


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
