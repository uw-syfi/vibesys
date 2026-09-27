"""Shared lifecycle context for one canonical VibeSys project run."""

import shutil
import time
from collections.abc import Callable
from contextlib import ExitStack
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import TextIO, overload

from vibesys.composition import (
    _vibesys_runtime_host_resource,
    agent_spec_from_config,
    prepare_domain_model_artifacts,
    resolve_agent_driver,
)
from vibesys.config import BUNDLED_RESOURCES, as_config
from vibesys.constants import (
    PROJECT_ROOT,
    ComputeBackend,
    DomainName,
)
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import (
    CoreEventType,
    ExperimentsChangedData,
)
from vibesys.inputs import InputBundle
from vibesys.orchestration.profilers import (
    ACTIVE_PROFILER_KINDS,
    PROFILERS_COMMON_STAGED_NAME,
    ProfilerDefinition,
    ProfilerKind,
    ProfilerPreflightResult,
    default_profiler_for_backend,
    preflight_profiler_kind,
    profiler_definition,
    resolve_profiler_kind,
)
from vibesys.run import (
    ExperimentRepository,
    ProjectProvisioningSpec,
    provision_project,
)
from vibesys.run.contracts import RunRequest
from vibesys.run.environment import open_run_environment
from vibesys.run.git_events import CoreGitTrackerEvents
from vibesys.run.integration import LocalRunIntegration, RunResources
from vibesys.run.project_policy import (
    build_project_path_policy,
    trusted_project_input_paths,
)
from vibesys.run.skills import platform_skill_excluded_paths
from vibesys.run.workspace_policy import (
    build_workspace_materialization_plan,
    create_project_materializer,
)
from vs_agent.api import (
    AgentBackend,
    AgentEventSink,
    agent_driver_supports_tool_servers,
    task_agent_host_resources,
)
from vs_project.api import (
    GitTracker,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    Project,
    RunExecutionRecord,
    RunLogger,
    generate_run_id,
)
from vs_runtime.api import OrchestrationResumeDecision, boot_trace
from vs_runtime.api.infrastructure import (
    ModelArtifactRequest,
    MultiSlotRoundTransactionCoordinator,
    NativeCpuProfilerKind,
    ProjectRunBaselineMissingError,
    ProjectRunDirtyResumeError,
    ProjectRunEffects,
    ProjectRunMismatchError,
    ProjectRunMismatchKind,
    ProjectRunRequest,
    ProjectRunResources,
    ProjectStateDeclaration,
    ProtocolBenchmarkContract,
    RunEnvironmentRequest,
    RunEnvironmentResources,
    RunEnvironmentSession,
    RunEnvironmentSpec,
    RunEnvironmentView,
    RunState,
    ScalarBenchmarkContract,
    TrustedEvaluationPlan,
    build_run_environment,
    make_run_environment_spec,
    open_project_run_resources,
    open_run_environment_resources,
    preflight_native_cpu_profiler,
    prepare_trusted_evaluator,
    run_environment_record,
)
from vs_sandbox.api import (
    ComputeBackendImpl,
    DeviceLease,
    HostResource,
    create_compute_backend,
)

_RUNTIME_STATE_NAMESPACE = "runtime"
_SKYPILOT_STATE_NAMESPACE = "skypilot"


def _run_log_emitter(events: AgentEventSink) -> Callable[[str, TextIO], None]:
    """Write run-log text and publish the same diagnostic on its run stream."""

    def emit(text: str, log_file: TextIO) -> None:
        events.agent_output(text + "\n", channel="diagnostic")
        log_file.write(text + "\n")
        log_file.flush()

    return emit


def _trusted_evaluation_plan(
    bundle: InputBundle,
    session: RunEnvironmentSession,
) -> TrustedEvaluationPlan:
    """Lower task and environment configuration into runtime execution facts."""
    scalar = bundle.benchmark_result
    contract = (
        ScalarBenchmarkContract(
            output_argument=scalar.json_argument,
            metric=scalar.metric,
        )
        if scalar is not None
        else (ProtocolBenchmarkContract() if bundle.benchmark_result_protocol is not None else None)
    )
    return TrustedEvaluationPlan(
        accuracy_command=session.view.paths.accuracy_command,
        accuracy_timeout_seconds=bundle.manifest.accuracy.timeout_seconds,
        benchmark_command=session.view.paths.benchmark_command,
        benchmark_timeout_seconds=bundle.manifest.benchmark.timeout_seconds,
        framework_setup_timeout_seconds=session.view.framework_setup_timeout_seconds,
        benchmark_contract=contract,
    )


_StateBinding = ProjectStateDeclaration


def _profiler_support_extra(definition: ProfilerDefinition) -> tuple[tuple[str, str], ...]:
    """Directories staged as siblings of an active profiler's support dir.

    Always includes the shared ``capture_runtime`` support package (staged
    as ``profilers_common``), plus each of the definition's declared
    ``extra_support_kinds`` (staged under their own ``support_name``, e.g.
    ``torch_profiler`` alongside ``rocprof_profiler``).
    """
    extra: list[tuple[str, str]] = []
    common_dir = BUNDLED_RESOURCES.directory("profilers", "_common")
    if common_dir is not None:
        extra.append((str(common_dir), PROFILERS_COMMON_STAGED_NAME))
    for extra_kind in sorted(definition.extra_support_kinds):
        extra_definition = profiler_definition(extra_kind)
        extra_dir = BUNDLED_RESOURCES.directory("profilers", extra_kind.value)
        if extra_dir is not None:
            extra.append((str(extra_dir), extra_definition.support_name))
    return tuple(extra)


def _native_profiler_preflight(kind: ProfilerKind) -> ProfilerPreflightResult:
    """Adapt runtime-native host checks into profiler policy results."""
    native_kind = {
        ProfilerKind.LINUX_CPU: NativeCpuProfilerKind.LINUX,
        ProfilerKind.MACOS_CPU: NativeCpuProfilerKind.MACOS,
    }[kind]
    capability = preflight_native_cpu_profiler(native_kind)
    return ProfilerPreflightResult(
        kind,
        capability.usable,
        capability.diagnostics,
        capability.details,
    )


def _coerce_dir(raw: str | Path | None, label: str) -> Path | None:
    if raw is None:
        return None
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        message = f"{label} path does not exist: {raw}"
        raise ValueError(message)
    if not p.is_dir():
        message = f"{label} path is not a directory: {raw}"
        raise ValueError(message)
    return p


def _installed_vibesys_version() -> str:
    """Return the installed distribution version for portable run metadata."""
    try:
        return distribution_version("vibesys")
    except PackageNotFoundError:
        return "0+unknown"


def _resume_orchestration_decision(
    recorded: OrchestrationRunManifest,
    requested: OrchestrationDescriptor,
    environment: RunEnvironmentSpec,
    execution: RunExecutionRecord,
    resume_policy: Callable[
        [OrchestrationDescriptor, OrchestrationDescriptor], OrchestrationResumeDecision
    ],
) -> OrchestrationResumeDecision:
    """Check the current manifest identity and delegate option policy to its owner."""
    if recorded.run_environment != run_environment_record(environment):
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message="resuming a run cannot change its recorded run_environment",
            )
        )
    if recorded.execution != execution:
        changed = ", ".join(
            name
            for name in RunExecutionRecord.model_fields
            if getattr(recorded.execution, name) != getattr(execution, name)
        )
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=f"resuming a run cannot change its recorded execution fields: {changed}",
            )
        )
    if (recorded.orchestration.id, recorded.orchestration.config_version) != (
        requested.id,
        requested.config_version,
    ):
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=(
                    f"run uses orchestration {recorded.orchestration.id!r} version "
                    f"{recorded.orchestration.config_version}, not {requested.id!r} "
                    f"version {requested.config_version}"
                ),
            )
        )
    return resume_policy(recorded.orchestration, requested)


@overload
def _coerce_dir_path(raw: str, label: str) -> str: ...


@overload
def _coerce_dir_path(raw: None, label: str) -> None: ...


def _coerce_dir_path(raw: str | None, label: str) -> str | None:
    path = _coerce_dir(raw, label)
    return str(path) if path is not None else None


def _coerce_skills_dirs(raw_dirs: list[str] | None) -> list[Path]:
    if not raw_dirs:
        return []
    result: list[Path] = []
    for raw in raw_dirs:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p = p.resolve()
        if not p.exists():
            message = f"--skills-dir path does not exist: {raw}"
            raise ValueError(message)
        if not p.is_dir():
            message = f"--skills-dir path is not a directory: {raw}"
            raise ValueError(message)
        result.append(p)
    return result


def _exact_resume_descriptor(
    recorded: OrchestrationDescriptor, requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    if recorded != requested:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=f"resuming orchestration {recorded.id!r} cannot change its descriptor",
            )
        )
    return OrchestrationResumeDecision(descriptor=None)


def _project_run_configuration_error(
    error: ProjectRunBaselineMissingError | ProjectRunMismatchError | ProjectRunDirtyResumeError,
) -> ConfigurationError:
    """Translate policy-neutral runtime failures into stable product diagnostics."""
    if isinstance(error, ProjectRunBaselineMissingError):
        code, stage = "project_trusted_baseline_missing", "workspace_setup"
    elif isinstance(error, ProjectRunDirtyResumeError):
        code, stage = "project_resume_configuration_dirty", "resume_resolution"
    else:
        code = {
            ProjectRunMismatchKind.TRUSTED_INPUT_BASELINE: "project_trusted_baseline_mismatch",
            ProjectRunMismatchKind.BRANCH: "project_state_mismatch",
            ProjectRunMismatchKind.TASK: "project_task_mismatch",
        }[error.kind]
        stage = "resume_resolution"
    return ConfigurationError(ConfigurationDiagnostic(code=code, stage=stage, message=str(error)))


def open_run_resources(
    request: RunRequest,
    integration: LocalRunIntegration,
    *,
    resume_policy: (
        Callable[[OrchestrationDescriptor, OrchestrationDescriptor], OrchestrationResumeDecision]
        | None
    ) = None,
    state_binding: _StateBinding | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
) -> "_RunResources":
    """Open the one project context from a canonical request.

    ``backend_factory`` overrides how the compute backend is constructed
    (default: ``vs_sandbox.api.create_compute_backend``); a test injects
    ``vs_sandbox.api.testing.FakeComputeBackend`` here instead of monkeypatching
    the registry or a backend's internal sandbox constructor.
    """
    teardown_stack = ExitStack()
    try:
        return _assemble_run_resources(
            teardown_stack=teardown_stack,
            request=request,
            integration=integration,
            resume_policy=resume_policy,
            state_binding=state_binding,
            backend_factory=backend_factory,
        )
    except BaseException as construction_error:
        _close_after_construction_failure(teardown_stack, construction_error)
        raise


def _close_after_construction_failure(
    teardown_stack: ExitStack, construction_error: BaseException
) -> None:
    """Unwind partial resource construction without replacing its root cause."""
    try:
        teardown_stack.close()
    except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-008203 [BLE001]; cleanup must annotate the original construction failure even if teardown raises a BaseException.
        construction_error.add_note(
            "Additional error while cleaning up partial resource construction: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )


def _assemble_run_resources(  # noqa: C901, PLR0912, PLR0913, PLR0915  # lint-waiver: LW-008204 [C901, PLR0912, PLR0913, PLR0915]; ordered resource setup and ExitStack rollback share mutable lifecycle state, which helper boundaries would obscure.
    *,
    teardown_stack: ExitStack,
    request: RunRequest,
    integration: LocalRunIntegration,
    resume_policy: (
        Callable[[OrchestrationDescriptor, OrchestrationDescriptor], OrchestrationResumeDecision]
        | None
    ) = None,
    state_binding: _StateBinding | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
) -> "_RunResources":
    bundle = request.input_bundle
    exp_name = request.resolved_run_id
    config = request.config
    input_path = str(bundle.root)
    accuracy_command = bundle.accuracy_command_display
    benchmark_command = bundle.benchmark_command_display
    runs_dir = request.runs_dir
    task_name = bundle.task_name
    task_root = bundle.task_root
    workspace_sources = bundle.workspace_sources
    evaluator_path = bundle.evaluator_path
    evaluator_package_root = bundle.evaluator_package_root
    benchmark_output_argument = bundle.benchmark_output_argument
    objective = request.objective or bundle.objective
    existing = request.resume is not None
    orchestration_descriptor = request.orchestration
    orchestration_resume = resume_policy or _exact_resume_descriptor
    profiler_kind = request.profiler_kind
    profiler_domain = bundle.domain
    skills_dirs = request.skills_dirs
    run_environment = request.run_environment
    agent_backend = request.agent_backend
    cli_provider = request.cli_provider
    backend = request.backend
    remote_repo = request.remote_repo
    repo_visibility = request.repo_visibility
    context_start = time.perf_counter()
    # Boot spans recorded before this function ran (the dispatch preamble)
    # come first, so the run log reads in the order the work happened once
    # the buffer below flushes into ``RunLogger``. Assembly's own spans stay
    # in ``boot_trace`` until the drain below, because the earliest of them
    # close before there is a logger to write to.
    buffered_logs: list[str] = boot_trace.drain_log_lines()
    with boot_trace.span("context"):
        with boot_trace.span("config_and_inputs"):
            config = as_config(config)
            for source in workspace_sources:
                if not source.strip_git:
                    raise ConfigurationError(
                        ConfigurationDiagnostic(
                            code="workspace_source_untrackable",
                            stage="workspace_setup",
                            message=(
                                f"workspace source {source.name!r} sets strip_git=false; canonical "
                                "projects require source repositories to be materialized without "
                                "nested Git metadata"
                            ),
                        )
                    )

            run_environment_spec = run_environment or make_run_environment_spec()
            environment = build_run_environment(run_environment_spec)
            input_path_str = _coerce_dir_path(input_path, "--input")
            input_dir = Path(input_path_str)
            run_id = exp_name if existing else generate_run_id(exp_name)
            collection_root = runs_dir.expanduser().resolve() if runs_dir is not None else None
            copied_project = not existing and collection_root is not None
            project_root = collection_root / run_id if copied_project else input_dir
            evaluator_source = _coerce_dir(evaluator_path, "evaluator.source")

            if not copied_project and workspace_sources:
                raise ConfigurationError(
                    ConfigurationDiagnostic(
                        code="project_materialization_required",
                        stage="workspace_setup",
                        message=(
                            "the input project declares workspace sources that must be materialized; "
                            "pass --runs-dir to provision a self-contained project"
                        ),
                    )
                )
            if not copied_project and evaluator_source is not None:
                try:
                    evaluator_source.relative_to(project_root)
                except ValueError as exc:
                    raise ConfigurationError(
                        ConfigurationDiagnostic(
                            code="project_evaluator_not_self_contained",
                            stage="workspace_setup",
                            message=(
                                "a directly launched project must contain its evaluator source; "
                                "pass --runs-dir to copy external evaluator inputs"
                            ),
                        )
                    ) from exc

        with boot_trace.span("backend_and_model"):
            backend_get = backend_factory or create_compute_backend
            backend_impl = backend_get(
                backend,
                log_dir=Project.log_directory_for(project_root, run_id),
                log=buffered_logs.append,
                image=environment.backend_image,
            )
            resolved_backend = str(agent_backend or config.agent.backend or AgentBackend.CLI)
            resolved_cli_provider = cli_provider or config.agent.cli_provider or "codex"
            model_name = config.model.name
        with boot_trace.span("profiler_preflight"):
            resolved_profiler_kind = resolve_profiler_kind(
                profiler_kind,
                domain=profiler_domain,
                backend_profiler_kind=default_profiler_for_backend(backend),
                environment_default_profiler_kind=ProfilerKind(environment.default_profiler_id),
                environment_supported_profiler_kinds=(
                    None
                    if environment.supported_profiler_ids is None
                    else frozenset(
                        ProfilerKind(profiler_id)
                        for profiler_id in environment.supported_profiler_ids
                    )
                ),
            )
            if resolved_profiler_kind in ACTIVE_PROFILER_KINDS:
                agent_spec = agent_spec_from_config(
                    config,
                    backend=agent_backend,
                    provider=cli_provider,
                    model=model_name,
                )
                if not agent_driver_supports_tool_servers(agent_spec):
                    driver_name = resolve_agent_driver(config)
                    definition = profiler_definition(resolved_profiler_kind)
                    raise ConfigurationError(
                        ConfigurationDiagnostic(
                            code="agent_profiler_incompatible",
                            stage="agent_capability_validation",
                            message=(
                                f"Profiler {resolved_profiler_kind.value!r} requires agent tool server "
                                f"{definition.mcp_name!r}, but agent driver {driver_name.value!r} does not "
                                "support agent tool servers. Select agent.driver='agentshim' or "
                                "disable profiling with --profiler none."
                            ),
                        )
                    )
            profiler_preflight = preflight_profiler_kind(
                resolved_profiler_kind,
                native_preflight=_native_profiler_preflight,
            )
            if not profiler_preflight.usable:
                raise ConfigurationError(
                    ConfigurationDiagnostic(
                        code="profiler_preflight_failed",
                        stage="profiler_preflight",
                        message=profiler_preflight.error_message(),
                    )
                )
            skill_source_paths = _coerce_skills_dirs(skills_dirs)
            execution_record = RunExecutionRecord(
                model=config.model.name,
                agent_backend=resolved_backend,
                agent_driver=(
                    resolve_agent_driver(config).value if resolved_backend == "cli" else None
                ),
                cli_provider=resolved_cli_provider,
                cli_timeout=config.agent.cli_timeout,
                compute_backend=backend.value,
                requested_profiler=profiler_kind.value,
                resolved_profiler=resolved_profiler_kind.value,
                default_reasoning_effort=config.thinking.level,
                thinking_budget=config.thinking.budget,
                outer_model=config.agent.outer.model,
                outer_reasoning_effort=config.agent.outer.reasoning_effort,
                inner_model=config.agent.inner.model,
                inner_reasoning_effort=config.agent.inner.reasoning_effort,
                perf_eval_load_levels=(
                    [level.model_dump(mode="json") for level in config.perf_eval.load_levels]
                    if config.perf_eval.load_levels is not None
                    else None
                ),
                skills_dirs=[str(path) for path in skill_source_paths],
            )

        with boot_trace.span("workspace_materialize"):
            profiler_support_path: str | None = None
            profiler_support_name: str | None = None
            profiler_support_extra: tuple[tuple[str, str], ...] = ()
            if resolved_profiler_kind in ACTIVE_PROFILER_KINDS:
                definition = profiler_definition(resolved_profiler_kind)
                profiler_support_name = definition.support_name
                default_support = BUNDLED_RESOURCES.directory("profilers", definition.kind.value)
                if default_support is not None:
                    profiler_support_path = str(default_support)
                profiler_support_extra = _profiler_support_extra(definition)

            input_project_dir = input_dir if (input_dir / "pyproject.toml").is_file() else None

            model_artifacts = None

            workspace_files = create_project_materializer(
                project_root,
                environment=environment,
                backend=backend_impl,
                log=buffered_logs.append,
            )
            construction_complete = False
            if copied_project:

                def _remove_incomplete_project() -> None:
                    if not construction_complete and project_root.exists():
                        shutil.rmtree(project_root)

                teardown_stack.callback(_remove_incomplete_project)
                source_reference = (task_root or input_dir) / "reference"
                model_artifacts = prepare_domain_model_artifacts(
                    bundle.domain,
                    ModelArtifactRequest(
                        reference_dir=source_reference,
                        model_cache_dir=collection_root / ".cache" / "huggingface",
                        runtime_artifact_dir=(
                            source_reference
                            if task_name is None
                            else collection_root / ".cache" / "llm-serving" / run_id
                        ),
                        log=buffered_logs.append,
                    ),
                    isolated=environment.isolated,
                    materialize_local_weights=environment.materialize_local_model_weights,
                )
                provision_project(
                    input_dir,
                    project_root,
                    spec=ProjectProvisioningSpec(
                        materializer=workspace_files,
                        workspace_sources=workspace_sources,
                        evaluator_source=evaluator_source,
                        task_name=task_name,
                        input_project_dir=input_project_dir,
                        input_excludes=model_artifacts.copy_excludes,
                    ),
                )
                if evaluator_source is not None:
                    evaluator_source = project_root / "_evaluator" / evaluator_source.name
            else:
                workspace_files.create()

        if existing:
            with boot_trace.span("workspace_repair"):
                workspace_files.repair()
        project_excluded_dirs = set(workspace_files.excluded_dirs)
        if profiler_support_name is not None:
            project_excluded_dirs.add(profiler_support_name)
        project_excluded_dirs.update(name for _path, name in profiler_support_extra)

        def resolve_recorded_run(
            recorded: OrchestrationRunManifest,
        ) -> OrchestrationResumeDecision:
            return _resume_orchestration_decision(
                recorded,
                orchestration_descriptor,
                run_environment_spec,
                execution_record,
                orchestration_resume,
            )

        try:
            project_resources = open_project_run_resources(
                ProjectRunRequest(
                    project_root=project_root,
                    run_id=run_id,
                    display_name=exp_name,
                    task_name=task_name,
                    existing=existing,
                    framework_version=_installed_vibesys_version(),
                    run_environment=run_environment_record(run_environment_spec),
                    execution=execution_record,
                    orchestration=orchestration_descriptor,
                    excluded_dirs=frozenset(project_excluded_dirs),
                    trusted_input_paths=tuple(
                        trusted_project_input_paths(
                            project_root,
                            evaluator_source=evaluator_source,
                        )
                    ),
                    state=(
                        ProjectStateDeclaration(state_binding.namespace, state_binding.model)
                        if state_binding is not None
                        else None
                    ),
                ),
                effects=ProjectRunEffects(
                    git_events=CoreGitTrackerEvents(integration.events),
                    log_emit=_run_log_emitter(integration.agent_events),
                    on_log_ready=integration.attach,
                ),
                buffered_logs=buffered_logs,
                resolve_resume=resolve_recorded_run,
            )
        except (
            ProjectRunBaselineMissingError,
            ProjectRunMismatchError,
            ProjectRunDirtyResumeError,
        ) as error:
            raise _project_run_configuration_error(error) from error
        teardown_stack.callback(project_resources.close)
        project = project_resources.project
        project_state = project.state
        log_dir = project_resources.logger.log_dir
        logger = project_resources.logger
        git = project_resources.git

        prepared_evaluator = prepare_trusted_evaluator(
            evaluator_package_root,
            project_state.model_cache_directory("evaluator-tools"),
        )
        evaluator_requirements = prepared_evaluator.requirements
        evaluator_tool_roots = prepared_evaluator.tool_roots

        with boot_trace.span("workspace_setup"):
            integration.attach(log_dir, project=project, run_id=run_id)
            integration.events.emit(
                CoreEventType.EXPERIMENTS_CHANGED,
                data=ExperimentsChangedData(reason="project_attached"),
            )
            logger.lprint(
                f"experiments gate open after {(time.perf_counter() - context_start) * 1000:.0f}ms"
            )

            project_ref_dir = (
                project_root / task_root.relative_to(input_dir) / "reference"
                if task_root is not None and copied_project
                else (task_root or project_root) / "reference"
            )
            ref_dir = project_ref_dir if project_ref_dir.is_dir() else None
            if ref_dir is not None:
                reference_py = sorted(ref_dir.glob("*.py"))
                reference_root = ref_dir.relative_to(project_root).as_posix()
                ref_name = (
                    f"{reference_root}/{reference_py[0].name}"
                    if len(reference_py) == 1
                    else reference_root
                )
            else:
                ref_name = "."

            if model_artifacts is None:
                model_artifacts = prepare_domain_model_artifacts(
                    bundle.domain,
                    ModelArtifactRequest(
                        reference_dir=project_ref_dir,
                        model_cache_dir=project_state.model_cache_directory("huggingface"),
                        runtime_artifact_dir=project_state.model_cache_directory("llm-serving"),
                        log=logger.lprint,
                    ),
                    isolated=environment.isolated,
                    materialize_local_weights=environment.materialize_local_model_weights,
                )

            plan = build_workspace_materialization_plan(
                project_root,
                existing=True,
                input_dir=project_root,
                evaluator_source=None,
                skill_sources=skill_source_paths,
                input_project_dir=None,
                profiler_support_path=profiler_support_path,
                profiler_support_name=profiler_support_name,
                skill_excluded_relative_paths=platform_skill_excluded_paths(backend),
                workspace_sources=(),
                extra_input_excludes=model_artifacts.copy_excludes,
                profiler_support_extra=profiler_support_extra,
            )
            workspace_files.materialize(plan, existing=True)

        with boot_trace.span("environment_plan"):
            runtime_state = project_state.portable_namespace(run_id, "runtime")
            objective_document: Path | None = None
            if objective is not None:
                objective_document = runtime_state.external_directory() / "effective-objective.md"
                objective_document.parent.mkdir(parents=True, exist_ok=True)
                objective_document.write_text(objective)
                git.snapshot_framework_state(
                    "vibesys: record effective objective",
                    runtime_state.snapshot(),
                )

            project_path_policy = build_project_path_policy(
                project_root,
                evaluator_source=evaluator_source,
            )

            experiment_repository = ExperimentRepository(project_root, logger.lprint)
            origin_exists = experiment_repository.has_origin()
            if (
                remote_repo is not None
                and origin_exists
                and not experiment_repository.origin_matches(remote_repo)
            ):
                raise ConfigurationError(
                    ConfigurationDiagnostic(
                        code="repository_setup_failed",
                        stage="repository_setup",
                        message=(
                            f"Project origin does not match requested repository {remote_repo!r}: "
                            f"{project_root}"
                        ),
                    )
                )
            should_publish = remote_repo is not None or (
                existing
                and origin_exists
                and (
                    collection_root is not None
                    or experiment_repository.current_run_branch_tracks_origin()
                )
            )
            if should_publish:
                try:
                    if remote_repo is not None and not origin_exists:
                        experiment_repository.create_remote(remote_repo, repo_visibility)
                except Exception as exc:
                    raise ConfigurationError(
                        ConfigurationDiagnostic(
                            code="repository_setup_failed",
                            stage="repository_setup",
                            message=f"Could not configure project repository {remote_repo!r}: {exc}",
                        )
                    ) from exc

                def _push_experiment_repository() -> None:
                    try:
                        experiment_repository.push()
                    except Exception as exc:
                        raise ConfigurationError(
                            ConfigurationDiagnostic(
                                code="repository_sync_failed",
                                stage="repository_sync",
                                message=f"Could not push project repository: {exc}",
                            )
                        ) from exc

                teardown_stack.callback(_push_experiment_repository)

            run_environment_request = RunEnvironmentRequest(
                log_dir=log_dir,
                workspace=project_root,
                seeded_workspace_paths=tuple(source.dest for source in workspace_sources),
                ref_dir=ref_dir,
                backend=backend_impl,
                agent_backend=resolved_backend,
                cli_provider=resolved_cli_provider,
                run_id=run_id,
                objective=objective,
                objective_document=objective_document,
                accuracy_command=accuracy_command,
                benchmark_command=benchmark_command,
                benchmark_output_argument=benchmark_output_argument,
                evaluator_requirements=evaluator_requirements,
                profiler_support_path=profiler_support_path,
                profiler_support_name=profiler_support_name,
                profiler_support_extra=profiler_support_extra,
                git_history_root=git.history_root,
                environment_bind_mounts=model_artifacts.bind_mounts,
                log=logger.lprint,
                framework_root=PROJECT_ROOT,
                project_path_policy=project_path_policy,
                state_namespace=project_state.local_namespace(run_id, "skypilot"),
            )
        environment_resources = open_run_environment_resources(
            run_environment_request,
            lambda request: open_run_environment(environment, request),
        )
        teardown_stack.callback(environment_resources.close)
        session = environment_resources.session

        # A microservice candidate is a container topology, so its local agent needs
        # resources the default confinement withholds. Other domains keep the
        # narrower default set.
        agent_host_resources = task_agent_host_resources(
            container_topology=profiler_domain is DomainName.MICROSERVICES,
            cli_sandboxed=session.view.cli_sandboxed,
            task_name=task_name,
            evaluator_package_root=evaluator_package_root,
            evaluator_tool_roots=evaluator_tool_roots,
        )
        if not session.view.cli_sandboxed:
            agent_host_resources = (*agent_host_resources, _vibesys_runtime_host_resource())

        result = _RunResources(
            backend=backend,
            profiler_kind=resolved_profiler_kind,
            skill_source_paths=skill_source_paths,
            ref_name=ref_name,
            trusted_evaluation_plan=_trusted_evaluation_plan(bundle, session),
            teardown_stack=teardown_stack,
            environment_resources=environment_resources,
            project_resources=project_resources,
            agent_host_resources=agent_host_resources,
        )
        integration.publish_resources(
            RunResources(
                project=project,
                run_id=run_id,
                workspace=project_root,
                log_dir=log_dir,
                agent_backend=resolved_backend,
                driver=resolve_agent_driver(config).value,
                provider=resolved_cli_provider,
                model=model_name,
                role_models=tuple(
                    role.model
                    for role in (config.agent.outer, config.agent.inner)
                    if role.model is not None
                ),
                config=config,
                compute_backend=backend,
                skill_source_dirs=tuple(skill_source_paths),
                environment=environment,
                environment_request=run_environment_request,
                environment_session=session,
                host_resources=agent_host_resources,
            )
        )
        construction_complete = True
    # Assembly's spans, including the enclosing one that just closed with the
    # total. The run log gets them in completion order: children, then parent.
    for line in boot_trace.drain_log_lines():
        logger.lprint(line)
    return result


class _RunResources:
    """Private owner of one workspace's assembled resources and teardown stack.

    Product composition exposes focused policy capabilities. This object keeps
    the Git tracker, environment session, logger, state namespace, and device
    lease together so setup failure and run closure unwind them in construction
    order.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-008208 [PLR0913]; `_RunResources` receives already-owned runtime resources explicitly, without a second mutable parameter container.
        self,
        *,
        backend: ComputeBackend,
        profiler_kind: ProfilerKind,
        skill_source_paths: list[Path],
        ref_name: str,
        trusted_evaluation_plan: TrustedEvaluationPlan,
        teardown_stack: ExitStack,
        environment_resources: RunEnvironmentResources,
        project_resources: ProjectRunResources,
        agent_host_resources: tuple[HostResource, ...] = (),
    ) -> None:
        self.backend = backend
        self.agent_host_resources = agent_host_resources
        self.profiler_kind = profiler_kind
        self._skill_source_paths = skill_source_paths
        self.ref_name = ref_name
        self._project_resources = project_resources
        self.environment_resources = environment_resources
        self.trusted_evaluation_plan = trusted_evaluation_plan
        self._teardown_stack = teardown_stack
        self._closed = False

    @property
    def workspace(self) -> Path:
        """Return the project root, which is also the only agent workspace."""
        return self.environment_resources.request.workspace

    @property
    def project_resources(self) -> ProjectRunResources:
        """Return the lower-owned project resource aggregate for host composition."""
        return self._project_resources

    @property
    def project(self) -> Project:
        return self._project_resources.project

    @property
    def git(self) -> GitTracker:
        return self._project_resources.git

    @property
    def logger(self) -> RunLogger:
        return self._project_resources.logger

    @property
    def state(self) -> RunState:
        return self._project_resources.state

    @property
    def run_id(self) -> str:
        return self._project_resources.state.run_id

    @property
    def log_dir(self) -> Path:
        """Return the environment's run-log directory."""
        return self.environment_resources.request.log_dir

    @property
    def environment_request(self) -> RunEnvironmentRequest:
        """Return the authoritative request used to open this workspace."""
        return self.environment_resources.request

    @property
    def run_environment_session(self) -> RunEnvironmentSession:
        """Return the lower-owned active environment session."""
        return self.environment_resources.session

    @property
    def run_environment_view(self) -> RunEnvironmentView:
        """Return environment facts resolved during session construction."""
        return self.environment_resources.view

    @property
    def device(self) -> DeviceLease:
        """Return the root-owned or candidate-borrowed device lease."""
        return self.environment_resources.device

    @property
    def run_log_path(self) -> Path:
        """Return the logger's current output path."""
        return self.logger.path

    @property
    def run_log_file(self) -> TextIO:
        """The current open log file handle (owned by ``RunLogger``)."""
        return self.logger.writer

    @property
    def skill_source_paths(self) -> list[Path]:
        """Skill source directories copied into the workspace for agents."""
        return self._skill_source_paths

    @property
    def round_transaction_coordinator(self) -> MultiSlotRoundTransactionCoordinator | None:
        """Return the checkpoint coordinator prepared for plugin state."""
        return self._project_resources.round_transaction_coordinator

    def lprint(self, text: str) -> None:
        self.logger.lprint(text)

    def switch_log_file(self, label: int | str) -> None:
        """Switch to a per-phase log file, see :meth:`RunLogger.switch`."""
        self.logger.switch(label)

    def reselect_gpu(self) -> None:
        """Delegate mid-run device rebalance to the lower resource owner."""
        self.environment_resources.reselect_device()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Unwinds in reverse construction order: device monitor stop,
        # environment teardown, run-environment exit, and log closure.
        self._teardown_stack.close()

    def __enter__(self) -> "_RunResources":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
