"""The SkyPilot run environment: a CPU-only Docker editor and the host bridge to the cluster."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vs_runtime._agent_tool_caches import agent_tool_cache_env
from vs_runtime._container_mount_plan import (
    container_mount_plan,
    materialize_effective_objective,
)
from vs_runtime._container_runtime_policy import (
    RefusesDockerInDocker,
    reject_docker_in_docker,
)
from vs_runtime._image_features import image_features
from vs_runtime._run_environment import (
    AgentPaths,
    DockerEnvironment,
    DockerEnvironmentConfig,
    RunEnvironmentPresentation,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    RunEnvironmentView,
    _build_and_push_agent_image,
    _cli_provider_env_and_auth_files,
    _noop_log,
    _prepare_evaluation_plan,
    _PreparedRunEnvironment,
    _required_runtime_document,
    _starting_container,
    _write_runtime_document,
)
from vs_runtime._run_environment_facts import (
    SkyPilotEnvironmentFacts,
)
from vs_runtime._skypilot_step_work_reaper import SkyPilotStepWorkReaper
from vs_runtime._trusted_evaluation_preparation import (
    REMOTE_EVALUATOR_GO_MODULES_ROOT,
    REMOTE_EVALUATOR_TOOLS_ROOT,
    TrustedEvaluationCommandPaths,
    evaluator_agent_toolchains,
    remote_evaluator_setup_command,
)
from vs_runtime._writer_reconciliation import (
    UnsupportedWriterReconciler,
)
from vs_sandbox.api import (
    SandboxKind,
    deduplicate_host_resources,
    evaluator_helpers,
    host_resource_for_mount,
    start_sandbox,
    stop_sandbox,
)
from vs_sandbox.api.skypilot import (
    InvocationJournal,
    ResolvedSkyPilotResources,
    SkyPilotBridge,
    SkyPilotJobRunner,
    load_cluster_profiles,
    resolve_profile,
    stable_cluster_name,
)
from vs_sandbox.api.symlink_mounts import (
    symlink_lifecycle_hooks,
)
from vs_sim.api import (
    CloseOnce,
    ReleaseStack,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_project.api import RunResourceRequest, StateNamespace
    from vs_proto.api import replay as writer_pb
    from vs_runtime._environment_system import EnvironmentSystem
    from vs_runtime._local_job_step_runner import StageWorkReaper
    from vs_runtime._run_environment import _AgentPathSandbox
    from vs_sandbox.api import CaptureCommandRunner, CommandRunner
    from vs_sim.api import BlockingRunner


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
    sandbox: CommandRunner
    view: RunEnvironmentView
    bridge: SkyPilotBridge
    _close: CloseOnce = field(init=False, default_factory=CloseOnce)
    _steps: ReleaseStack = field(init=False, default_factory=ReleaseStack)

    def __post_init__(self) -> None:
        self._steps.callback(self.bridge.close)
        self._steps.callback(stop_sandbox, self.sandbox)

    @property
    def capture_commands(self) -> CaptureCommandRunner:
        """Run capture in the editor for environments without a separate role."""
        return self.sandbox

    def __enter__(self) -> _SkyPilotRunEnvironmentSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop the sandbox and the bridge; a step that failed runs again on the next call."""
        self._close.run(self._steps.close)


class SkyPilotEnvironment(RefusesDockerInDocker, DockerEnvironment):
    """CPU-only Docker editor with host-mediated SkyPilot evaluation."""

    config: SkyPilotEnvironmentConfig
    requires_local_profiler_preflight = True
    materialize_local_model_weights = False
    default_profiler_id = "none"
    supported_profiler_ids: frozenset[str] | None = frozenset({"auto", "none"})

    def reconcile_writers(
        self,
        manifest: writer_pb.WriterManifest,
        *,
        system: EnvironmentSystem,
        log: Callable[[str], None],
    ) -> writer_pb.WriterReconciliation:
        del system, log
        return UnsupportedWriterReconciler("skypilot: remote writer jobs").reconcile_writers(
            manifest
        )

    def _docker_settings(self) -> DockerEnvironmentConfig:
        """The editor container's Docker client is the host's own: SkyPilot's config names none."""
        return DockerEnvironmentConfig()

    def agent_work_reaper(
        self,
        run_id: str,
        *,
        system: EnvironmentSystem,
        blocking: BlockingRunner,
        local_namespace: Callable[[str], StateNamespace],
        log: Callable[[str], None],
    ) -> StageWorkReaper:
        """End one step's agent processes in the editor container, then cancel its cluster jobs.

        The cluster is named per run and its jobs are keyed by invocation, so the jobs of one
        step are found through the bridge's journal by the step's tag, never by the cluster.
        """
        agents = super().agent_work_reaper(
            run_id, system=system, blocking=blocking, local_namespace=local_namespace, log=log
        )
        return SkyPilotStepWorkReaper(
            agents,
            InvocationJournal(local_namespace("skypilot")),
            SkyPilotJobRunner(executable=self.config.executable),
            blocking,
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

    def attaches_backend_accelerator(self, *, docker_in_docker: bool) -> bool:
        """The local editor is CPU-only; the cluster job holds the accelerator."""
        del docker_in_docker
        return False

    def prepare(self, request: RunEnvironmentRequest) -> _PreparedRunEnvironment:
        """Resolve and validate SkyPilot resources without starting them."""
        reject_docker_in_docker(docker_in_docker=request.docker_in_docker, environment="skypilot")
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
                runtime_go_module_cache_root=(
                    str(REMOTE_EVALUATOR_GO_MODULES_ROOT)
                    if requirements.go_module is not None
                    else None
                ),
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

            container_image = _build_and_push_agent_image(
                request,
                image_features(evaluator_agent_toolchains(requirements)),
                backend_label="SkyPilot",
            )

            plan = container_mount_plan(request)
            resources, docker_symlinks = plan.resources, plan.symlinks
            cli_provider_env, auth_files = _cli_provider_env_and_auth_files(request)
            for name, value in agent_tool_cache_env().items():
                cli_provider_env.setdefault(name, value)
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
            runtime_document = _write_runtime_document(request.log_dir, runtime_presentation)
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
                run_id=request.run_id,
            )
            with _starting_container(request, sandbox) as output:
                start_sandbox(sandbox, output=output)
        except Exception:
            bridge.close()
            raise

        agent_path_sandbox = cast("_AgentPathSandbox", sandbox)
        objective_document = materialize_effective_objective(request)
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
                workspace_git_writable=plan.agent_git_writable,
            ),
        )
