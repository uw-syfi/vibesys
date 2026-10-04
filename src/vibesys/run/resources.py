"""Explicit product composition for one canonical VibeSys run."""

import shlex
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import overload

from vibesys.composition import (
    _vibesys_runtime_host_resources,
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
from vibesys.orchestration.profilers import (
    ACTIVE_PROFILER_KINDS,
    ProfilerKind,
    profiler_definition,
)
from vibesys.orchestration.profilers import (
    profiler_support_extra as resolve_profiler_support_extra,
)
from vibesys.orchestration.skill_selection import (
    platform_skill_excluded_paths,
    resolve_skill_source_paths,
)
from vibesys.run.contracts import RunRequest
from vibesys.run.environment import open_run_environment
from vibesys.run.evaluation import trusted_evaluation_plan
from vibesys.run.experiment_repo import ExperimentRepository
from vibesys.run.git_events import CoreGitTrackerEvents
from vibesys.run.integration import LocalRunIntegration, RunResources, run_log_emitter
from vibesys.run.profilers import resolve_run_profiler, validate_run_request
from vibesys.run.project import (
    ProjectProvisioningSpec,
    exact_resume_descriptor,
    installed_vibesys_version,
    project_run_configuration_error,
    provision_project,
    resume_orchestration_decision,
    validate_agent_role_catalog,
)
from vibesys.run.project_policy import (
    build_project_path_policy,
    trusted_project_input_paths,
)
from vibesys.run.workspace_policy import (
    AGENT_CONFIG_FILES,
    build_workspace_materialization_plan,
    create_project_materializer,
    materialized_skill_dirs,
)
from vs_agent.api import (
    AgentBackend,
    AgentSpec,
    task_agent_host_resources,
)
from vs_project.api import (
    AgentRoleExecutionRecord,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    Project,
    RunExecutionRecord,
    generate_run_id,
)
from vs_runtime.api import (
    OrchestrationResumeDecision,
    ProfileExecution,
    RunFacts,
    WorkspaceSourceFact,
    boot_trace,
)
from vs_runtime.api.infrastructure import (
    ModelArtifactRequest,
    ProjectRunBaselineMissingError,
    ProjectRunDirtyResumeError,
    ProjectRunEffects,
    ProjectRunMismatchError,
    ProjectRunRequest,
    ProjectRunResources,
    ProjectStateDeclaration,
    RunEnvironmentRequest,
    RunEnvironmentResources,
    TrustedEvaluationPlan,
    build_run_environment,
    make_run_environment_spec,
    offered_skill_facts,
    open_project_run_resources,
    open_run_environment_resources,
    prepare_trusted_evaluator,
    run_environment_record,
)
from vs_sandbox.api import (
    ComputeBackendImpl,
    HostResource,
    create_compute_backend,
)

_StateBinding = ProjectStateDeclaration


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


@overload
def _coerce_dir_path(raw: str, label: str) -> str: ...


@overload
def _coerce_dir_path(raw: None, label: str) -> None: ...


def _coerce_dir_path(raw: str | None, label: str) -> str | None:
    path = _coerce_dir(raw, label)
    return str(path) if path is not None else None


def open_run_resources(  # noqa: PLR0913  # lint-waiver: LW-731905 [PLR0913]; private run composition receives independent policy bindings and effect factories explicitly instead of hiding them in a service container.
    request: RunRequest,
    integration: LocalRunIntegration,
    *,
    ownership: ExitStack,
    agent_specs: Mapping[str, AgentSpec],
    resume_policy: (
        Callable[[OrchestrationDescriptor, OrchestrationDescriptor], OrchestrationResumeDecision]
        | None
    ) = None,
    state_binding: _StateBinding | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
) -> "_PreparedRun":
    """Open one project context and register its resources with ``ownership``.

    The caller owns the stack and must close it after use or construction
    failure. Product composition chooses every resource; the runtime host owns
    their common lifetime.

    ``backend_factory`` overrides how the compute backend is constructed
    (default: ``vs_sandbox.api.create_compute_backend``); a test injects
    ``vs_sandbox.api.testing.FakeComputeBackend`` here instead of monkeypatching
    the registry or a backend's internal sandbox constructor.
    """
    return _assemble_run_resources(
        ownership=ownership,
        request=request,
        integration=integration,
        agent_specs=agent_specs,
        resume_policy=resume_policy,
        state_binding=state_binding,
        backend_factory=backend_factory,
    )


def _assemble_run_resources(  # noqa: C901, PLR0912, PLR0913, PLR0915  # lint-waiver: LW-008204 [C901, PLR0912, PLR0913, PLR0915]; ordered product setup and ownership registration share mutable lifecycle state, which helper boundaries would obscure.
    *,
    ownership: ExitStack,
    request: RunRequest,
    integration: LocalRunIntegration,
    agent_specs: Mapping[str, AgentSpec],
    resume_policy: (
        Callable[[OrchestrationDescriptor, OrchestrationDescriptor], OrchestrationResumeDecision]
        | None
    ) = None,
    state_binding: _StateBinding | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
) -> "_PreparedRun":
    validate_run_request(request)
    bundle = request.input_bundle
    exp_name = request.exp_name or request.resolved_run_id
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
    orchestration_resume = resume_policy or exact_resume_descriptor
    profiler_domain = bundle.domain
    skills_dirs = request.skills_dirs
    run_environment = request.run_environment
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
            run_id = (
                request.resolved_run_id
                if existing or request.run_id is not None
                else generate_run_id(exp_name)
            )
            collection_root = runs_dir.expanduser().resolve() if runs_dir is not None else None
            copied_project = not existing and collection_root is not None
            project_root = collection_root / run_id if copied_project else input_dir
            evaluator_source = _coerce_dir(evaluator_path, "evaluator.source")

            # A resumed collection run already holds its materialized sources.
            if not existing and not copied_project and workspace_sources:
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

            if existing:
                recorded_run = Project.open(project_root).state.load_run(run_id)
                validate_agent_role_catalog(recorded_run.execution.agent_roles, agent_specs)

        with boot_trace.span("profiler_preflight"):
            resolved_profiler_kind = resolve_run_profiler(request, environment)

        with boot_trace.span("backend_and_model"):
            backend_get = backend_factory or create_compute_backend
            backend_impl = backend_get(
                backend,
                log_dir=Project.log_directory_for(project_root, run_id),
                log=buffered_logs.append,
                image=environment.backend_image,
            )
            model_name = config.model.name
        with boot_trace.span("execution_record"):
            skill_source_paths = resolve_skill_source_paths(skills_dirs)
            resolved_backend = str(
                request.agent_backend or config.agent.backend or AgentBackend.CLI
            )
            resolved_cli_provider = request.cli_provider or config.agent.cli_provider or "codex"
            execution_record = RunExecutionRecord(
                model=config.model.name,
                agent_backend=resolved_backend,
                agent_driver=(
                    resolve_agent_driver(config).value if resolved_backend == "cli" else None
                ),
                cli_provider=resolved_cli_provider,
                cli_timeout=config.agent.cli_timeout,
                compute_backend=backend.value,
                requested_profiler=request.profiler_kind.value,
                resolved_profiler=resolved_profiler_kind.value,
                default_reasoning_effort=config.thinking.level,
                thinking_budget=config.thinking.budget,
                agent_roles={
                    role_id: AgentRoleExecutionRecord(
                        model=spec.model if spec.model is not None else config.model.name,
                        reasoning_effort=spec.reasoning_effort,
                    )
                    for role_id, spec in agent_specs.items()
                },
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
                profiler_support_extra = resolve_profiler_support_extra(definition)

            input_project_dir = input_dir if (input_dir / "pyproject.toml").is_file() else None

            model_artifacts = None

            workspace_files = create_project_materializer(
                project_root,
                environment=environment,
                backend=backend_impl,
                log=buffered_logs.append,
            )
            if copied_project:
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
        project_excluded_dirs.update(materialized_skill_dirs(skill_source_paths))

        def resolve_recorded_run(
            recorded: OrchestrationRunManifest,
        ) -> OrchestrationResumeDecision:
            return resume_orchestration_decision(
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
                    framework_version=installed_vibesys_version(),
                    run_environment=run_environment_record(run_environment_spec),
                    execution=execution_record,
                    orchestration=orchestration_descriptor,
                    objective=objective,
                    provisional_project=workspace_files if copied_project else None,
                    excluded_dirs=frozenset(project_excluded_dirs),
                    excluded_files=AGENT_CONFIG_FILES,
                    candidate_support_dirs=frozenset(
                        name
                        for name in (
                            profiler_support_name,
                            *(name for _path, name in profiler_support_extra),
                        )
                        if name is not None
                    ),
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
                    log_emit=run_log_emitter(integration.agent_events),
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
            raise project_run_configuration_error(error) from error
        ownership.callback(project_resources.close)
        project = project_resources.project
        project_state = project.state
        log_dir = project_resources.logger.log_dir
        logger = project_resources.logger
        git = project_resources.git

        prepared_evaluator = prepare_trusted_evaluator(
            evaluator_package_root,
            # Tools are installed under their specification digest, so every
            # project on this machine can reuse one build.
            project_state.machine_cache_directory("evaluator-tools"),
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
            project_path_policy = build_project_path_policy(
                project_root,
                evaluator_source=evaluator_source,
            )

            experiment_repository = ExperimentRepository(project_root, logger.lprint)
            experiment_repository.configure(
                remote_repo,
                repo_visibility,
                existing_run=existing,
                collection_project=collection_root is not None,
            )
            ownership.callback(experiment_repository.close)

            run_environment_request = RunEnvironmentRequest(
                log_dir=log_dir,
                agent_homes_dir=Project.agent_homes_directory_for(project_root, run_id),
                workspace=project_root,
                seeded_workspace_paths=tuple(source.dest for source in workspace_sources),
                ref_dir=ref_dir,
                backend=backend_impl,
                agent_backend=resolved_backend,
                cli_provider=resolved_cli_provider,
                run_id=run_id,
                objective=objective,
                objective_document=project_resources.objective_document,
                accuracy_command=accuracy_command,
                benchmark_command=benchmark_command,
                profile_command=(
                    shlex.join(bundle.profile_command) if bundle.profile_command else None
                ),
                profile_timeout_seconds=(
                    bundle.manifest.profile.timeout_seconds if bundle.manifest.profile else None
                ),
                benchmark_output_argument=benchmark_output_argument,
                evaluator_requirements=evaluator_requirements,
                profiler_support_path=profiler_support_path,
                profiler_support_name=profiler_support_name,
                profiler_support_extra=profiler_support_extra,
                git_history_root=git.history_root,
                run_owned_roots=(project_state.candidate_worktrees_directory(run_id),),
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
        ownership.callback(environment_resources.close)
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
            agent_host_resources = (*agent_host_resources, *_vibesys_runtime_host_resources())
        result = _PreparedRun(
            backend=backend,
            agent_specs=agent_specs,
            facts=_run_facts(
                request,
                environment_resources,
                ref_name=ref_name,
                profiler_kind=resolved_profiler_kind,
                skill_source_paths=skill_source_paths,
            ),
            skill_source_paths=tuple(skill_source_paths),
            evaluation_plan=trusted_evaluation_plan(bundle, session),
            environment_resources=environment_resources,
            project_resources=project_resources,
            agent_host_resources=agent_host_resources,
            profiler_agent_resources=session.view.profiler_mcp_resources,
        )
        integration.publish_resources(
            RunResources(
                project_resources=project_resources,
                environment_resources=environment_resources,
                agent_backend=resolved_backend,
                driver=resolve_agent_driver(config).value,
                provider=resolved_cli_provider,
                model=model_name,
                role_models=tuple(
                    spec.model
                    for spec in agent_specs.values()
                    if spec.model is not None and spec.model != model_name
                ),
                config=config,
                backend=backend,
                skill_source_dirs=tuple(skill_source_paths),
                host_resources=agent_host_resources,
            )
        )
        project_resources.mark_ready()
        experiment_repository.arm()
    # Assembly's spans, including the enclosing one that just closed with the
    # total. The run log gets them in completion order: children, then parent.
    for line in boot_trace.drain_log_lines():
        logger.lprint(line)
    return result


def _run_facts(
    request: RunRequest,
    environment: RunEnvironmentResources,
    *,
    ref_name: str,
    profiler_kind: ProfilerKind,
    skill_source_paths: list[Path],
) -> RunFacts:
    """Resolve the immutable policy facts exposed by the runtime host."""
    bundle = request.input_bundle
    view = environment.view
    return RunFacts(
        domain_id=bundle.domain.value,
        objective=request.objective or bundle.objective,
        environment_notes=view.prompt_notes,
        profile_execution=ProfileExecution(view.profile_execution),
        objective_location=view.paths.objective,
        reference_location=ref_name,
        accuracy_command=view.paths.accuracy_command,
        benchmark_command=view.paths.benchmark_command,
        accuracy_configured=bool(view.paths.accuracy_command),
        benchmark_configured=(
            bundle.benchmark_result is not None or bundle.benchmark_result_protocol is not None
        ),
        profiler_id=profiler_kind.value,
        workspace_sources=tuple(
            WorkspaceSourceFact(name=source.name, dest=source.dest)
            for source in bundle.workspace_sources
        ),
        skills=offered_skill_facts(skill_source_paths),
    )


@dataclass(slots=True)
class _PreparedRun:
    """Typed product values backed by resources in the caller's ownership stack."""

    project_resources: ProjectRunResources
    environment_resources: RunEnvironmentResources
    facts: RunFacts
    backend: ComputeBackend
    agent_specs: Mapping[str, AgentSpec]
    skill_source_paths: tuple[Path, ...]
    evaluation_plan: TrustedEvaluationPlan
    agent_host_resources: tuple[HostResource, ...]
    profiler_agent_resources: tuple[HostResource, ...]
