"""Run environment assembly over the existing sandbox implementations.

Layering:

    policy -> RunContext -> RunEnvironment -> ComputeBackendImpl.make_sandbox -> Sandbox

``RunContext`` prepares run resources before opening this session.

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

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile

# lint-waiver: LW-007062 [TC003]; Pydantic resolves this dataclass field annotation at runtime
from collections.abc import Mapping  # noqa: TC003
from contextlib import suppress
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from vibesys.backends import SandboxKind
from vibesys.constants import PROJECT_ROOT
from vibesys.evaluators import (
    PROJECT_ROOT_TOKEN,
    PYTHON_TOKEN,
    CargoGitToolSpec,
    EvaluatorToolError,
    EvaluatorToolLifecycleHooks,
    evaluator_tools_install_command,
    load_evaluator_package,
    prepare_evaluator_tools,
    tool_install_root,
    tool_path_replacements,
)
from vibesys.profilers import ProfilerKind
from vibesys.prompts import PROMPTS_DIR, render_template
from vibesys.sandbox._command_translation import (
    _reject_semantic_tokens_in_source,
    _translate_command_argument,
)
from vibesys.sandbox.modal_evaluator import encode_setup_command
from vibesys.skypilot.bridge import SkyPilotBridge
from vibesys.skypilot.config import load_cluster_profiles, resolve_profile
from vibesys.skypilot.runner import SkyPilotJobRunner, stable_cluster_name
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
from vs_sandbox.api import (
    BeforeReadyContext,
    HostResource,
    HostResourceAccess,
    ProjectPathPolicy,
    SandboxLifecycleHooks,
)

_RunEnvironmentName = Literal["local", "docker", "modal", "skypilot"]
_RECORDED_ENVIRONMENT_NAMES: tuple[_RunEnvironmentName, ...] = (
    "local",
    "docker",
    "modal",
    "skypilot",
)
_ENVIRONMENTS_TEMPLATE_DIR = PROMPTS_DIR / "environments"
_RUNTIME_OBJECTIVE_CONTAINER_PATH = "/opt/vibesys-runtime/objective.md"
"""The runtime resource declaration's ``agent_path`` for the effective objective.

Used in exactly one place: the ``HostResource`` :func:`_container_mount_plan`
declares for the materialized objective document. Every environment's
:class:`AgentPaths` instead asks the started sandbox's own
:meth:`~vs_sandbox.docker_sandbox.DockerSandbox.agent_path` for that document's
container path, so this constant and the resource declaration are the single
source of truth an environment consults."""
_SANDBOX_EVALUATOR_TOOLS_ROOT = Path("/opt/vibesys-evaluator-tools")
_REMOTE_EVALUATOR_TOOLS_ROOT = Path(".vibesys-evaluator-tools")
_REMOTE_EVALUATOR_TOOLCHAINS_ROOT = Path(".vibesys-evaluator-toolchains")
_EVALUATOR_RUST_TOOLCHAIN_VERSION = "1.92.0"
_DOCKER_EVALUATOR_CACHE_SCHEMA = 2
_PYTHON_DOWNLOAD_SCRIPT = """\
import sys
import time
import urllib.request

url = sys.argv[1].format(arch=sys.argv[3])
for attempt in range(5):
    try:
        urllib.request.urlretrieve(url, sys.argv[2])
        break
    except Exception:
        if attempt == 4:
            raise
        time.sleep(5)
"""

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vibesys.backends.base import ComputeBackendImpl
    from vibesys.domains.environment import EnvironmentBindMount
    from vibesys.evaluators.input_manifest import WorkspaceSource
    from vs_project.api import StateNamespace
    from vs_sandbox.api import Sandbox


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
class _CommandPathOptions:
    """Path translation choices for one evaluator command."""

    isolated: bool = False
    evaluator_package_root: str | None = None
    evaluator_tools_root: Path | None = None
    agent_path: Callable[[Path | str], str] | None = None


_DEFAULT_COMMAND_PATH_OPTIONS = _CommandPathOptions()


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
    # Optional namespace for environments that isolate each candidate in a
    # named deployment. The selected environment owns the concrete naming
    # rules; loops consume only the namespace capability.
    deployment_namespace: str | None = None
    supports_parallel_candidate_evaluation: bool = False
    # Optional environment variable understood by the environment-owned
    # evaluator wrapper when the final trusted command should release its
    # deployment lease.
    deployment_release_env_var: str | None = None
    # Extra wall-clock budget for environment-owned setup that wraps a trusted
    # command, such as deploying a fresh service and waiting for readiness.
    framework_setup_timeout_seconds: int = 0


@dataclass(frozen=True)
class CandidateRuntime:
    """Environment-owned prompt and lifecycle identity for one candidate."""

    prompt_notes: str
    deployment_name: str | None = None


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
    objective: str | None = None
    objective_document: Path | None = None
    accuracy_command: str | None = None
    benchmark_command: str | None = None
    benchmark_output_argument: str | None = None
    evaluator_package_root: Path | None = None
    evaluator_tools_root: Path | None = None
    profiler_support_path: str | None = None
    profiler_support_name: str | None = None
    git_history_root: Path | None = None
    environment_bind_mounts: tuple[EnvironmentBindMount, ...] = ()
    workspace_sources: tuple[WorkspaceSource, ...] = ()
    log: Callable[[str], None] | None = None
    framework_root: Path = PROJECT_ROOT
    project_path_policy: ProjectPathPolicy = field(default_factory=ProjectPathPolicy)
    state_namespace: StateNamespace | None = None


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


class RunEnvironment(Protocol):
    """Environment policy for run execution and candidate evaluation."""

    isolated: bool
    materialize_local_model_weights: bool
    default_profiler_kind: ProfilerKind
    supported_profiler_kinds: frozenset[ProfilerKind] | None
    backend_image: str | None

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:
        """Start the environment and return its run-scoped session."""
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

    def teardown_deployment(self, name: str, *, log: Callable[[str], None]) -> None:
        """Tear down a per-evaluation deployment such as a candidate service.

        Environments that dispatch each evaluation to its own named remote
        deployment implement this to release it once the evaluation is done;
        environments that run everything in-process are a no-op.
        """
        ...

    def candidate_runtime(
        self, view: RunEnvironmentView, generation: int, child_idx: int
    ) -> CandidateRuntime:
        """Return adapter-owned instructions and identity for one candidate."""
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

    def teardown_deployment(self, name: str, *, log: Callable[[str], None]) -> None:
        del name, log

    def candidate_runtime(
        self,
        view: RunEnvironmentView,
        generation: int,
        child_idx: int,
    ) -> CandidateRuntime:
        del generation, child_idx
        return CandidateRuntime(view.prompt_notes, view.deployment_namespace)


def _start_sandbox(sandbox: Sandbox) -> None:
    """Start the container of a sandbox kind that owns one.

    ``Sandbox`` is the command-execution contract and says
    nothing about container lifetime, so the lookup stays dynamic. Every
    Docker- and Modal-kind sandbox this module builds implements ``start``.
    """
    start = getattr(sandbox, "start", None)
    if not callable(start):
        message = f"{type(sandbox).__name__} has no container to start"
        raise TypeError(message)
    start()


def _stop_sandbox(sandbox: Sandbox) -> None:
    """Stop the sandbox's container, if it owns one."""
    stop = getattr(sandbox, "stop", None)
    if callable(stop):
        stop()


@dataclass
class _DefaultRunEnvironmentSession:
    sandbox: Sandbox
    view: RunEnvironmentView
    stop_on_close: bool = False
    _closed: bool = False

    def __enter__(self) -> _DefaultRunEnvironmentSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop the sandbox when this session owns its lifecycle."""
        if self._closed:
            return
        self._closed = True
        if self.stop_on_close:
            _stop_sandbox(self.sandbox)


class LocalEnvironment(_NoopWorkspaceRecovery):
    """Run agents directly on the host filesystem."""

    isolated: bool = False
    materialize_local_model_weights: bool = True
    default_profiler_kind: ProfilerKind = ProfilerKind.NSYS
    supported_profiler_kinds: frozenset[ProfilerKind] | None = None
    backend_image: str | None = None

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:
        """Create a host-local sandbox and its agent path view."""
        objective_document = _materialize_effective_objective(request)
        tools = _evaluator_tools(request)
        lifecycle_hooks: list[SandboxLifecycleHooks] = []
        if tools:
            lifecycle_hooks.append(
                EvaluatorToolLifecycleHooks(
                    tools,
                    _required_evaluator_tools_root(request),
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
        return _DefaultRunEnvironmentSession(
            sandbox=sandbox,
            view=RunEnvironmentView(
                paths=AgentPaths(
                    objective=(
                        str(objective_document)
                        if objective_document is not None
                        else "OBJECTIVE.md"
                    ),
                    accuracy_command=_environment_command(request, request.accuracy_command),
                    benchmark_command=_environment_command(request, request.benchmark_command),
                    profiler_support=request.profiler_support_path,
                ),
            ),
            stop_on_close=False,
        )


@dataclass(frozen=True)
class DockerEnvironmentConfig:
    """Optional image override for the Docker run environment."""

    image: str | None = None


class DockerEnvironment:
    """Run agents and evaluator commands in a Docker sandbox."""

    isolated = True
    materialize_local_model_weights = True
    default_profiler_kind = ProfilerKind.NSYS
    supported_profiler_kinds: frozenset[ProfilerKind] | None = None

    def __init__(self, config: DockerEnvironmentConfig) -> None:
        """Configure Docker execution from its image settings."""
        self.config = config
        self.backend_image = config.image

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> DockerEnvironment:
        """Build Docker environment configuration from CLI options."""
        image = options.get("image")
        return cls(DockerEnvironmentConfig(image=str(image) if image else None))

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:
        """Start the Docker sandbox and resolve candidate-facing paths."""
        image_helpers = import_module("vibesys.sandbox.images")
        tools = _evaluator_tools(request)
        # The task image, when a task has a Dockerfile, is built by the
        # headless entrypoint and arrives here as the backend image; only the
        # agent layer is applied on top of it.
        container_image = image_helpers.agent_image(
            _docker_backend_image(request),
            toolchains=_docker_agent_toolchains(request, tools),
        )
        resources, docker_symlinks = _container_mount_plan(request)
        resources = resources + _resources_for_mounts(
            _docker_evaluator_tool_mounts(request, tools, container_image=container_image),
            purpose="evaluator tool",
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
        resources = _dedupe_resources(resources)
        lifecycle_hooks = _symlink_lifecycle_hooks(docker_symlinks)

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
        # DOCKER-kind sandboxes always manage a container lifetime.
        _start_sandbox(sandbox)

        return _DefaultRunEnvironmentSession(
            sandbox=sandbox,
            view=RunEnvironmentView(
                paths=_isolated_paths(
                    request,
                    cast("_AgentPathSandbox", sandbox),
                    evaluator_tools_root=_SANDBOX_EVALUATOR_TOOLS_ROOT,
                ),
                prompt_notes=render_template(
                    "docker/prompt_notes.j2",
                    template_dir=_ENVIRONMENTS_TEMPLATE_DIR,
                    history_root=request.git_history_root,
                ),
                isolated=True,
                cli_sandboxed=True,
                env_kind="docker",
            ),
            stop_on_close=True,
        )

    def repair_workspace(
        self, workspace: Path, *, backend: ComputeBackendImpl, log: Callable[[str], None]
    ) -> None:
        """Chown workspace files back to the host user after Docker writes."""
        if not workspace.exists():
            return
        uid, gid = os.getuid(), os.getgid()
        chown_cmd = f"chown -R {uid}:{gid} /workspace"
        try:
            result = _docker_workspace_run(
                workspace,
                backend=backend,
                shell_command=chown_cmd,
                timeout=120,
            )
            if result.returncode != 0:
                log(
                    f"[warn] chown failed for {workspace} "
                    f"(rc={result.returncode}): "
                    f"{result.stderr.decode(errors='replace').strip()}"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"[warn] chown failed for {workspace}: {exc}")

    def remove_workspace_child(
        self, workspace: Path, rel_path: str, *, backend: ComputeBackendImpl
    ) -> bool:
        """Remove a workspace-relative child inside the Docker sandbox."""
        target = workspace / rel_path
        with suppress(OSError, subprocess.TimeoutExpired):
            _docker_workspace_run(
                workspace,
                backend=backend,
                shell_command=f"rm -rf -- {shlex.quote(f'/workspace/{rel_path}')}",
                timeout=120,
            )
        return not (target.exists() or target.is_symlink())

    def teardown_deployment(self, name: str, *, log: Callable[[str], None]) -> None:
        """Leave deployment teardown to the owning Docker session."""
        # The editor container is torn down by the session; nothing per-candidate.
        del name, log

    def candidate_runtime(
        self,
        view: RunEnvironmentView,
        generation: int,
        child_idx: int,
    ) -> CandidateRuntime:
        """Return candidate prompt/runtime settings for Docker evaluation."""
        del generation, child_idx
        return CandidateRuntime(view.prompt_notes, view.deployment_namespace)


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
            _stop_sandbox(self.sandbox)
        finally:
            self.bridge.close()


class SkyPilotEnvironment(DockerEnvironment):
    """CPU-only Docker editor with host-mediated SkyPilot evaluation."""

    config: SkyPilotEnvironmentConfig
    materialize_local_model_weights = False
    default_profiler_kind = ProfilerKind.NONE
    supported_profiler_kinds: frozenset[ProfilerKind] | None = frozenset(
        {ProfilerKind.AUTO, ProfilerKind.NONE}
    )

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

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:
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
        if self.config.resources is None:
            message = "SkyPilot requires portable run resources"
            raise ValueError(message)
        if request.state_namespace is None:
            message = "SkyPilot requires a machine-local state namespace"
            raise ValueError(message)
        profiles = load_cluster_profiles(self.config.profiles_file)
        cluster_resources = resolve_profile(profiles, self.config.profile, self.config.resources)
        cluster_name = stable_cluster_name(request.run_id, cluster_resources)
        commands: dict[str, tuple[str, ...]] = {}
        for kind, raw_command in (
            ("accuracy", request.accuracy_command),
            ("benchmark", request.benchmark_command),
        ):
            command = _remote_evaluator_command(request, raw_command)
            if command is not None:
                commands[kind] = command

        log = request.log or _noop_log
        bridge = SkyPilotBridge(
            runner=SkyPilotJobRunner(executable=self.config.executable),
            cluster_name=cluster_name,
            resources=cluster_resources,
            workspace=request.workspace,
            evaluator_package_root=request.evaluator_package_root,
            hidden_paths=request.project_path_policy.hidden_paths,
            commands=commands,
            framework_setup_command=_remote_evaluator_setup_command(request),
            benchmark_output_argument=request.benchmark_output_argument,
            state_namespace=request.state_namespace,
            socket_path=request.log_dir / "skypilot-bridge.sock",
            log=log,
        )
        try:
            bridge.start()

            image_helpers = import_module("vibesys.sandbox.images")

            tools = _evaluator_tools(request)
            container_image = image_helpers.agent_image(
                _docker_backend_image(request),
                toolchains=_docker_agent_toolchains(request, tools),
            )
            container_image = _ensure_pushed_for_remote_backend(
                container_image,
                ensure_pushed=image_helpers.ensure_pushed,
                backend_label="SkyPilot",
            )

            resources, docker_symlinks = _container_mount_plan(request)
            cli_provider_env, auth_files = _cli_provider_env_and_auth_files(request)
            cli_provider_env.setdefault("UV_CACHE_DIR", "/workspace/.cache/uv")
            helper_source = Path(__file__).with_name("skypilot_evaluator.py")
            helper_path = "/opt/vibesys-skypilot-evaluator.py"
            socket_path = "/opt/vibesys-skypilot/bridge.sock"
            caller_state_path = "/opt/vibesys-skypilot/caller-state"
            caller_state = request.state_namespace.external_directory("caller")
            cli_provider_env["VIBESYS_SKYPILOT_CALLER_STATE"] = caller_state_path
            resources = resources + _resources_for_mounts(
                [
                    (str(helper_source), helper_path, True),
                    (str(bridge.socket_path), socket_path, False),
                    (str(caller_state), caller_state_path, False),
                ]
            )
            runtime_document = request.log_dir / "runtime-environment.md"
            runtime_document.write_text(
                render_template(
                    "skypilot/runtime_notes.j2",
                    template_dir=_ENVIRONMENTS_TEMPLATE_DIR,
                    nodes=cluster_resources.nodes,
                    accelerators_per_node=cluster_resources.accelerators_per_node,
                    accelerator_type=cluster_resources.accelerator_type,
                    profile_name=cluster_resources.profile_name,
                )
            )
            runtime_path = "/opt/vibesys-runtime/environment.md"
            resources.append(
                _resource_for_mount(str(runtime_document), runtime_path, read_only=True)
            )
            sandbox = request.backend.make_sandbox(
                SandboxKind.DOCKER,
                host_workspace=str(request.workspace),
                log_path=request.log_dir / "docker.log",
                bind_mounts=[],
                resources=_dedupe_resources(resources),
                extra_env=cli_provider_env,
                auth_files=auth_files,
                lifecycle_hooks=_symlink_lifecycle_hooks(docker_symlinks),
                container_image=container_image,
                attach_accelerator=False,
            )
            _start_sandbox(sandbox)
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
                prompt_notes=render_template(
                    "skypilot/prompt_notes.j2",
                    template_dir=_ENVIRONMENTS_TEMPLATE_DIR,
                    runtime_container_path=agent_path_sandbox.agent_path(runtime_document),
                ),
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
    materialize_local_model_weights = False
    default_profiler_kind = ProfilerKind.TORCH
    supported_profiler_kinds: frozenset[ProfilerKind] | None = frozenset(
        {ProfilerKind.AUTO, ProfilerKind.TORCH, ProfilerKind.NONE}
    )

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

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:
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
        builds (:func:`~vibesys.sandbox.images.agent_image`), pushed to and
        pulled back from a registry (:func:`~vibesys.sandbox.images.ensure_pushed`),
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
        # Host-side: ensure Modal Volumes exist for the model + optional
        # draft.  These run before the Docker container starts and are
        # idempotent (skip-if-ready sentinel).
        self._ensure_model_volume(request)
        self._ensure_draft_volume(request)

        image_helpers = import_module("vibesys.sandbox.images")

        tools = _evaluator_tools(request)
        container_image = image_helpers.agent_image(
            _docker_backend_image(request),
            toolchains=_docker_agent_toolchains(request, tools),
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
        app_name = _modal_app_name(request.run_id, fallback=self.config.app)
        cli_provider_env["VIBESYS_MODAL_APP_NAME"] = app_name
        runtime_document = request.log_dir / "runtime-environment.md"
        reference_path = _reference_container_path(request).removeprefix("/workspace/")
        runtime_document.write_text(
            render_template(
                "modal/runtime_notes.j2",
                template_dir=_ENVIRONMENTS_TEMPLATE_DIR,
                gpu=self.config.gpu,
                app_name=app_name,
                workspace_sources=request.workspace_sources,
                reference_path=reference_path,
                history_root=request.git_history_root,
            )
        )
        runtime_container_path = "/opt/vibesys-runtime/environment.md"
        resources.append(
            _resource_for_mount(str(runtime_document), runtime_container_path, read_only=True)
        )
        evaluator_helper = request.framework_root / "src/vibesys/sandbox/modal_evaluator.py"
        evaluator_container_path = "/opt/vibesys-modal-evaluator.py"
        resources.append(
            _resource_for_mount(str(evaluator_helper), evaluator_container_path, read_only=True)
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
                _resource_for_mount(str(modal_auth), f"{agent_home}/.modal.toml", read_only=True)
            )
        modal_config_dir = Path.home() / ".modal"
        if modal_config_dir.is_dir():
            resources.append(
                _resource_for_mount(str(modal_config_dir), f"{agent_home}/.modal", read_only=True)
            )

        resources = _dedupe_resources(resources)
        lifecycle_hooks = _symlink_lifecycle_hooks(docker_symlinks)

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
        # DOCKER-kind sandboxes always manage a container lifetime.
        _start_sandbox(sandbox)

        setup_timeout_seconds = 1200
        evaluator_arguments = ["python", evaluator_container_path]
        if self.config.entrypoint is not None:
            evaluator_arguments.extend(("--entrypoint", self.config.entrypoint))
        evaluator_arguments.extend(("--readiness-timeout-seconds", str(setup_timeout_seconds)))
        remote_setup = _remote_evaluator_setup_command(request, preserve_bootstrap=True)
        if remote_setup is not None:
            evaluator_arguments.extend(
                ("--setup-command-base64", encode_setup_command(("sh", "-c", remote_setup)))
            )
        if request.evaluator_package_root is not None:
            evaluator_arguments.extend(
                ("--evaluator-package-root", "/opt/vibesys-evaluator-package")
            )
        evaluator_prefix = f"{shlex.join(evaluator_arguments)} --"
        agent_path_sandbox = cast("_AgentPathSandbox", sandbox)
        objective_document = _materialize_effective_objective(request)
        return _DefaultRunEnvironmentSession(
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
                        _environment_command(
                            request,
                            request.accuracy_command,
                            paths=_CommandPathOptions(
                                isolated=True,
                                evaluator_package_root=".vibesys-evaluator-package",
                                evaluator_tools_root=_REMOTE_EVALUATOR_TOOLS_ROOT,
                            ),
                        ),
                    ),
                    benchmark_command=_prefix_command(
                        evaluator_prefix,
                        _environment_command(
                            request,
                            request.benchmark_command,
                            paths=_CommandPathOptions(
                                isolated=True,
                                evaluator_package_root=".vibesys-evaluator-package",
                                evaluator_tools_root=_REMOTE_EVALUATOR_TOOLS_ROOT,
                            ),
                        ),
                    ),
                    profiler_support=(
                        request.profiler_support_name if request.profiler_support_path else None
                    ),
                ),
                prompt_notes=render_template(
                    "modal/prompt_notes.j2",
                    template_dir=_ENVIRONMENTS_TEMPLATE_DIR,
                    runtime_container_path=agent_path_sandbox.agent_path(runtime_document),
                ),
                isolated=True,
                cli_sandboxed=True,
                host_device_reselect=False,
                env_kind="modal",
                profile_execution="remote",
                deployment_namespace=app_name,
                supports_parallel_candidate_evaluation=True,
                deployment_release_env_var="VIBESYS_RELEASE_MODAL_DEPLOYMENT",
                framework_setup_timeout_seconds=setup_timeout_seconds,
            ),
            stop_on_close=True,
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

    def teardown_deployment(self, name: str, *, log: Callable[[str], None]) -> None:
        """Stop an idle candidate app so deployed apps don't accumulate.

        Each candidate deploys its GPU server to its own ``vibesys-…-g<g>c<c>``
        Modal app; Modal scales the *containers* to zero after
        ``scaledown_window`` (no ongoing GPU cost), but the app objects and
        their web endpoints linger until stopped. We stop it on the host via
        the Modal CLI — the stable public interface, authenticated by the same
        ``~/.modal.toml`` the SDK path uses. Best-effort: a failed stop just
        leaves an idle app behind and must never fail a run.
        """
        try:
            result = subprocess.run(  # noqa: S603  # lint-waiver: LW-009088 [S603]; Modal teardown passes fixed argv directly with no shell.
                [sys.executable, "-m", "modal", "app", "stop", name, "--yes"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"[warn] modal app stop {name} raised: {exc}")
            return
        if result.returncode != 0:
            log(
                f"[warn] modal app stop {name} failed "
                f"(exit {result.returncode}): {result.stderr.strip()[:200]}"
            )
        else:
            log(f"[modal] stopped candidate app {name}")

    def candidate_runtime(
        self, view: RunEnvironmentView, generation: int, child_idx: int
    ) -> CandidateRuntime:
        """Render candidate-specific Modal deployment details and prompt notes."""
        base_name = view.deployment_namespace
        if not base_name:
            return CandidateRuntime(view.prompt_notes)
        candidate_name = candidate_modal_app_name(base_name, generation, child_idx)
        return CandidateRuntime(
            prompt_notes=render_template(
                "modal/candidate_override.j2",
                template_dir=_ENVIRONMENTS_TEMPLATE_DIR,
                prompt_notes=view.prompt_notes,
                base_name=base_name,
                candidate_name=candidate_name,
            ),
            deployment_name=candidate_name,
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
    message = f"unknown run environment: {spec.name!r}"
    raise ValueError(message)


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


def candidate_modal_app_name(base_app_name: str, generation: int, child_idx: int) -> str:
    """Derive a per-candidate Modal app name from the per-run base name.

    Every candidate in a run must deploy to its *own* Modal app: Modal app
    logs are cumulative per app name, so if all candidates share one name the
    judge reads the first (often broken) deploy's crash for every later
    candidate and fails them identically.  We append a ``-g<gen>c<child>``
    suffix, truncating the base to keep the whole name within Modal's 63-char
    limit.  The leading timestamp+uuid in the base keeps it unique per run
    even after truncation.
    """
    suffix = f"-g{generation}c{child_idx}"
    keep = 63 - len(suffix)
    trimmed = base_app_name[:keep].rstrip("-")
    return f"{trimmed}{suffix}"


def _materialize_effective_objective(request: RunEnvironmentRequest) -> Path | None:
    """Persist the exact run objective outside candidate Git history.

    Operator constraints are composed at the CLI boundary. Keeping the effective
    text in the framework-owned log directory makes it survive candidate rollback
    and resume, while isolated environments mount it read-only for every role.
    """
    if request.objective is None:
        return None
    if request.objective_document is not None:
        path = request.objective_document.resolve()
        try:
            path.relative_to(request.workspace.resolve())
        except ValueError as exc:
            message = f"effective objective must be inside the project workspace: {path}"
            raise ValueError(message) from exc
        if not path.is_file() or path.read_text() != request.objective:
            message = f"effective objective does not match its committed document: {path}"
            raise ValueError(message)
        return path
    path = request.log_dir / "effective-objective.md"
    path.write_text(request.objective)
    return path


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
    return AgentPaths(
        objective=(
            sandbox.agent_path(objective_document)
            if objective_document is not None
            else "OBJECTIVE.md"
        ),
        accuracy_command=_environment_command(
            request,
            request.accuracy_command,
            paths=_CommandPathOptions(
                isolated=True,
                evaluator_tools_root=evaluator_tools_root,
                agent_path=sandbox.agent_path,
            ),
        ),
        benchmark_command=_environment_command(
            request,
            request.benchmark_command,
            paths=_CommandPathOptions(
                isolated=True,
                evaluator_tools_root=evaluator_tools_root,
                agent_path=sandbox.agent_path,
            ),
        ),
        profiler_support=(request.profiler_support_name if request.profiler_support_path else None),
    )


def _prefix_command(prefix: str, command: str | None) -> str | None:
    if not command:
        return None
    return f"{prefix} {command}"


def _noop_log(message: str) -> None:
    del message


def _environment_command(
    request: RunEnvironmentRequest,
    command: str | None,
    *,
    paths: _CommandPathOptions = _DEFAULT_COMMAND_PATH_OPTIONS,
) -> str | None:
    """Translate semantic paths in argv, then quote the translated command.

    ``agent_path`` maps evaluator paths through the started sandbox when the
    caller does not pass an explicit ``evaluator_package_root`` override.
    Callers without a sandbox, including remote dispatch wrappers, pass an
    explicit override instead.
    """
    if command is None:
        return None
    try:
        arguments = shlex.split(command)
    except ValueError as exc:
        message = f"invalid evaluator command: {exc}"
        raise ValueError(message) from exc
    replacements = [
        (PROJECT_ROOT_TOKEN, "/workspace" if paths.isolated else str(request.workspace)),
        (PYTHON_TOKEN, "python3" if paths.isolated else sys.executable),
    ]
    if request.evaluator_package_root is not None:
        if paths.evaluator_package_root is not None:
            translated_root = paths.evaluator_package_root
        elif paths.agent_path is not None:
            translated_root = paths.agent_path(request.evaluator_package_root)
        elif paths.isolated:
            translated_root = "/opt/vibesys-evaluator-package"
        else:
            translated_root = str(request.evaluator_package_root)
        replacements.append((str(request.evaluator_package_root), translated_root))
    tools = _evaluator_tools(request)
    if tools:
        tools_root = paths.evaluator_tools_root or _required_evaluator_tools_root(request)
        replacements.extend(tool_path_replacements(tools, tools_root).items())
    _reject_semantic_tokens_in_source(arguments, replacements)
    arguments = [_translate_command_argument(argument, replacements) for argument in arguments]
    return shlex.join(arguments)


def _remote_evaluator_command(
    request: RunEnvironmentRequest, command: str | None
) -> tuple[str, ...] | None:
    """Translate a trusted command into the synchronized remote workdir."""
    rendered = _environment_command(
        request,
        command,
        paths=_CommandPathOptions(
            isolated=True,
            evaluator_tools_root=_REMOTE_EVALUATOR_TOOLS_ROOT,
        ),
    )
    if rendered is None:
        return None
    arguments = shlex.split(rendered)
    replacements = [
        ("/opt/vibesys-evaluator-package", ".vibesys-evaluator-package"),
        ("/workspace", "."),
    ]
    return tuple(_translate_command_argument(argument, replacements) for argument in arguments)


def _docker_workspace_run(
    workspace: Path,
    *,
    backend: ComputeBackendImpl,
    shell_command: str,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    image = getattr(backend, "image", "ubuntu:latest")
    return subprocess.run(  # noqa: S603  # lint-waiver: LW-009089 [S603]; Docker receives framework-built commands and a quoted workspace path with no host shell.
        [  # noqa: S607  # lint-waiver: LW-009090 [S607]; Docker is intentionally resolved from the operator's configured PATH.
            "docker",
            "run",
            "--rm",
            "-v",
            f"{workspace}:/workspace",
            image,
            "bash",
            "-c",
            shell_command,
        ],
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _resource_for_mount(
    host_path: str,
    container_path: str,
    *,
    read_only: bool,
    purpose: str = "container mount",
) -> HostResource:
    """Lower one ``(host, container, readonly)`` mount tuple to a ``HostResource``.

    ``agent_path`` is left unset (identity) only when the container path
    matches the host path verbatim; every mount built here presents at a
    dedicated container location, so this is effectively always set.
    """
    access = HostResourceAccess.READ_ONLY if read_only else HostResourceAccess.READ_WRITE
    normalized_host = str(Path(host_path))
    agent_path = container_path if container_path != normalized_host else None
    return HostResource(Path(host_path), access, purpose, agent_path)


def _resources_for_mounts(
    mounts: Sequence[tuple[str, str, bool]],
    *,
    purpose: str = "container mount",
) -> list[HostResource]:
    """Lower a batch of ``(host, container, readonly)`` mounts to resources."""
    return [
        _resource_for_mount(host, container, read_only=read_only, purpose=purpose)
        for host, container, read_only in mounts
    ]


def _dedupe_resources(resources: Sequence[HostResource]) -> list[HostResource]:
    """Keep one resource per container-visible path, preferring the last declared.

    Mirrors the historical ``bind_mounts`` dedup: a path re-declared later
    (for example an evaluator-tool mount landing on a path an earlier, coarser
    grant already covered) wins, while the position of the first declaration
    is preserved.
    """
    seen: dict[str, HostResource] = {}
    for resource in resources:
        key = resource.agent_path if resource.agent_path is not None else str(resource.path)
        seen[key] = resource
    return list(seen.values())


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
        _collect_symlink_mounts(
            ref_dir,
            reference_container_path,
            bind_mounts=bind_mounts,
            symlinks=symlinks,
            skip=skip_environment_mount_symlinks,
        )
        if ref_dir.parent != ref_dir:
            _collect_symlink_mounts(
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
        host_path = _find_mount_root(resolved)
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

    if request.evaluator_package_root is not None:
        bind_mounts.append(
            (
                str(request.evaluator_package_root),
                "/opt/vibesys-evaluator-package",
                True,
            )
        )

    bind_mounts.extend(_container_project_policy_mounts(request))

    if (
        include_cli_provider_mounts
        and (request.agent_backend or AgentBackend.CLI) == AgentBackend.CLI
        and request.cli_provider
    ):
        bind_mounts.extend(auth_bind_mounts(request.cli_provider))
        bind_mounts.append((str(request.framework_root), "/opt/vibesys", True))

    return _resources_for_mounts(bind_mounts), symlinks


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


def _container_project_policy_mounts(
    request: RunEnvironmentRequest,
) -> list[tuple[str, str, bool]]:
    """Translate project visibility rules into Docker overlay mounts."""
    resolved = request.project_path_policy.resolve(request.workspace)
    workspace = request.workspace.resolve()
    mounts = [
        (
            str(protected.path),
            f"/workspace/{protected.path.relative_to(workspace).as_posix()}",
            True,
        )
        for protected in resolved.read_only_paths
    ]

    mask_root = request.log_dir / "sandbox-hidden"
    for index, hidden in enumerate(resolved.hidden_paths):
        mask = mask_root / str(index)
        if hidden.is_directory:
            mask.mkdir(parents=True, exist_ok=True)
        else:
            mask.parent.mkdir(parents=True, exist_ok=True)
            mask.touch(exist_ok=True)
        relative = hidden.path.relative_to(workspace).as_posix()
        mounts.append((str(mask), f"/workspace/{relative}", True))
    return mounts


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

    ``ensure_pushed`` (:func:`vibesys.sandbox.images.ensure_pushed`) already
    raises :class:`~vibesys.sandbox.images.ImagePushError` naming the image
    and what went wrong; this only adds which run environment could not
    start because of it, so the operator does not have to guess whether a
    Modal or a SkyPilot launch is the one that failed to reach the registry.
    """
    image_push_error = import_module("vibesys.sandbox.images").ImagePushError

    try:
        return ensure_pushed(image_id)
    except image_push_error as exc:
        raise image_push_error.run_environment_push_failed(image_id, backend_label, exc) from exc


def _evaluator_container_setup(
    request: RunEnvironmentRequest,
    *,
    include_declared_tools: bool = True,
    rootless: bool = False,
) -> list[str]:
    """Install the toolchain required by bundled evaluator packages."""
    if request.evaluator_package_root is None:
        return []
    toolchains = set(load_evaluator_package(request.evaluator_package_root).metadata.toolchains)
    if include_declared_tools and _evaluator_tools(request):
        toolchains.add("rust")
    if not toolchains:
        return []
    commands = (
        [
            "command -v python3 >/dev/null && command -v tar >/dev/null || "
            "{ echo 'evaluator setup requires Python 3 and tar in this remote environment' "
            ">&2; exit 1; }",
            f"mkdir -p .bin {shlex.quote(str(_REMOTE_EVALUATOR_TOOLCHAINS_ROOT))}",
            'PATH="$PWD/.bin:$PATH"; export PATH',
        ]
        if rootless
        else [
            "command -v python3 >/dev/null && command -v tar >/dev/null || "
            "{ apt-get update -qq && apt-get install -y -qq python3 ca-certificates tar; }",
        ]
    )
    if "go" in toolchains:
        go_destination = (
            f"$PWD/{_REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/go" if rootless else "/usr/local/go"
        )
        go_link = "$PWD/.bin/go" if rootless else "/usr/local/bin/go"
        go_archive = (
            f"{_REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/go.tgz" if rootless else "/tmp/vibesys-go.tgz"  # noqa: S108  # lint-waiver: LW-009095 [S108]; fixed scratch file is private to the isolated setup container and immediately removed.
        )
        go_download = _python_download_command(
            "https://go.dev/dl/go1.23.12.linux-{arch}.tar.gz",
            go_archive,
            architecture_variable="go_arch",
        )
        commands.append(
            "go_version=$(go env GOVERSION 2>/dev/null || true); "
            'case "$go_version" in go1.2[1-9]*|go1.[3-9][0-9]*) ;; *) '
            'arch=$(uname -m); case "$arch" in x86_64) go_arch=amd64 ;; '
            "aarch64|arm64) go_arch=arm64 ;; *) "
            'echo "unsupported Go architecture: $arch" >&2; exit 1 ;; esac; '
            f"{go_download} || "
            "{ echo 'failed to download evaluator Go toolchain' >&2; exit 1; }; "
            f"rm -rf {go_destination} && mkdir -p $(dirname {go_destination}) && "
            f"tar -C $(dirname {go_destination}) -xzf {go_archive} && "
            f"ln -sf {go_destination}/bin/go {go_link} && rm -f {go_archive} || "
            "{ echo 'failed to install evaluator Go toolchain' >&2; exit 1; } ;; esac"
        )
        commands.append("GOWORK=off; export GOWORK")
    if "rust" in toolchains:
        rustup_environment = (
            f"RUSTUP_HOME=$PWD/{_REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/rustup "
            f"CARGO_HOME=$PWD/{_REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/cargo "
            if rootless
            else ""
        )
        cargo_link = (
            f"ln -sf $PWD/{_REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/cargo/bin/* $PWD/.bin/"
            if rootless
            else "ln -sf /root/.cargo/bin/* /usr/local/bin/"
        )
        rustup_init = (
            f"{_REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/rustup-init"
            if rootless
            else "/tmp/vibesys-rustup-init"  # noqa: S108  # lint-waiver: LW-009096 [S108]; fixed scratch file is private to the isolated setup container and immediately removed.
        )
        rustup_download = _python_download_command(
            "https://static.rust-lang.org/rustup/dist/{arch}-unknown-linux-gnu/rustup-init",
            rustup_init,
            architecture_variable="rust_arch",
        )
        commands.append(
            "rust_version=$(rustc --version 2>/dev/null | awk '{print $2}' || true); "
            "cargo_version=$(cargo --version 2>/dev/null | awk '{print $2}' || true); "
            'rust_ready=; case "$rust_version" in '
            "1.7[89].*|1.[89][0-9].*|1.[1-9][0-9][0-9].*) "
            'case "$cargo_version" in ?*) rust_ready=1 ;; esac ;; esac; '
            'if [ "$rust_ready" != 1 ]; then '
            'arch=$(uname -m); case "$arch" in x86_64) rust_arch=x86_64 ;; '
            "aarch64|arm64) rust_arch=aarch64 ;; *) "
            'echo "unsupported Rust architecture: $arch" >&2; exit 1 ;; esac; '
            f"{rustup_download} || "
            "{ echo 'failed to download evaluator Rust toolchain' >&2; exit 1; }; "
            f"chmod +x {rustup_init} && {rustup_environment}{rustup_init} "
            "-y --profile minimal --no-modify-path "
            f"--default-toolchain {_EVALUATOR_RUST_TOOLCHAIN_VERSION} && "
            f"{cargo_link} && rm -f {rustup_init} || "
            "{ echo 'failed to install evaluator Rust toolchain' >&2; exit 1; }; fi"
        )
        if rootless:
            commands.append(
                f"if [ -d $PWD/{_REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/cargo ]; then "
                f"RUSTUP_HOME=$PWD/{_REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/rustup; "
                f"CARGO_HOME=$PWD/{_REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/cargo; "
                "export RUSTUP_HOME CARGO_HOME; fi"
            )
    return commands


def _python_download_command(
    url_template: str,
    destination: str,
    *,
    architecture_variable: str,
) -> str:
    command = shlex.join(("python3", "-c", _PYTHON_DOWNLOAD_SCRIPT, url_template, destination))
    return f'{command} "${{{architecture_variable}}}"'


class _EvaluatorToolBuildRequiredError(RuntimeError):
    pass


def _docker_evaluator_tool_mounts(
    request: RunEnvironmentRequest,
    tools: Mapping[str, CargoGitToolSpec],
    *,
    container_image: str | None = None,
) -> list[tuple[str, str, bool]]:
    """Build tools in the target image, then mount verified roots read-only."""
    if not tools:
        return []
    resolved_image = container_image or _resolve_docker_image_id(_docker_backend_image(request))
    host_parent = _docker_evaluator_tools_root(request, image_identity=resolved_image)

    def require_builder(_arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise _EvaluatorToolBuildRequiredError

    try:
        prepare_evaluator_tools(tools, host_parent, command_runner=require_builder)
    except _EvaluatorToolBuildRequiredError:
        for name in tools:
            (host_parent / name).mkdir(parents=True, exist_ok=True)
        builder_workspace = request.log_dir / "evaluator-tool-builder-workspace"
        builder_workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".host-owner-", dir=host_parent) as marker:
            container_marker = str(_SANDBOX_EVALUATOR_TOOLS_ROOT / Path(marker.name).name)
            builder = request.backend.make_sandbox(
                SandboxKind.DOCKER,
                host_workspace=str(builder_workspace),
                log_path=request.log_dir / "evaluator-tool-builder.log",
                bind_mounts=[
                    (str(host_parent), str(_SANDBOX_EVALUATOR_TOOLS_ROOT), False),
                ],
                extra_env={},
                extra_init_commands=_evaluator_container_setup(request),
                lifecycle_hooks=[EvaluatorToolLifecycleHooks(tools, _SANDBOX_EVALUATOR_TOOLS_ROOT)],
                attach_accelerator=False,
                ephemeral=True,
                container_image=resolved_image,
            )
            try:
                _start_sandbox(builder)
                container_roots = [
                    str(tool_install_root(_SANDBOX_EVALUATOR_TOOLS_ROOT, name, spec))
                    for name, spec in tools.items()
                ]
                ownership_script = (
                    'owner=$(stat -c "%u:%g" -- "$1") && shift && chown -R "$owner" -- "$@"'
                )
                ownership = builder.execute(
                    shlex.join(
                        (
                            "sh",
                            "-c",
                            ownership_script,
                            "vibesys-chown",
                            container_marker,
                            *container_roots,
                        )
                    ),
                    timeout=120,
                )
                if ownership.exit_code != 0:
                    detail = (ownership.output or "chown failed").strip()
                    raise EvaluatorToolError.cache_ownership_failed(detail[:500])
            finally:
                _stop_sandbox(builder)
        try:
            prepare_evaluator_tools(tools, host_parent, command_runner=require_builder)
        except _EvaluatorToolBuildRequiredError as exc:
            raise EvaluatorToolError.builder_incomplete() from exc

    return [
        (
            str(tool_install_root(host_parent, name, spec)),
            str(tool_install_root(_SANDBOX_EVALUATOR_TOOLS_ROOT, name, spec)),
            True,
        )
        for name, spec in tools.items()
    ]


def _docker_evaluator_tools_root(
    request: RunEnvironmentRequest,
    *,
    image_identity: str,
) -> Path:
    base = _required_evaluator_tools_root(request)
    identity = (
        f"{_DOCKER_EVALUATOR_CACHE_SCHEMA}\0{image_identity}\0{os.uname().machine}\0"
        f"{_EVALUATOR_RUST_TOOLCHAIN_VERSION}"
    ).encode()
    return base / "docker" / hashlib.sha256(identity).hexdigest()


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


def _inspect_docker_image_id(image: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603  # lint-waiver: LW-009091 [S603]; inspect uses fixed Docker argv and an image value as a single shell-free argument.
            ["docker", "image", "inspect", "--format={{.Id}}", image],  # noqa: S607  # lint-waiver: LW-009092 [S607]; Docker is intentionally resolved from the operator's configured PATH.
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode != 0:
        return None
    identity = result.stdout.strip()
    return (
        identity
        if identity.startswith("sha256:") and not any(c.isspace() for c in identity)
        else None
    )


def _resolve_docker_image_id(image: str) -> str:
    """Resolve and pin the exact Docker image used for tool build and execution."""
    if identity := _inspect_docker_image_id(image):
        return identity
    try:
        pull = subprocess.run(  # noqa: S603  # lint-waiver: LW-009093 [S603]; pull uses fixed Docker argv and an image value as a single shell-free argument.
            ["docker", "image", "pull", image],  # noqa: S607  # lint-waiver: LW-009094 [S607]; Docker is intentionally resolved from the operator's configured PATH.
            capture_output=True,
            check=False,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise EvaluatorToolError.docker_missing() from exc
    except subprocess.TimeoutExpired as exc:
        raise EvaluatorToolError.docker_pull_timed_out(image) from exc
    if pull.returncode != 0:
        detail = (pull.stderr or pull.stdout or "docker image pull failed").strip()[:500]
        raise EvaluatorToolError.docker_image_unresolvable(image, detail)
    if identity := _inspect_docker_image_id(image):
        return identity
    raise EvaluatorToolError.docker_image_id_missing(image)


def _remote_evaluator_setup_command(
    request: RunEnvironmentRequest,
    *,
    preserve_bootstrap: bool = False,
) -> str | None:
    """Build idempotent setup for the environment that runs the evaluator."""
    commands = _evaluator_container_setup(request, rootless=True)
    tools = _evaluator_tools(request)
    if tools:
        commands.append(evaluator_tools_install_command(tools, _REMOTE_EVALUATOR_TOOLS_ROOT))
    if not commands:
        return None
    reserved_paths = [
        str(_REMOTE_EVALUATOR_TOOLS_ROOT),
        str(_REMOTE_EVALUATOR_TOOLCHAINS_ROOT),
    ]
    if not preserve_bootstrap:
        reserved_paths[:0] = [".bin", ".pip", ".uv-cache"]
    reserved = shlex.join(("rm", "-rf", "--", *reserved_paths))
    return "set -e\n" + "\n".join((reserved, *commands))


def _evaluator_tools(request: RunEnvironmentRequest) -> dict[str, CargoGitToolSpec]:
    if request.evaluator_package_root is None:
        return {}
    return load_evaluator_package(request.evaluator_package_root).metadata.tools


def _docker_agent_toolchains(
    request: RunEnvironmentRequest,
    tools: Mapping[str, CargoGitToolSpec],
) -> frozenset[str]:
    """Return the toolchains the agent image should bake in for this run.

    Mirrors what :func:`_evaluator_container_setup` installs per-run today:
    the evaluator package's declared toolchains, plus ``"rust"`` when the run
    also needs cargo-git evaluator tools. *tools* is the caller's own
    :func:`_evaluator_tools` result, passed in rather than recomputed.
    """
    toolchains: set[str] = set()
    if request.evaluator_package_root is not None:
        toolchains |= set(
            load_evaluator_package(request.evaluator_package_root).metadata.toolchains
        )
    if tools:
        toolchains.add("rust")
    return frozenset(toolchains)


def _required_evaluator_tools_root(request: RunEnvironmentRequest) -> Path:
    if request.evaluator_tools_root is None:
        message = "evaluator tools require an operator-owned tools root"
        raise ValueError(message)
    root = request.evaluator_tools_root.resolve()
    try:
        root.relative_to(request.workspace.resolve())
    except ValueError:
        return root
    message = "evaluator tools root must be outside the candidate workspace"
    raise ValueError(message)


@dataclass(frozen=True)
class _SymlinkLifecycleHooks(SandboxLifecycleHooks):
    commands: tuple[str, ...]

    def before_ready(self, context: BeforeReadyContext) -> None:
        for command in self.commands:
            result = context.sandbox.execute(command)
            if result.exit_code != 0:
                message = f"failed to create sandbox symlink with {command!r}: {result.output}"
                raise RuntimeError(message)
        save_symlink_commands = getattr(context.sandbox, "save_symlink_commands", None)
        if callable(save_symlink_commands):
            save_symlink_commands(list(self.commands))


def _symlink_lifecycle_hooks(
    symlinks: list[tuple[str, str]],
) -> list[SandboxLifecycleHooks]:
    if not symlinks:
        return []
    commands = tuple(
        f"ln -sfn {shlex.quote(target)} {shlex.quote(link)}" for link, target in symlinks
    )
    return [_SymlinkLifecycleHooks(commands)]


def _collect_symlink_mounts(
    scan_dir: Path,
    container_prefix: str,
    *,
    bind_mounts: list[tuple[str, str, bool]],
    symlinks: list[tuple[str, str]],
    skip: set[str] | None = None,
) -> None:
    for child in scan_dir.iterdir():
        if not child.is_symlink():
            continue
        if skip and child.name in skip:
            continue
        target = child.resolve()
        try:
            target.relative_to(scan_dir.resolve())
        except ValueError:
            pass
        else:
            continue

        host_path = _find_mount_root(target)
        if host_path == target:
            bind_mounts.append((str(host_path), f"{container_prefix}/{child.name}", True))
        else:
            rel = target.relative_to(host_path)
            ancestor_mount = f"/workspace/_mounts/{child.name}"
            bind_mounts.append((str(host_path), ancestor_mount, True))
            symlinks.append((f"{container_prefix}/{child.name}", f"{ancestor_mount}/{rel}"))


def _find_mount_root(target: Path) -> Path:
    if not target.is_dir():
        return target
    needs_ancestor = False
    for path in target.rglob("*"):
        if path.is_symlink():
            link_target = path.parent / path.readlink()
            try:
                link_target.resolve().relative_to(target.resolve())
            except ValueError:
                needs_ancestor = True
                break
    if not needs_ancestor:
        return target
    root = target
    for path in target.rglob("*"):
        if path.is_symlink():
            resolved = (path.parent / path.readlink()).resolve()
            while not str(resolved).startswith(str(root)):
                root = root.parent
    return root
