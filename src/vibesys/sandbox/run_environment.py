"""Run environment assembly over the existing sandbox implementations.

Layering:

    loop -> _RunContext -> RunEnvironment -> ComputeBackendImpl.make_sandbox -> Sandbox

``_RunContext`` owns the experiment lifecycle: workspace/log setup, reference
and helper file materialization, model construction, git snapshots, GPU
monitoring, and agent runner wiring.  It asks this module for a run-environment
session once the workspace is ready.

``RunEnvironment`` owns run-level execution policy for a location such as local,
Docker, or Modal.  It decides path exposure, bind mounts, execution constraints,
model-weight handling, prompt-visible paths, sandbox startup, and cleanup.  It
does not execute agent commands directly.

``ComputeBackendImpl.make_sandbox`` is the compute-platform factory.  It knows
how to construct a local/Docker/Modal sandbox for CUDA, Metal, or another
compute backend.

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
from collections.abc import Callable, Mapping, Sequence  # noqa: TC003  # tracked: #288
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from vibesys.backends import SandboxKind
from vibesys.backends.base import ComputeBackendImpl  # noqa: TC001  # tracked: #288
from vibesys.constants import DEFAULT_AGENT_BACKEND, PROJECT_ROOT
from vibesys.domains.environment import EnvironmentBindMount  # noqa: TC001  # tracked: #288
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
from vibesys.input_manifest import WorkspaceSource  # noqa: TC001  # tracked: #288
from vibesys.profilers import ProfilerKind
from vibesys.prompts import PROMPTS_DIR, render_template
from vibesys.sandbox.modal_evaluator import encode_setup_command
from vibesys.skypilot.bridge import SkyPilotBridge
from vibesys.skypilot.config import load_cluster_profiles, resolve_profile
from vibesys.skypilot.runner import SkyPilotJobRunner, stable_cluster_name
from vs_project import RunEnvironmentRecord, RunResourceRequest
from vs_sandbox import BeforeReadyContext, ProjectPathPolicy, SandboxLifecycleHooks

_SHELL_COMMAND_ARG_COUNT = 3
_RunEnvironmentName = Literal["local", "docker", "modal", "skypilot"]
_RECORDED_ENVIRONMENT_NAMES: tuple[_RunEnvironmentName, ...] = (
    "local",
    "docker",
    "modal",
    "skypilot",
)
_ENVIRONMENTS_TEMPLATE_DIR = PROMPTS_DIR / "environments"
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
    # Annotation only; deepagents pulls langchain + anthropic (~seconds).
    from deepagents.backends.protocol import SandboxBackendProtocol

    from vs_project import StateNamespace


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
    host_device_reselect: bool = True
    # Coarse environment label for diagnostics and adapter selection:
    # ``"local"`` | ``"docker"`` | ``"modal"``.
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
class RunEnvironmentRequest:  # noqa: D101  # tracked: #288
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


class RunEnvironmentSession(Protocol):  # noqa: D101  # tracked: #288
    sandbox: SandboxBackendProtocol
    view: RunEnvironmentView

    def __enter__(self) -> RunEnvironmentSession: ...  # noqa: D105  # tracked: #288
    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...  # noqa: D105  # tracked: #288
    def close(self) -> None: ...  # noqa: D102  # tracked: #288


class RunEnvironment(Protocol):  # noqa: D101  # tracked: #288
    isolated: bool
    materialize_local_model_weights: bool
    default_profiler_kind: ProfilerKind
    supported_profiler_kinds: frozenset[ProfilerKind] | None
    backend_image: str | None

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession: ...  # noqa: D102  # tracked: #288
    def repair_workspace(  # noqa: D102  # tracked: #288
        self, workspace: Path, *, backend: ComputeBackendImpl, log: Callable[[str], None]
    ) -> None: ...
    def remove_workspace_child(  # noqa: D102  # tracked: #288
        self, workspace: Path, rel_path: str, *, backend: ComputeBackendImpl
    ) -> bool: ...
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
        workspace: Path,  # noqa: ARG002  # tracked: #288
        *,
        backend: ComputeBackendImpl,  # noqa: ARG002  # tracked: #288
        log: Callable[[str], None],  # noqa: ARG002  # tracked: #288
    ) -> None:
        return

    def remove_workspace_child(
        self,
        workspace: Path,  # noqa: ARG002  # tracked: #288
        rel_path: str,  # noqa: ARG002  # tracked: #288
        *,
        backend: ComputeBackendImpl,  # noqa: ARG002  # tracked: #288
    ) -> bool:
        return False

    def teardown_deployment(self, name: str, *, log: Callable[[str], None]) -> None:  # noqa: ARG002  # tracked: #288
        return

    def candidate_runtime(
        self,
        view: RunEnvironmentView,
        generation: int,  # noqa: ARG002  # tracked: #288
        child_idx: int,  # noqa: ARG002  # tracked: #288
    ) -> CandidateRuntime:
        return CandidateRuntime(view.prompt_notes, view.deployment_namespace)


def _start_sandbox(sandbox: SandboxBackendProtocol) -> None:
    """Start the container of a sandbox kind that owns one.

    ``SandboxBackendProtocol`` is the command-execution contract and says
    nothing about container lifetime, so the lookup stays dynamic. Every
    Docker- and Modal-kind sandbox this module builds implements ``start``.
    """
    start = getattr(sandbox, "start", None)
    if not callable(start):
        message = f"{type(sandbox).__name__} has no container to start"
        raise TypeError(message)
    start()


def _stop_sandbox(sandbox: SandboxBackendProtocol) -> None:
    """Stop the sandbox's container, if it owns one."""
    stop = getattr(sandbox, "stop", None)
    if callable(stop):
        stop()


@dataclass
class _DefaultRunEnvironmentSession:
    sandbox: SandboxBackendProtocol
    view: RunEnvironmentView
    stop_on_close: bool = False
    _closed: bool = False

    def __enter__(self) -> _DefaultRunEnvironmentSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.stop_on_close:
            _stop_sandbox(self.sandbox)


class LocalEnvironment(_NoopWorkspaceRecovery):  # noqa: D101  # tracked: #288
    isolated: bool = False
    materialize_local_model_weights: bool = True
    default_profiler_kind: ProfilerKind = ProfilerKind.NSYS
    supported_profiler_kinds: frozenset[ProfilerKind] | None = None
    backend_image: str | None = None

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:  # noqa: D102  # tracked: #288
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
            passthrough_paths=[],
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
class DockerEnvironmentConfig:  # noqa: D101  # tracked: #288
    image: str | None = None


class DockerEnvironment:  # noqa: D101  # tracked: #288
    isolated = True
    materialize_local_model_weights = True
    default_profiler_kind = ProfilerKind.NSYS
    supported_profiler_kinds: frozenset[ProfilerKind] | None = None

    def __init__(self, config: DockerEnvironmentConfig) -> None:  # noqa: D107  # tracked: #288
        self.config = config
        self.backend_image = config.image

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> DockerEnvironment:  # noqa: D102  # tracked: #288
        image = options.get("image")
        return cls(DockerEnvironmentConfig(image=str(image) if image else None))

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:  # noqa: D102  # tracked: #288
        tools = _evaluator_tools(request)
        container_image = (
            _resolve_docker_image_id(_docker_backend_image(request)) if tools else None
        )
        bind_mounts, docker_symlinks, passthrough = _container_mount_plan(request)
        bind_mounts.extend(
            _docker_evaluator_tool_mounts(request, tools, container_image=container_image)
        )
        extra_init_commands, cli_provider_env = _cli_container_setup(request)
        extra_init_commands.extend(
            _evaluator_container_setup(request, include_declared_tools=False)
        )
        cli_provider_env.setdefault("UV_CACHE_DIR", "/workspace/.cache/uv")
        if request.git_history_root is not None:
            cli_provider_env.setdefault("VIBESYS_GIT_HISTORY", "/opt/vibesys-history")
        bind_mounts = _dedupe_mounts(bind_mounts)
        lifecycle_hooks = _symlink_lifecycle_hooks(docker_symlinks)

        sandbox = request.backend.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(request.workspace),
            log_path=request.log_dir / "docker.log",
            bind_mounts=bind_mounts,
            passthrough_paths=passthrough,
            extra_env=cli_provider_env,
            extra_init_commands=extra_init_commands,
            lifecycle_hooks=lifecycle_hooks,
            container_image=container_image,
        )
        log: Callable[[str], None] = request.log or (lambda _: None)
        label = getattr(request.backend, "image", self.config.image or "<backend-default>")
        log(f"[docker] starting container with image {label}")
        # DOCKER-kind sandboxes always manage a container lifetime.
        _start_sandbox(sandbox)

        return _DefaultRunEnvironmentSession(
            sandbox=sandbox,
            view=RunEnvironmentView(
                paths=_isolated_paths(
                    request,
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
        except Exception as exc:  # noqa: BLE001  # tracked: #288
            log(f"[warn] chown failed for {workspace}: {exc}")

    def remove_workspace_child(  # noqa: D102  # tracked: #288
        self, workspace: Path, rel_path: str, *, backend: ComputeBackendImpl
    ) -> bool:
        target = workspace / rel_path
        try:  # noqa: SIM105  # tracked: #288
            _docker_workspace_run(
                workspace,
                backend=backend,
                shell_command=f"rm -rf -- {shlex.quote(f'/workspace/{rel_path}')}",
                timeout=120,
            )
        except Exception:  # noqa: BLE001, S110  # tracked: #288
            pass
        return not (target.exists() or target.is_symlink())

    def teardown_deployment(self, name: str, *, log: Callable[[str], None]) -> None:  # noqa: ARG002, D102  # tracked: #288
        # The editor container is torn down by the session; nothing per-candidate.
        return

    def candidate_runtime(  # noqa: D102  # tracked: #288
        self,
        view: RunEnvironmentView,
        generation: int,  # noqa: ARG002  # tracked: #288
        child_idx: int,  # noqa: ARG002  # tracked: #288
    ) -> CandidateRuntime:
        return CandidateRuntime(view.prompt_notes, view.deployment_namespace)


@dataclass(frozen=True)
class ModalEnvironmentConfig:  # noqa: D101  # tracked: #288
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
    sandbox: SandboxBackendProtocol
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
            raise ValueError("SkyPilot requires a non-empty cluster profile")  # noqa: TRY003
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
        """Open the bridge and CPU-only local editor container."""
        if self.config.resources is None:
            raise ValueError("SkyPilot requires portable run resources")  # noqa: TRY003
        if request.state_namespace is None:
            raise ValueError("SkyPilot requires a machine-local state namespace")  # noqa: TRY003
        profiles = load_cluster_profiles(self.config.profiles_file)
        resources = resolve_profile(profiles, self.config.profile, self.config.resources)
        cluster_name = stable_cluster_name(request.run_id, resources)
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
            resources=resources,
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
            bind_mounts, docker_symlinks, passthrough = _container_mount_plan(request)
            extra_init_commands, cli_provider_env = _cli_container_setup(request)
            cli_provider_env.setdefault("UV_CACHE_DIR", "/workspace/.cache/uv")
            helper_source = Path(__file__).with_name("skypilot_evaluator.py")
            helper_path = "/opt/vibesys-skypilot-evaluator.py"
            socket_path = "/opt/vibesys-skypilot/bridge.sock"
            caller_state_path = "/opt/vibesys-skypilot/caller-state"
            caller_state = request.state_namespace.external_directory("caller")
            cli_provider_env["VIBESYS_SKYPILOT_CALLER_STATE"] = caller_state_path
            bind_mounts.extend(
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
                    nodes=resources.nodes,
                    accelerators_per_node=resources.accelerators_per_node,
                    accelerator_type=resources.accelerator_type,
                    profile_name=resources.profile_name,
                )
            )
            runtime_path = "/opt/vibesys-runtime/environment.md"
            bind_mounts.append((str(runtime_document), runtime_path, True))
            passthrough.extend(["/opt/vibesys-runtime", "/opt/vibesys-skypilot"])
            sandbox = request.backend.make_sandbox(
                SandboxKind.DOCKER,
                host_workspace=str(request.workspace),
                log_path=request.log_dir / "docker.log",
                bind_mounts=_dedupe_mounts(bind_mounts),
                passthrough_paths=passthrough,
                extra_env=cli_provider_env,
                extra_init_commands=extra_init_commands,
                lifecycle_hooks=_symlink_lifecycle_hooks(docker_symlinks),
                attach_accelerator=False,
            )
            _start_sandbox(sandbox)
        except Exception:
            bridge.close()
            raise

        prefix = f"python {helper_path} --socket {socket_path}"
        return _SkyPilotRunEnvironmentSession(
            sandbox=sandbox,
            bridge=bridge,
            view=RunEnvironmentView(
                paths=AgentPaths(
                    objective=(
                        "/opt/vibesys-runtime/objective.md"
                        if request.objective is not None
                        else "OBJECTIVE.md"
                    ),
                    accuracy_command=(f"{prefix} accuracy" if "accuracy" in commands else None),
                    benchmark_command=(f"{prefix} benchmark" if "benchmark" in commands else None),
                    profiler_support=None,
                ),
                prompt_notes=render_template(
                    "skypilot/prompt_notes.j2",
                    template_dir=_ENVIRONMENTS_TEMPLATE_DIR,
                    runtime_container_path=runtime_path,
                ),
                isolated=True,
                cli_sandboxed=True,
                host_device_reselect=False,
                env_kind="skypilot",
                profile_execution="remote",
                supports_parallel_candidate_evaluation=False,
            ),
        )


class ModalEnvironment(_NoopWorkspaceRecovery):  # noqa: D101  # tracked: #288
    isolated = True
    materialize_local_model_weights = False
    default_profiler_kind = ProfilerKind.TORCH
    supported_profiler_kinds: frozenset[ProfilerKind] | None = frozenset(
        {ProfilerKind.AUTO, ProfilerKind.TORCH, ProfilerKind.NONE}
    )

    def __init__(self, config: ModalEnvironmentConfig) -> None:  # noqa: D107  # tracked: #288
        self.config = config
        self.model_volume: str | None = config.model_volume
        self.backend_image = config.image

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> ModalEnvironment:  # noqa: D102  # tracked: #288
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
        entrypoint; we install the Modal
        Python SDK and mount the host's ``~/.modal.toml`` into the container
        so those calls authenticate.

        We retain the host-side Modal Volume bootstrap (model + optional
        draft) so the implementer's ``modal.Volume.from_name(...)`` calls
        resolve.  The previous "long-lived Modal sandbox running codex
        inside" architecture is gone — it caused HOME-leak auth bugs,
        codex-vs-model-weight memory contention, and per-run sandbox
        cold-start overhead that this design eliminates.
        """
        # Host-side: ensure Modal Volumes exist for the model + optional
        # draft.  These run before the Docker container starts and are
        # idempotent (skip-if-ready sentinel).
        self._ensure_model_volume(request)
        self._ensure_draft_volume(request)

        bind_mounts, docker_symlinks, passthrough = _container_mount_plan(request)
        extra_init_commands, cli_provider_env = _cli_container_setup(request)
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
        bind_mounts.append((str(runtime_document), runtime_container_path, True))
        passthrough.append("/opt/vibesys-runtime")
        evaluator_helper = request.framework_root / "src/vibesys/sandbox/modal_evaluator.py"
        evaluator_container_path = "/opt/vibesys-modal-evaluator.py"
        bind_mounts.append((str(evaluator_helper), evaluator_container_path, True))

        # Mount host Modal auth so `modal run` inside the container
        # authenticates as the host user.
        modal_auth = Path.home() / ".modal.toml"
        if modal_auth.exists():
            bind_mounts.append((str(modal_auth), "/root/.modal.toml", True))
        modal_config_dir = Path.home() / ".modal"
        if modal_config_dir.is_dir():
            bind_mounts.append((str(modal_config_dir), "/root/.modal", True))

        # Install the Modal Python SDK alongside the agent's other packages.
        # Pinned to a recent release; the wire protocol is forward-compatible
        # with the host's Modal CLI as long as both are within ~one major.
        extra_init_commands.insert(0, "pip install --quiet 'modal>=0.66'")

        bind_mounts = _dedupe_mounts(bind_mounts)
        lifecycle_hooks = _symlink_lifecycle_hooks(docker_symlinks)

        sandbox = request.backend.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(request.workspace),
            log_path=request.log_dir / "docker.log",
            bind_mounts=bind_mounts,
            passthrough_paths=passthrough,
            extra_env=cli_provider_env,
            extra_init_commands=extra_init_commands,
            lifecycle_hooks=lifecycle_hooks,
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
        return _DefaultRunEnvironmentSession(
            sandbox=sandbox,
            view=RunEnvironmentView(
                paths=AgentPaths(
                    objective=(
                        "/opt/vibesys-runtime/objective.md"
                        if request.objective is not None
                        else "OBJECTIVE.md"
                    ),
                    accuracy_command=_prefix_command(
                        evaluator_prefix,
                        _environment_command(
                            request,
                            request.accuracy_command,
                            isolated=True,
                            evaluator_package_root=".vibesys-evaluator-package",
                            evaluator_tools_root=_REMOTE_EVALUATOR_TOOLS_ROOT,
                        ),
                    ),
                    benchmark_command=_prefix_command(
                        evaluator_prefix,
                        _environment_command(
                            request,
                            request.benchmark_command,
                            isolated=True,
                            evaluator_package_root=".vibesys-evaluator-package",
                            evaluator_tools_root=_REMOTE_EVALUATOR_TOOLS_ROOT,
                        ),
                    ),
                    profiler_support=(
                        request.profiler_support_name if request.profiler_support_path else None
                    ),
                ),
                prompt_notes=render_template(
                    "modal/prompt_notes.j2",
                    template_dir=_ENVIRONMENTS_TEMPLATE_DIR,
                    runtime_container_path=runtime_container_path,
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
        from vs_sandbox import ensure_model_volume  # noqa: PLC0415  # tracked: #288

        meta = json.loads(meta_path.read_text())
        model_id = meta.get("model_id")
        if not model_id:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"meta.json at {meta_path} missing required 'model_id' field "
                "(needed for Modal auto-upload)"
            )
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
        from vs_sandbox import ensure_model_volume  # noqa: PLC0415  # tracked: #288

        draft_meta = json.loads(draft_meta_path.read_text())
        draft_model_id = draft_meta.get("model_id")
        if not draft_model_id:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"draft_meta.json at {draft_meta_path} missing required 'model_id' field"
            )
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
            result = subprocess.run(  # noqa: S603  # tracked: #288
                [sys.executable, "-m", "modal", "app", "stop", name, "--yes"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:  # timeout, missing binary, etc.  # noqa: BLE001  # tracked: #288
            log(f"[warn] modal app stop {name} raised: {exc}")
            return
        if result.returncode != 0:
            log(
                f"[warn] modal app stop {name} failed "
                f"(exit {result.returncode}): {result.stderr.strip()[:200]}"
            )
        else:
            log(f"[modal] stopped candidate app {name}")

    def candidate_runtime(  # noqa: D102  # tracked: #288
        self, view: RunEnvironmentView, generation: int, child_idx: int
    ) -> CandidateRuntime:
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


def build_run_environment(spec: RunEnvironmentSpec) -> RunEnvironment:  # noqa: D103  # tracked: #288
    if spec.name == "local":
        return LocalEnvironment()
    if spec.name == "docker":
        return DockerEnvironment.from_options(spec.options)
    if spec.name == "modal":
        return ModalEnvironment.from_options(spec.options)
    if spec.name == "skypilot":
        return SkyPilotEnvironment.from_options(spec.options, spec.resources)
    raise ValueError(f"unknown run environment: {spec.name!r}")  # noqa: TRY003  # tracked: #288


def make_run_environment_spec(  # noqa: PLR0913  # tracked: #288
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
        raise ValueError(  # noqa: TRY003
            "--docker, --modal, and --skypilot are mutually exclusive"
        )
    if use_skypilot:
        if not cluster_profile:
            raise ValueError("--skypilot requires --cluster-profile")  # noqa: TRY003
        if resources is None:
            raise ValueError("--skypilot requires input [resources]")  # noqa: TRY003
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
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"effective objective must be inside the project workspace: {path}"
            ) from exc
        if not path.is_file() or path.read_text() != request.objective:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"effective objective does not match its committed document: {path}"
            )
        return path
    path = request.log_dir / "effective-objective.md"
    path.write_text(request.objective)
    return path


def _isolated_paths(
    request: RunEnvironmentRequest,
    *,
    evaluator_tools_root: Path | None = None,
) -> AgentPaths:
    return AgentPaths(
        objective=(
            "/opt/vibesys-runtime/objective.md" if request.objective is not None else "OBJECTIVE.md"
        ),
        accuracy_command=_environment_command(
            request,
            request.accuracy_command,
            isolated=True,
            evaluator_tools_root=evaluator_tools_root,
        ),
        benchmark_command=_environment_command(
            request,
            request.benchmark_command,
            isolated=True,
            evaluator_tools_root=evaluator_tools_root,
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
    isolated: bool = False,
    evaluator_package_root: str | None = None,
    evaluator_tools_root: Path | None = None,
) -> str | None:
    """Translate semantic paths in argv, then quote the translated command."""
    if command is None:
        return None
    try:
        arguments = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"invalid evaluator command: {exc}") from exc  # noqa: TRY003
    project_root = "/workspace" if isolated else str(request.workspace)
    replacements = [
        (PROJECT_ROOT_TOKEN, project_root),
        (PYTHON_TOKEN, "python3" if isolated else sys.executable),
    ]
    if request.evaluator_package_root is not None:
        replacements.append(
            (
                str(request.evaluator_package_root),
                (
                    evaluator_package_root
                    or (
                        "/opt/vibesys-evaluator-package"
                        if isolated
                        else str(request.evaluator_package_root)
                    )
                ),
            )
        )
    tools = _evaluator_tools(request)
    if tools:
        tools_root = evaluator_tools_root or _required_evaluator_tools_root(request)
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
        isolated=True,
        evaluator_tools_root=_REMOTE_EVALUATOR_TOOLS_ROOT,
    )
    if rendered is None:
        return None
    arguments = shlex.split(rendered)
    replacements = [
        ("/opt/vibesys-evaluator-package", ".vibesys-evaluator-package"),
        ("/workspace", "."),
    ]
    return tuple(_translate_command_argument(argument, replacements) for argument in arguments)


def _translate_command_argument(
    argument: str,
    replacements: list[tuple[str, str]],
) -> str:
    """Translate paths in one argv item, including serialized nested argv."""
    if not any(source in argument for source, _ in replacements):
        return argument
    try:
        nested = json.loads(argument)
    except json.JSONDecodeError:
        nested = None
    if isinstance(nested, list) and all(isinstance(item, str) for item in nested):
        _reject_semantic_tokens_in_source(nested, replacements)
        translated: list[str] = []
        for item in nested:
            translated_item = item
            for source, destination in replacements:
                translated_item = translated_item.replace(source, destination)
            translated.append(translated_item)
        return json.dumps(translated, separators=(",", ":"))
    for source, destination in replacements:
        argument = argument.replace(source, destination)
    return argument


def _reject_semantic_tokens_in_source(
    arguments: list[str],
    replacements: list[tuple[str, str]],
) -> None:
    """Reject raw path substitution into shell or interpreter source code."""
    source_index = _executable_source_index(arguments)
    if source_index is not None and any(
        source in arguments[source_index] for source, _ in replacements
    ):
        raise ValueError(  # noqa: TRY003
            "semantic path tokens in executable source are unsafe; pass them as "
            "positional arguments after the source"
        )


def _executable_source_index(arguments: list[str]) -> int | None:
    """Return the source-code argv index for supported command interpreters."""
    shell_source_index = _nested_shell_source_index(arguments)
    if shell_source_index is not None:
        return shell_source_index
    command_index, split_string_index = _env_wrapped_command_indexes(arguments)
    if split_string_index is not None or command_index is None:
        return split_string_index
    executable = Path(arguments[command_index]).name
    if executable in {"node", "nodejs"}:
        return _option_value_index(
            arguments,
            command_index,
            separate={"-e", "-p", "--eval", "--print"},
            inline_prefixes=("--eval=", "--print="),
        )
    if _is_python_executable(executable):
        return _option_value_index(arguments, command_index, separate={"-c"})
    return None


def _option_value_index(
    arguments: list[str],
    command_index: int,
    *,
    separate: set[str],
    inline_prefixes: tuple[str, ...] = (),
) -> int | None:
    for index, argument in enumerate(arguments[command_index + 1 :], start=command_index + 1):
        if argument in separate:
            source_index = index + 1
            return source_index if source_index < len(arguments) else None
        if inline_prefixes and argument.startswith(inline_prefixes):
            return index
    return None


def _is_python_executable(executable: str) -> bool:
    suffix = executable.removeprefix("python")
    return executable.startswith("python") and (
        not suffix or all(part.isdigit() for part in suffix.split("."))
    )


def _nested_shell_source_index(arguments: list[str]) -> int | None:
    """Return the script index for common ``sh`` and ``bash`` command forms."""
    command_index, split_string_index = _env_wrapped_command_indexes(arguments)
    if split_string_index is not None:
        return split_string_index
    if command_index is None:
        return None
    arguments = arguments[command_index:]
    if len(arguments) < _SHELL_COMMAND_ARG_COUNT or arguments[0] not in {
        "bash",
        "sh",
        "/bin/bash",
        "/bin/sh",
        "/usr/bin/bash",
        "/usr/bin/sh",
    }:
        return None
    for index, argument in enumerate(arguments[1:], start=1):
        if argument == "--":
            return None
        if argument == "-c" or (
            argument.startswith("-") and not argument.startswith("--") and "c" in argument[1:]
        ):
            source_index = index + 1
            return command_index + source_index if source_index < len(arguments) else None
    return None


def _env_wrapped_command_indexes(arguments: list[str]) -> tuple[int | None, int | None]:
    """Return command and shell-like split-string indexes for an ``env`` wrapper."""
    if not arguments or arguments[0] not in {"env", "/bin/env", "/usr/bin/env"}:
        return 0, None
    index = 1
    while index < len(arguments):
        action, width = _classify_env_argument(arguments[index])
        if action == "command":
            return index, None
        if action == "end-options":
            index += width
            return (index if index < len(arguments) else None), None
        if index + width > len(arguments):
            return None, None
        if action == "split-string":
            return None, index + width - 1
        index += width
    return None, None


def _classify_env_argument(argument: str) -> tuple[str, int]:
    """Classify one GNU ``env`` wrapper argument and its argv width."""
    action = "command"
    width = 0
    if argument == "--":
        action, width = "end-options", 1
    elif argument in {"-S", "--split-string"}:
        action, width = "split-string", 2
    elif argument.startswith("--split-string=") or (argument.startswith("-S") and argument != "-S"):
        action, width = "split-string", 1
    elif argument in {"-C", "-u", "--argv0", "--chdir", "--unset"}:
        action, width = "option", 2
    elif argument.startswith("-"):
        action, width = "option", 1
    elif "=" in argument and argument.partition("=")[0]:
        action, width = "assignment", 1
    return action, width


def _docker_workspace_run(
    workspace: Path,
    *,
    backend: ComputeBackendImpl,
    shell_command: str,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    image = getattr(backend, "image", "ubuntu:latest")
    return subprocess.run(  # noqa: S603  # tracked: #288
        [  # noqa: S607  # tracked: #288
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


def _container_mount_plan(  # noqa: C901  # tracked: #288
    request: RunEnvironmentRequest,
    *,
    include_cli_provider_mounts: bool = True,
) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str]], list[str]]:
    """Build the bind mounts + setup symlinks for a sandbox.

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

    passthrough_paths: list[str] = []
    objective_document = _materialize_effective_objective(request)
    if objective_document is not None:
        bind_mounts.append((str(objective_document), "/opt/vibesys-runtime/objective.md", True))
        passthrough_paths.append("/opt/vibesys-runtime")
    if request.git_history_root is not None:
        bind_mounts.append((str(request.git_history_root), "/opt/vibesys-history", True))
        passthrough_paths.append("/opt/vibesys-history")
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
        if mount.container_path.startswith("/"):
            passthrough_paths.append(mount.container_path)

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
        and (request.agent_backend or DEFAULT_AGENT_BACKEND) == "cli"
        and request.cli_provider
    ):
        from vibesys.agents.cli_docker import auth_bind_mounts  # noqa: PLC0415  # tracked: #288

        bind_mounts.extend(auth_bind_mounts(request.cli_provider))
        bind_mounts.append((str(request.framework_root), "/opt/vibesys", True))

    return bind_mounts, symlinks, passthrough_paths


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


def _cli_container_setup(
    request: RunEnvironmentRequest,
) -> tuple[list[str], dict[str, str]]:
    effective_agent = request.agent_backend or DEFAULT_AGENT_BACKEND
    if effective_agent != "cli" or not request.cli_provider:
        return [], {}
    from vibesys.agents.cli_docker import (  # noqa: PLC0415  # tracked: #288
        DOCKER_AUTH_ENV_VARS,
        DOCKER_AUTH_PATHS,
        DOCKER_PROVIDER_ENV,
        auth_copy_commands,
        auth_env_passthrough,
        docker_init_commands,
    )

    provider = request.cli_provider
    auth_commands = auth_copy_commands(provider)
    auth_env = auth_env_passthrough(provider)
    if not auth_commands and not auth_env:
        checked_files = (
            ", ".join(str(spec.host_path) for spec in DOCKER_AUTH_PATHS.get(provider, []))
            or "<none registered>"
        )
        checked_env = ", ".join(DOCKER_AUTH_ENV_VARS.get(provider, ())) or "<none registered>"
        raise ValueError(  # noqa: TRY003  # tracked: #288
            f"no {provider!r} CLI authentication is available for the container: "
            f"none of the host files exist ({checked_files}) and none of the "
            f"environment variables are set ({checked_env}). Authenticate the "
            f"{provider} CLI on this host, or export one of those variables, "
            "before running in an isolated environment."
        )
    env = dict(DOCKER_PROVIDER_ENV.get(provider, {}))
    # Container processes inherit only what ``docker run -e`` sets; the editor
    # container has no other view of the host environment.
    env.update(auth_env)
    commands = [*auth_commands, *docker_init_commands(provider)]
    return commands, env


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
            f"{_REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/go.tgz" if rootless else "/tmp/vibesys-go.tgz"  # noqa: S108  # isolated setup container
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
            else "/tmp/vibesys-rustup-init"  # noqa: S108  # isolated setup container
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
                passthrough_paths=[str(_SANDBOX_EVALUATOR_TOOLS_ROOT)],
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
                    raise EvaluatorToolError(  # noqa: TRY003
                        "Docker evaluator tool builder could not return cache ownership "
                        f"to the host user: {detail[:500]}"
                    )
            finally:
                _stop_sandbox(builder)
        try:
            prepare_evaluator_tools(tools, host_parent, command_runner=require_builder)
        except _EvaluatorToolBuildRequiredError as exc:
            raise EvaluatorToolError(  # noqa: TRY003
                "Docker evaluator tool builder did not publish every declared tool"
            ) from exc

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
    image = getattr(request.backend, "image", None)
    if not isinstance(image, str) or not image:
        raise EvaluatorToolError("Docker evaluator tools require a configured backend image")  # noqa: TRY003
    return image


def _inspect_docker_image_id(image: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["docker", "image", "inspect", "--format={{.Id}}", image],  # noqa: S607
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
        pull = subprocess.run(  # noqa: S603
            ["docker", "image", "pull", image],  # noqa: S607
            capture_output=True,
            check=False,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise EvaluatorToolError(  # noqa: TRY003
            "Docker was not found while resolving the evaluator image"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise EvaluatorToolError(f"Docker image pull timed out: {image}") from exc  # noqa: TRY003
    if pull.returncode != 0:
        detail = (pull.stderr or pull.stdout or "docker image pull failed").strip()[:500]
        raise EvaluatorToolError(f"Could not resolve Docker image {image!r}: {detail}")  # noqa: TRY003
    if identity := _inspect_docker_image_id(image):
        return identity
    raise EvaluatorToolError(  # noqa: TRY003
        f"Docker image {image!r} has no resolvable immutable image ID after pull"
    )


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


def _required_evaluator_tools_root(request: RunEnvironmentRequest) -> Path:
    if request.evaluator_tools_root is None:
        raise ValueError("evaluator tools require an operator-owned tools root")  # noqa: TRY003
    root = request.evaluator_tools_root.resolve()
    try:
        root.relative_to(request.workspace.resolve())
    except ValueError:
        return root
    raise ValueError("evaluator tools root must be outside the candidate workspace")  # noqa: TRY003


def _dedupe_mounts(
    mounts: list[tuple[str, str, bool]],
) -> list[tuple[str, str, bool]]:
    seen: dict[str, tuple[str, str, bool]] = {}
    for host_path, container_path, readonly in mounts:
        seen[container_path] = (host_path, container_path, readonly)
    return list(seen.values())


@dataclass(frozen=True)
class _SymlinkLifecycleHooks(SandboxLifecycleHooks):
    commands: tuple[str, ...]

    def before_ready(self, context: BeforeReadyContext) -> None:
        for command in self.commands:
            result = context.sandbox.execute(command)
            if result.exit_code != 0:
                raise RuntimeError(  # noqa: TRY003
                    f"failed to create sandbox symlink with {command!r}: {result.output}"
                )
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
