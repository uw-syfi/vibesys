"""Shared lifecycle context for one canonical VibeSys project run."""

import asyncio
import re
import shutil
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any, TextIO, TypeVar, overload

from pydantic import BaseModel

from vibesys import backends, boot_trace
from vibesys.agent_spec_config import agent_spec_from_config, resolve_agent_driver
from vibesys.backends.base import ComputeBackendImpl, ContentionMonitor
from vibesys.config import Config, as_config
from vibesys.constants import (
    PROJECT_ROOT,
    ComputeBackend,
    DomainName,
)
from vibesys.domains.environment import (
    EnvironmentBindMount,
    EnvironmentContext,
    EnvironmentHooks,
    EnvironmentPatch,
    NoopEnvironmentHooks,
)
from vibesys.domains.registry import resolve_domain
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.evaluators import load_evaluator_package, tool_install_root
from vibesys.evaluators.input_manifest import WorkspaceSource
from vibesys.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    CoreEventType,
    EventStatus,
    ExperimentsChangedData,
    InvocationFinishedData,
    InvocationStartedData,
    PhaseData,
    json_value,
)
from vibesys.orchestration import OrchestrationResumeDecision
from vibesys.orchestration.request import RunRequest
from vibesys.profilers import (
    ACTIVE_PROFILER_KINDS,
    ProfilerKind,
    preflight_profiler_kind,
    profiler_definition,
    resolve_profiler_kind,
)
from vibesys.render.log import log_and_print
from vibesys.render.run_log import RunLogRenderer
from vibesys.render.sink import output_sink
from vibesys.resource_paths import profiler_support_dir
from vibesys.run import (
    DeviceLease,
    ExperimentRepository,
    GitTracker,
    ProjectProvisioningSpec,
    RunCommands,
    RunLogger,
    RunPaths,
    RunResourceHandoff,
    RunStateNamespace,
    Workspace,
    provision_project,
    splice_steering,
)
from vibesys.run.git_events import CoreGitTrackerEvents
from vibesys.run.integration import LocalRunIntegration
from vibesys.run.project_policy import (
    build_project_path_policy,
    trusted_project_input_paths,
)
from vibesys.run.recovery import RecoveryWorkspace
from vibesys.run.round_transaction import (
    MultiSlotRoundTransactionCoordinator,
    RoundRecoveryOutcome,
    RoundTransaction,
    RoundTransactionCoordinator,
)
from vibesys.run.state import RunState
from vibesys.sandbox.run_environment import (
    RunEnvironment,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    RunEnvironmentSpec,
    build_run_environment,
    make_run_environment_spec,
    run_environment_record,
)
from vibesys.skills import SkillSelection, platform_skill_selection
from vs_agent.api import (
    AgentBackend,
    AgentClientProtocol,
    AgentProgress,
    AgentSessionState,
    DurableSessionStore,
    agent_driver_supports_mcp_servers,
    build_agent_client,
    task_agent_host_resources,
)
from vs_project.api import (
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    Project,
    RunExecutionRecord,
    StateTransition,
    generate_run_id,
)
from vs_sandbox.api import HostResource, HostResourceAccess, ProjectPathPolicy, Sandbox

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class RunStartHints:
    """Optional policy-owned budget and agent roles shown when a run starts."""

    max_rounds: int | None = None
    expected_roles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunSetup:
    """Policy-owned facts needed before the shared run context can open."""

    state_namespace: str | None = None
    state_model: type[BaseModel] | None = None
    state_slots: Mapping[str, type[BaseModel]] | None = None
    resume_policy: (
        Callable[[OrchestrationDescriptor, OrchestrationDescriptor], OrchestrationResumeDecision]
        | None
    ) = None
    resume_recovery: Callable[[RecoveryWorkspace], None] | None = None
    use_default_agent: bool = False
    start_hints: RunStartHints | None = None

    def __post_init__(self) -> None:
        """Reject incomplete policy-owned state slot declarations."""
        if self.state_namespace is None and (self.state_model is not None or self.state_slots):
            raise ValueError("RunSetup state slots require state_namespace")  # noqa: TRY003
        if self.state_namespace is not None and self.state_model is None and not self.state_slots:
            raise ValueError("RunSetup requires at least one state slot")  # noqa: TRY003
        if self.state_model is not None and self.state_slots:
            raise ValueError("RunSetup cannot mix legacy state_model with state_slots")  # noqa: TRY003
        if self.state_namespace == "":
            raise ValueError("RunSetup.state_namespace must be nonempty")  # noqa: TRY003


class _MissingDefaultAgentClientError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("this run has no built-in agent client")


class _MissingDefaultAgentSpecError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("built-in agent client requires an agent specification")


def _attempt_from_label(round_label: str) -> int | None:
    match = re.search(r"retry-(\d+)", round_label)
    return int(match.group(1)) if match else None


def _execution_status(error: BaseException | None) -> EventStatus:
    if error is None:
        return EventStatus.COMPLETED
    if isinstance(error, asyncio.CancelledError) or type(error).__name__ == "CancelledError":
        return EventStatus.CANCELLED
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return EventStatus.INTERRUPTED
    return EventStatus.FAILED


def _coerce_dir(raw: str | Path | None, label: str) -> Path | None:
    if raw is None:
        return None
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        raise ValueError(f"{label} path does not exist: {raw}")  # noqa: TRY003  # tracked: #288
    if not p.is_dir():
        raise ValueError(f"{label} path is not a directory: {raw}")  # noqa: TRY003  # tracked: #288
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
    """Check the generic v4 identity and delegate option policy to its owner."""
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
            raise ValueError(f"--skills-dir path does not exist: {raw}")  # noqa: TRY003  # tracked: #288
        if not p.is_dir():
            raise ValueError(f"--skills-dir path is not a directory: {raw}")  # noqa: TRY003  # tracked: #288
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


def _round_transaction_for_setup(
    setup: RunSetup,
) -> (
    Callable[
        [Project, GitTracker, str],
        RoundTransactionCoordinator | MultiSlotRoundTransactionCoordinator,
    ]
    | None
):
    namespace = setup.state_namespace
    model = setup.state_model
    models = setup.state_slots
    if namespace is None:
        return None

    if models is not None:

        def open_multi_coordinator(
            project: Project, git: GitTracker, run_id: str
        ) -> MultiSlotRoundTransactionCoordinator:
            # A resumed v4 run can still have a journal written before the
            # policy adopted multi-slot checkpoints. Replay it through its
            # original typed slot before opening the new journal.
            legacy_model = models.get("state.json")
            if legacy_model is not None:
                legacy_slot = project.state.portable_namespace(run_id, namespace).slot(
                    "state.json", legacy_model
                )
                RoundTransactionCoordinator(project, git, run_id, state_slot=legacy_slot).recover()
            return MultiSlotRoundTransactionCoordinator(
                project, git, run_id, namespace=namespace, models=models
            )

        return open_multi_coordinator

    if model is None:
        return None

    def open_coordinator(
        project: Project, git: GitTracker, run_id: str
    ) -> RoundTransactionCoordinator[BaseModel]:
        slot = project.state.portable_namespace(run_id, namespace).slot("state.json", model)
        return RoundTransactionCoordinator(project, git, run_id, state_slot=slot)

    return open_coordinator


def open_run_context(
    request: RunRequest, setup: RunSetup, integration: LocalRunIntegration
) -> "_RunContext":
    """Open the one project context from a canonical request and policy setup."""
    teardown_stack = ExitStack()
    try:
        return _assemble_run_context(
            teardown_stack=teardown_stack,
            request=request,
            setup=setup,
            integration=integration,
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
    except BaseException as cleanup_error:  # noqa: BLE001  # tracked: #288
        construction_error.add_note(
            "Additional error while cleaning up partial resource construction: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )


def _assemble_run_context(  # noqa: C901, PLR0912, PLR0915  # tracked: #288
    *,
    teardown_stack: ExitStack,
    request: RunRequest,
    setup: RunSetup,
    integration: LocalRunIntegration,
) -> "_RunContext":
    bundle = request.input_bundle
    exp_name = request.resume.run_id if request.resume is not None else request.exp_name
    if exp_name is None:
        raise ValueError("RunRequest.exp_name is required for a fresh run")  # noqa: TRY003
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
    orchestration_resume = setup.resume_policy or _exact_resume_descriptor
    trusted_input_baseline = None
    debug = request.debug
    profiler_kind = request.profiler_kind
    profiler_domain = bundle.domain
    skills_dirs = request.skills_dirs
    run_environment = request.run_environment
    agent_backend = request.agent_backend
    cli_provider = request.cli_provider
    backend = request.backend
    environment_hooks = resolve_domain(bundle.domain).environment_hooks
    remote_repo = request.remote_repo
    repo_visibility = request.repo_visibility
    round_transaction_factory = _round_transaction_for_setup(setup)
    build_default_agent_client = setup.use_default_agent
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
            if copied_project:
                assert collection_root is not None  # noqa: S101  # tracked: #288
                project_root = collection_root / run_id
            else:
                project_root = input_dir
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
            backend_impl = backends.get(
                backend,
                log_dir=Project.log_directory_for(project_root, run_id),
                log=buffered_logs.append,
                image=environment.backend_image,
            )
            resolved_backend = str(agent_backend or config.agent.backend or AgentBackend.CLI)
            resolved_cli_provider = cli_provider or config.agent.cli_provider or "codex"
            model_name = config.model.name
            agent_spec = (
                agent_spec_from_config(
                    config,
                    backend=agent_backend,
                    provider=cli_provider,
                    model=model_name,
                )
                if build_default_agent_client
                else None
            )
        with boot_trace.span("profiler_preflight"):
            resolved_profiler_kind = resolve_profiler_kind(
                profiler_kind,
                domain=profiler_domain,
                backend_profiler_kind=getattr(backend_impl, "profiler_kind", None),
                environment_default_profiler_kind=environment.default_profiler_kind,
                environment_supported_profiler_kinds=environment.supported_profiler_kinds,
            )
            driver_supports_mcp = (
                agent_driver_supports_mcp_servers(agent_spec) if agent_spec is not None else None
            )
            if resolved_profiler_kind in ACTIVE_PROFILER_KINDS and driver_supports_mcp is False:
                driver_name = resolve_agent_driver(config)
                definition = profiler_definition(resolved_profiler_kind)
                raise ConfigurationError(
                    ConfigurationDiagnostic(
                        code="agent_profiler_incompatible",
                        stage="agent_capability_validation",
                        message=(
                            f"Profiler {resolved_profiler_kind.value!r} requires session MCP server "
                            f"{definition.mcp_name!r}, but agent driver {driver_name.value!r} does not "
                            "support session MCP servers. Select agent.driver='agentshim' or "
                            "disable profiling with --profiler none."
                        ),
                    )
                )
            profiler_preflight = preflight_profiler_kind(resolved_profiler_kind)
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
                feature_flags={
                    flag.value: enabled for flag, enabled in config.feature_flags.items()
                },
                skills_dirs=[str(path) for path in skill_source_paths],
            )

        with boot_trace.span("workspace_materialize"):
            profiler_support_path: str | None = None
            profiler_support_name: str | None = None
            if resolved_profiler_kind in ACTIVE_PROFILER_KINDS:
                definition = profiler_definition(resolved_profiler_kind)
                profiler_support_name = definition.support_name
                default_support = profiler_support_dir(definition.kind.value)
                if default_support is not None:
                    profiler_support_path = str(default_support)

            input_project_dir = input_dir if (input_dir / "pyproject.toml").is_file() else None

            hooks = environment_hooks or NoopEnvironmentHooks()
            hook_log: list[Callable[[str], None]] = [buffered_logs.append]
            environment_context: EnvironmentContext | None = None
            environment_patch: EnvironmentPatch | None = None

            def _teardown_environment_hooks() -> None:
                assert environment_context is not None  # noqa: S101  # tracked: #288
                try:
                    hooks.teardown(environment_context)
                except Exception as exc:  # noqa: BLE001  # tracked: #288
                    hook_log[0](f"[warn] environment hook teardown failed: {exc}")

            workspace_files = Workspace(
                project_root,
                run_environment=environment,
                backend=backend_impl,
                log=buffered_logs.append,
                project_root=PROJECT_ROOT,
                compute_backend=backend,
            )
            construction_complete = False
            if copied_project:
                assert collection_root is not None  # noqa: S101  # tracked: #288

                def _remove_incomplete_project() -> None:
                    if not construction_complete and project_root.exists():
                        shutil.rmtree(project_root)

                teardown_stack.callback(_remove_incomplete_project)
                source_reference = (task_root or input_dir) / "reference"
                environment_context = EnvironmentContext(
                    reference_path=source_reference,
                    workspace=project_root,
                    run_environment=environment,
                    project_root=PROJECT_ROOT,
                    model_cache_dir=collection_root / ".cache" / "huggingface",
                    runtime_artifact_dir=(
                        source_reference
                        if task_name is None
                        else collection_root / ".cache" / "llm-serving" / run_id
                    ),
                    log=buffered_logs.append,
                )
                environment_patch = hooks.prepare(environment_context)
                teardown_stack.callback(_teardown_environment_hooks)
                provision_project(
                    input_dir,
                    project_root,
                    spec=ProjectProvisioningSpec(
                        workspace=workspace_files,
                        workspace_sources=workspace_sources,
                        evaluator_source=evaluator_source,
                        task_name=task_name,
                        input_project_dir=input_project_dir,
                        input_excludes=environment_patch.copy_excludes,
                    ),
                )
                if evaluator_source is not None:
                    evaluator_source = project_root / "_evaluator" / evaluator_source.name
            else:
                workspace_files.create()

        with boot_trace.span("project_open"):
            project = Project.open(project_root)
            project_state = project.state
            log_dir = project_state.log_directory(run_id)
            log_dir.mkdir(parents=True, exist_ok=True)
            evaluator_tools_root = None
            evaluator_tool_roots: tuple[Path, ...] = ()
            if evaluator_package_root is not None:
                evaluator_tools = load_evaluator_package(evaluator_package_root).metadata.tools
                if evaluator_tools:
                    evaluator_tools_root = project_state.model_cache_directory("evaluator-tools")
                    evaluator_tool_roots = tuple(
                        tool_install_root(evaluator_tools_root, name, spec)
                        for name, spec in evaluator_tools.items()
                    )
        with boot_trace.span("log_bootstrap"):
            integration.attach(log_dir)
            logger = RunLogger(log_dir, emit=log_and_print)
            teardown_stack.callback(logger.close)
            # Registered after logger.close so LIFO teardown unsubscribes the
            # renderer before the log file closes.
            teardown_stack.callback(output_sink().subscribe(RunLogRenderer(logger.writer).handle))
            hook_log[0] = logger.lprint
            for message in buffered_logs:
                logger.lprint(message)

        paths = RunPaths(
            project_root=project_root,
            log_dir=log_dir,
            run_log_path=logger.path,
        )
        if existing:
            with boot_trace.span("workspace_repair"):
                workspace_files.repair()

        with boot_trace.span("git_tracker_init"):
            project_excluded_dirs = set(workspace_files.excluded_dirs)
            if profiler_support_name is not None:
                project_excluded_dirs.add(profiler_support_name)
            git = GitTracker(
                project_root,
                run_id=run_id,
                events=CoreGitTrackerEvents(),
                excluded_dirs=project_excluded_dirs,
                trusted_input_paths=trusted_project_input_paths(
                    project_root,
                    evaluator_source=evaluator_source,
                ),
            )
            git.init(existing, trusted_input_baseline=trusted_input_baseline)
        with boot_trace.span("project_state_resume"):
            effective_orchestration = orchestration_descriptor
            round_transaction_coordinator: (
                RoundTransactionCoordinator | MultiSlotRoundTransactionCoordinator | None
            ) = None
            if existing:
                project_state.load_project()
                run_manifest = project_state.load_run(run_id)
                if git.trusted_input_baseline is None:
                    git.configure_trusted_input_baseline(run_manifest.trusted_input_baseline)
                elif git.trusted_input_baseline != run_manifest.trusted_input_baseline:
                    raise ConfigurationError(
                        ConfigurationDiagnostic(
                            code="project_trusted_baseline_mismatch",
                            stage="resume_resolution",
                            message=(
                                f"run {run_id!r} records trusted input baseline "
                                f"{run_manifest.trusted_input_baseline!r}, but the requested "
                                f"baseline resolves to {git.trusted_input_baseline!r}"
                            ),
                        )
                    )
                if run_manifest.branch != git.project_branch:
                    raise ConfigurationError(
                        ConfigurationDiagnostic(
                            code="project_state_mismatch",
                            stage="resume_resolution",
                            message=(
                                f"run {run_id!r} records branch {run_manifest.branch!r}, "
                                f"but Git selected {git.project_branch!r}"
                            ),
                        )
                    )
                if run_manifest.task_name != task_name:
                    raise ConfigurationError(
                        ConfigurationDiagnostic(
                            code="project_task_mismatch",
                            stage="resume_resolution",
                            message=(
                                f"run {run_id!r} records task {run_manifest.task_name!r}, "
                                f"but task {task_name!r} was selected"
                            ),
                        )
                    )
                decision = _resume_orchestration_decision(
                    run_manifest,
                    effective_orchestration,
                    run_environment_spec,
                    execution_record,
                    orchestration_resume,
                )
                if round_transaction_factory is not None:
                    round_transaction_coordinator = round_transaction_factory(project, git, run_id)
                    recovery = round_transaction_coordinator.recover()
                    if recovery is not RoundRecoveryOutcome.NO_TRANSACTION:
                        logger.lprint(f"[project] recovered round transaction: {recovery.value}")
                if setup.resume_recovery is not None:
                    if setup.state_namespace is None:
                        raise TypeError("resume recovery requires a state namespace")  # noqa: TRY003
                    setup.resume_recovery(
                        RecoveryWorkspace(
                            project,
                            git,
                            project.state.portable_namespace(run_id, setup.state_namespace),
                        )
                    )
                if decision.descriptor is not None:
                    if decision.requires_clean_workspace:
                        pending = git.pending_changes()
                        if pending:
                            raise ConfigurationError(
                                ConfigurationDiagnostic(
                                    code="project_resume_configuration_dirty",
                                    stage="resume_resolution",
                                    message=(
                                        "commit or discard pending project changes before increasing "
                                        f"the run limit: {', '.join(pending)}"
                                    ),
                                )
                            )
                    project_state.update_run_orchestration(run_id, decision.descriptor)
                    snapshot = project_state.run_manifest_snapshot(run_id)
                    if decision.requires_clean_workspace:
                        git.snapshot_with_framework_metadata(
                            "vibesys: update run orchestration", snapshot
                        )
                    else:
                        git.snapshot_framework_metadata_only(
                            "vibesys: migrate run orchestration", snapshot
                        )
                project_state.set_current_run(run_id)
            else:
                project_state.create_project(project_root.name)
                if git.trusted_input_baseline is None:
                    raise ConfigurationError(
                        ConfigurationDiagnostic(
                            code="project_trusted_baseline_missing",
                            stage="workspace_setup",
                            message="Git did not provide the project run branch-point commit",
                        )
                    )
                run_manifest = project_state.new_run_manifest(
                    exp_name,
                    task_name=task_name,
                    run_id=run_id,
                    branch=git.project_branch,
                    vibesys_version=_installed_vibesys_version(),
                    run_environment=run_environment_record(run_environment_spec),
                    execution=execution_record,
                    orchestration=effective_orchestration,
                    trusted_input_baseline=git.trusted_input_baseline,
                )
                project_state.create_run(run_manifest)
                git.snapshot_with_framework_metadata(
                    f"vibesys: initialize run {run_id}",
                    project_state.initialization_snapshot(run_id),
                )

        with boot_trace.span("round_transaction_recovery"):
            if not existing and round_transaction_factory is not None:
                round_transaction_coordinator = round_transaction_factory(project, git, run_id)

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

            if environment_context is None:
                environment_context = EnvironmentContext(
                    reference_path=project_ref_dir,
                    workspace=project_root,
                    run_environment=environment,
                    project_root=PROJECT_ROOT,
                    model_cache_dir=project_state.model_cache_directory("huggingface"),
                    runtime_artifact_dir=project_state.model_cache_directory("llm-serving"),
                    log=logger.lprint,
                )
                environment_patch = hooks.prepare(environment_context)
                teardown_stack.callback(_teardown_environment_hooks)
            assert environment_patch is not None  # noqa: S101  # tracked: #288

            plan = workspace_files.plan_setup(
                existing=True,
                input_dir=project_root,
                evaluator_source=None,
                skill_sources=skill_source_paths,
                input_project_dir=None,
                profiler_support_path=profiler_support_path,
                profiler_support_name=profiler_support_name,
                workspace_sources=(),
                extra_input_excludes=environment_patch.copy_excludes,
            )
            workspace_files.setup(plan, existing=True)

        with boot_trace.span("environment_open"):
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

            tracked_experiment_repository: ExperimentRepository | None = None
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
                tracked_experiment_repository = experiment_repository

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
                workspace_sources=(),
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
                evaluator_package_root=evaluator_package_root,
                evaluator_tools_root=evaluator_tools_root,
                profiler_support_path=profiler_support_path,
                profiler_support_name=profiler_support_name,
                git_history_root=git.history_root,
                environment_bind_mounts=environment_patch.bind_mounts,
                log=logger.lprint,
                framework_root=PROJECT_ROOT,
                project_path_policy=project_path_policy,
                state_namespace=project_state.local_namespace(run_id, "skypilot"),
            )
            session = teardown_stack.enter_context(environment.open(run_environment_request))
        with boot_trace.span("device_monitor_start"):
            # Snapshot the agent-facing commands once the session is open; the
            # view's paths are fixed for the session lifetime.
            commands = RunCommands(
                judge_accuracy_command=session.view.paths.accuracy_command,
                judge_benchmark_command=session.view.paths.benchmark_command,
                profiler_support_agent_path=session.view.paths.profiler_support,
                profiler_benchmark_command=session.view.paths.benchmark_command,
            )

            # Start backend-specific background monitoring (CUDA: nvidia-smi).
            device = DeviceLease(backend_impl, log_dir=log_dir, run_environment_view=session.view)
            teardown_stack.callback(device.close)
            device.start_monitor()

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

        run_state = RunState(project, git, run_id)

        agent_client: AgentClientProtocol | None = None
        if build_default_agent_client:
            if agent_spec is None:
                raise _MissingDefaultAgentSpecError
            with boot_trace.span("agent_client_build"):
                # Provider session IDs for built-in roles remain machine-local.
                agent_session_store = DurableSessionStore(
                    run_state.local(RunStateNamespace.AGENT).slot(
                        "sessions.json", AgentSessionState
                    ),
                    log=logger.lprint,
                )
                # Existing loops share this client and its role sandbox map.
                agent_client = build_agent_client(
                    spec=agent_spec,
                    session_store=agent_session_store,
                    backends={
                        "implementer": session.sandbox,
                        "judge": session.sandbox,
                        "perf_eval": session.sandbox,
                        "profiler": session.sandbox,
                        "orchestrator": session.sandbox,
                    },
                    skill_source_dirs=skill_source_paths,
                    skill_selection=platform_skill_selection(backend),
                    run_log_file=logger.writer,
                    use_docker=session.view.cli_sandboxed,
                    log_dir=log_dir,
                    project_path_policy=project_path_policy,
                    require_host_sandbox=not session.view.cli_sandboxed,
                    host_resources=agent_host_resources,
                    events=output_sink(),
                )
            teardown_stack.callback(agent_client.close)

        result = _RunContext(
            backend=backend,
            run_environment=environment,
            integration=integration,
            logger=logger,
            paths=paths,
            debug=debug,
            backend_impl=backend_impl,
            model_name=model_name,
            input_path=input_path_str,
            workspace_sources=(),
            evaluator_path=evaluator_source,
            evaluator_package_root=evaluator_package_root,
            evaluator_tools_root=evaluator_tools_root,
            evaluator_tool_roots=evaluator_tool_roots,
            effective_objective=objective,
            accuracy_command=accuracy_command,
            benchmark_command=benchmark_command,
            profiler_kind=resolved_profiler_kind,
            profiler_support_path=profiler_support_path,
            profiler_support_name=profiler_support_name,
            skill_source_paths=skill_source_paths,
            ref_name=ref_name,
            environment_hooks=hooks,
            environment_context=environment_context,
            environment_patch=environment_patch,
            workspace_files=workspace_files,
            git=git,
            experiment_repository=tracked_experiment_repository,
            teardown_stack=teardown_stack,
            environment_request=run_environment_request,
            run_environment_session=session,
            commands=commands,
            device=device,
            agent_client=agent_client,
            project=project,
            state=run_state,
            run_id=run_id,
            round_transaction_coordinator=round_transaction_coordinator,
            agent_host_resources=agent_host_resources,
        )
        integration.publish_resources(
            RunResourceHandoff(
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
                run_environment_sandboxed=session.view.cli_sandboxed,
                project_path_policy=project_path_policy,
                host_resources=agent_host_resources,
            )
        )
        construction_complete = True
    # Assembly's spans, including the enclosing one that just closed with the
    # total. The run log gets them in completion order: children, then parent.
    for line in boot_trace.drain_log_lines():
        logger.lprint(line)
    return result


@dataclass(frozen=True, slots=True)
class WorkspaceContextSpec:
    """One isolated workspace's identity and environment settings."""

    scope_id: str
    revision: str
    config: Config
    log_namespace: str = RunStateNamespace.RUNTIME
    log_directory: str = "workspaces"
    agent_backend: str | None = None
    cli_provider: str | None = None
    use_default_agent: bool = False


@dataclass(frozen=True, slots=True)
class ScopedAgentEnvironment:
    """One agent sandbox opened over an isolated workspace context."""

    session: RunEnvironmentSession
    skill_source_dirs: tuple[Path, ...]
    skill_selection: SkillSelection
    project_path_policy: ProjectPathPolicy
    host_resources: tuple[HostResource, ...]
    backends: dict[str, Sandbox] | None
    use_docker: bool

    def close(self) -> None:
        """Release this agent's environment session."""
        self.session.close()


def open_scoped_agent_environment(
    context: "_RunContext",
    *,
    mounts: tuple[HostResource, ...] = (),
    agent_backend: str | None = None,
    cli_provider: str | None = None,
) -> ScopedAgentEnvironment:
    """Open an independently configured agent in this context's workspace."""
    base = context.environment_request
    request = replace(
        base,
        agent_backend=agent_backend if agent_backend is not None else base.agent_backend,
        cli_provider=cli_provider if cli_provider is not None else base.cli_provider,
        environment_bind_mounts=(
            *base.environment_bind_mounts,
            *(
                EnvironmentBindMount(
                    mount.path,
                    mount.agent_path if mount.agent_path is not None else str(mount.path),
                    read_only=mount.access is HostResourceAccess.READ_ONLY,
                )
                for mount in mounts
            ),
        ),
    )
    session = context.run_environment.open(request)
    sandboxed = session.view.cli_sandboxed
    return ScopedAgentEnvironment(
        session=session,
        skill_source_dirs=tuple(context.skill_source_paths),
        skill_selection=platform_skill_selection(context.backend),
        project_path_policy=request.project_path_policy,
        host_resources=context.agent_host_resources,
        backends={"chat": session.sandbox} if sandboxed else None,
        use_docker=session.view.cli_sandboxed if sandboxed else False,
    )


def create_workspace_context(parent: "_RunContext", spec: WorkspaceContextSpec) -> "_RunContext":
    """Open an isolated Git tree, run environment, and optional agent client."""
    teardown_stack = ExitStack()
    try:
        return _assemble_workspace_context(teardown_stack=teardown_stack, parent=parent, spec=spec)
    except BaseException as construction_error:
        _close_after_construction_failure(teardown_stack, construction_error)
        raise


def create_candidate_context(  # noqa: PLR0913  # tracked: #288
    parent: "_RunContext",
    *,
    config: Config,
    generation: int,
    child_idx: int,
    parent_commit: str,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
) -> "_RunContext":
    """Build an isolated sub-context for evaluating one candidate concurrently.

    The sub-context shares the parent run's identity, model, compute backend,
    run-environment policy, and — crucially — the parent workspace's **git
    object store**, so a candidate's commit lands in the one evolutionary
    lineage. Everything that would collide under concurrency is its own:

    - a **git worktree** checked out at ``parent_commit`` (isolated working
      tree / index / detached HEAD; edits never touch the shared tree);
    - a fresh **run-environment session** (its own isolated editor sandbox);
    - its own **agent client** (the CLI session is not thread-safe);
    - a **no-tee ``RunLogger``** writing only to the candidate's log file — only
      the top-level run logger may own the process ``sys.stderr``.

    The caller gates this path on the selected environment's parallel-candidate
    capability. Close the returned context (or use it as a context manager) to
    stop environment-owned resources and remove the worktree.
    """
    return create_workspace_context(
        parent,
        WorkspaceContextSpec(
            scope_id=f"g{generation}c{child_idx}",
            revision=parent_commit,
            config=config,
            log_namespace=RunStateNamespace.EVOLVE,
            log_directory="candidates",
            agent_backend=agent_backend,
            cli_provider=cli_provider,
            use_default_agent=True,
        ),
    )


def _assemble_workspace_context(
    *,
    teardown_stack: ExitStack,
    parent: "_RunContext",
    spec: WorkspaceContextSpec,
) -> "_RunContext":
    config = as_config(spec.config)
    workspace = parent.project.state.candidate_worktree_directory(parent.run_id, spec.scope_id)
    log_dir = parent.state.local(spec.log_namespace).external_directory(
        f"{spec.log_directory}/{spec.scope_id}/logs"
    )

    # Materialize the parent's tree in an isolated worktree (shared object
    # store). `git worktree add` touches the main repo's admin area, so the
    # caller serializes this; the container/agent work afterward is isolated.
    # Remove the worktree only after it has been materialized, including when
    # git itself reports failure after partially materializing its admin state.
    teardown_stack.callback(lambda: parent.git.remove_worktree(workspace))
    parent.git.add_worktree(workspace, spec.revision)

    logger = RunLogger(log_dir, tee_stderr=False, emit=log_and_print)
    teardown_stack.callback(logger.close)

    resolved_backend = str(spec.agent_backend or config.agent.backend or AgentBackend.CLI)
    resolved_cli_provider = spec.cli_provider or config.agent.cli_provider or "codex"
    effective_objective = getattr(parent, "effective_objective", None)

    git = GitTracker(
        workspace,
        run_id=parent.run_id,
        events=CoreGitTrackerEvents(),
        excluded_dirs=parent.EXCLUDED_WORKSPACE_DIRS,
        trusted_input_paths=trusted_project_input_paths(
            workspace,
            evaluator_source=None,
        ),
    )
    if parent.git.trusted_input_baseline is not None:
        git.configure_trusted_input_baseline(parent.git.trusted_input_baseline)
    workspace_files = Workspace(
        workspace,
        run_environment=parent.run_environment,
        backend=parent.backend_impl,
        log=logger.lprint,
        project_root=PROJECT_ROOT,
        compute_backend=parent.backend,
    )
    project_path_policy = build_project_path_policy(workspace, evaluator_source=None)
    objective_document = None
    if effective_objective is not None:
        objective_document = parent.state.portable(
            RunStateNamespace.RUNTIME
        ).equivalent_external_file(workspace, "effective-objective.md")

    # Reuse adapter-owned resources provisioned when the parent environment was
    # opened. Candidate sessions do not need to rematerialize reference inputs.
    workspace_environment_request = RunEnvironmentRequest(
        log_dir=log_dir,
        workspace=workspace,
        workspace_sources=parent.workspace_sources,
        ref_dir=None,
        backend=parent.backend_impl,
        agent_backend=resolved_backend,
        cli_provider=resolved_cli_provider,
        run_id=parent.run_id,
        objective=effective_objective,
        objective_document=objective_document,
        accuracy_command=parent.accuracy_command,
        benchmark_command=parent.benchmark_command,
        benchmark_output_argument=parent.environment_request.benchmark_output_argument,
        evaluator_package_root=parent.evaluator_package_root,
        evaluator_tools_root=parent.evaluator_tools_root,
        profiler_support_path=parent.profiler_support_path,
        profiler_support_name=parent.profiler_support_name,
        git_history_root=parent.git.history_root,
        environment_bind_mounts=parent.environment_patch.bind_mounts,
        log=logger.lprint,
        framework_root=PROJECT_ROOT,
        project_path_policy=project_path_policy,
        state_namespace=parent.state.local(RunStateNamespace.SKYPILOT),
    )
    session = teardown_stack.enter_context(
        parent.run_environment.open(workspace_environment_request)
    )
    commands = RunCommands(
        judge_accuracy_command=session.view.paths.accuracy_command,
        judge_benchmark_command=session.view.paths.benchmark_command,
        profiler_support_agent_path=session.view.paths.profiler_support,
        profiler_benchmark_command=session.view.paths.benchmark_command,
    )

    # No session store: an evolve candidate names no conversation narrower than
    # an agent role, so every one of its turns lands on a role-scoped key, and
    # role-scoped conversations are never checkpointed (they belong to one
    # process). Candidates also share the parent's run ID and local namespace
    # and run concurrently, so a single per-run map would alias them anyway.
    agent_client = None
    if spec.use_default_agent:
        agent_spec = agent_spec_from_config(
            config,
            backend=spec.agent_backend,
            provider=spec.cli_provider,
            model=parent.model_name,
        )
        agent_client = build_agent_client(
            spec=agent_spec,
            backends={
                "implementer": session.sandbox,
                "judge": session.sandbox,
                "perf_eval": session.sandbox,
                "profiler": session.sandbox,
                "orchestrator": session.sandbox,
            },
            skill_source_dirs=parent.skill_source_paths,
            skill_selection=platform_skill_selection(parent.backend),
            run_log_file=logger.writer,
            use_docker=session.view.cli_sandboxed,
            log_dir=log_dir,
            project_path_policy=project_path_policy,
            require_host_sandbox=not session.view.cli_sandboxed,
            host_resources=parent.agent_host_resources,
            events=output_sink(),
        )
        teardown_stack.callback(agent_client.close)

    paths = RunPaths(
        project_root=workspace,
        log_dir=log_dir,
        run_log_path=logger.path,
    )

    return _RunContext(
        backend=parent.backend,
        run_environment=parent.run_environment,
        integration=parent.integration,
        logger=logger,
        paths=paths,
        debug=parent.debug,
        backend_impl=parent.backend_impl,
        model_name=parent.model_name,
        input_path=parent.input_path,
        workspace_sources=parent.workspace_sources,
        evaluator_path=parent.evaluator_path,
        evaluator_package_root=parent.evaluator_package_root,
        evaluator_tools_root=parent.evaluator_tools_root,
        evaluator_tool_roots=parent.evaluator_tool_roots,
        effective_objective=effective_objective,
        accuracy_command=parent.accuracy_command,
        benchmark_command=parent.benchmark_command,
        profiler_kind=parent.profiler_kind,
        profiler_support_path=parent.profiler_support_path,
        profiler_support_name=parent.profiler_support_name,
        skill_source_paths=parent.skill_source_paths,
        ref_name=parent.ref_name,
        environment_hooks=parent.environment_hooks,
        environment_context=parent.environment_context,
        environment_patch=parent.environment_patch,
        workspace_files=workspace_files,
        git=git,
        # Candidate worktrees share the parent repository and may run in
        # parallel. Only the parent context owns remote synchronization.
        experiment_repository=None,
        teardown_stack=teardown_stack,
        environment_request=workspace_environment_request,
        run_environment_session=session,
        commands=commands,
        device=parent.device,  # shared under the environment's parallel contract
        agent_client=agent_client,
        project=parent.project,
        state=parent.state,
        run_id=parent.run_id,
        agent_host_resources=parent.agent_host_resources,
    )


class _RunContext:
    """Experiment lifecycle owner shared by simple, orchestrate, and issue loops.

    ``_RunContext`` sits above the run-environment abstraction:

        loop -> _RunContext -> RunEnvironment -> ComputeBackendImpl.make_sandbox -> Sandbox

    Instances are assembled by :func:`open_run_context`, which owns every
    construction side effect (project state, log files, candidate worktree,
    model, compute backend, copied helper inputs, Git snapshot tracking,
    run-environment session, agent client, GPU monitor). Environment-specific
    setup should stay in ``vibesys.sandbox.run_environment``; this class only
    asks the selected run environment for policy decisions and the opened
    sandbox session.
    """

    def __init__(  # noqa: ANN204, PLR0913  # tracked: #288
        self,
        *,
        backend: ComputeBackend,
        run_environment: RunEnvironment,
        integration: LocalRunIntegration,
        logger: RunLogger,
        paths: RunPaths,
        debug: bool,
        backend_impl: ComputeBackendImpl,
        model_name: str,
        input_path: str | None,
        workspace_sources: tuple[WorkspaceSource, ...],
        evaluator_path: Path | None,
        evaluator_package_root: Path | None,
        evaluator_tools_root: Path | None,
        evaluator_tool_roots: tuple[Path, ...],
        effective_objective: str | None,
        accuracy_command: str,
        benchmark_command: str,
        profiler_kind: ProfilerKind,
        profiler_support_path: str | None,
        profiler_support_name: str | None,
        skill_source_paths: list[Path],
        ref_name: str,
        environment_hooks: EnvironmentHooks,
        environment_context: EnvironmentContext,
        environment_patch: EnvironmentPatch,
        workspace_files: Workspace,
        git: GitTracker,
        experiment_repository: ExperimentRepository | None,
        teardown_stack: ExitStack,
        environment_request: RunEnvironmentRequest,
        run_environment_session: RunEnvironmentSession,
        commands: RunCommands,
        device: DeviceLease,
        agent_client: AgentClientProtocol | None,
        project: Project,
        state: RunState,
        run_id: str,
        round_transaction_coordinator: (
            RoundTransactionCoordinator | MultiSlotRoundTransactionCoordinator | None
        ) = None,
        agent_host_resources: tuple[HostResource, ...] = (),
    ):
        self.backend = backend
        # Retained so a candidate sub-context can hand its own agent runner the
        # same declarations the parent computed, rather than recomputing them
        # from state a candidate context does not carry.
        self.agent_host_resources = agent_host_resources
        self.run_environment = run_environment
        self.integration = integration
        self.events = integration.events
        self.logger = logger
        self._paths = paths
        self.debug = debug
        self.backend_impl = backend_impl
        self.model_name = model_name
        self.input_path = input_path
        self.workspace_sources = workspace_sources
        self.evaluator_path = evaluator_path
        self.evaluator_package_root = evaluator_package_root
        self.evaluator_tools_root = evaluator_tools_root
        self.evaluator_tool_roots = evaluator_tool_roots
        self.effective_objective = effective_objective
        self.accuracy_command = accuracy_command
        self.benchmark_command = benchmark_command
        self.profiler_kind = profiler_kind
        self.profiler_support_path = profiler_support_path
        self.profiler_support_name = profiler_support_name
        self._skill_source_paths = skill_source_paths
        self.skills_for_agents = [src.name for src in skill_source_paths]
        self.ref_name = ref_name
        self.environment_hooks = environment_hooks
        self.environment_context = environment_context
        self.environment_patch = environment_patch
        self.workspace_files = workspace_files
        self.EXCLUDED_WORKSPACE_DIRS = workspace_files.excluded_dirs
        self.git = git
        self.project = project
        self.state = state
        self.run_id = run_id
        self._round_transaction_coordinator = round_transaction_coordinator
        self._pending_round_transaction: RoundTransaction | None = None
        self._experiment_repository = experiment_repository
        self._teardown_stack = teardown_stack
        self.environment_request = environment_request
        self.run_environment_session = run_environment_session
        self.run_environment_view = run_environment_session.view
        self.implementer_backend = run_environment_session.sandbox
        self.judge_backend = run_environment_session.sandbox
        self.commands = commands
        self.device = device
        # Expose the picked device for legacy callers (gpu monitor tests etc).
        self.selected_gpu = device.selected_device
        self._agent_client = agent_client
        self._closed = False
        self._progress_stack: list[AgentProgress] = []

    # -- path passthroughs ----------------------------------------------------
    # Canonical values live in the frozen ``RunPaths`` record.

    @property
    def project_root(self) -> Path:
        return self._paths.project_root

    @property
    def agent_client(self) -> AgentClientProtocol:
        """Return the built-in loop client when this context has one."""
        if self._agent_client is None:
            raise _MissingDefaultAgentClientError
        return self._agent_client

    @agent_client.setter
    def agent_client(self, client: AgentClientProtocol) -> None:
        self._agent_client = client

    @property
    def log_dir(self) -> Path:
        return self._paths.log_dir

    @property
    def workspace(self) -> Path:
        return self._paths.workspace

    def begin_completed_round(
        self,
        round_number: int,
        *,
        state_transition: StateTransition,
    ) -> None:
        """Journal a completed project round before mutating local state."""
        if self._round_transaction_coordinator is None:
            return
        if isinstance(self._round_transaction_coordinator, MultiSlotRoundTransactionCoordinator):
            raise TypeError
        if self._pending_round_transaction is not None:
            raise RuntimeError("a completed-round transaction is already active")  # noqa: TRY003  # tracked: #288
        self._pending_round_transaction = self._round_transaction_coordinator.begin(
            round_number,
            state_transition=state_transition,
        )

    def persist_completed_round(self) -> None:
        """Commit one completed project round and its active-state transition."""
        if self._round_transaction_coordinator is None:
            return
        transaction = self._pending_round_transaction
        if transaction is None:
            raise RuntimeError("begin_completed_round must precede project round persistence")  # noqa: TRY003  # tracked: #288
        transaction.complete()
        self._pending_round_transaction = None

    def publish_committed_state(
        self,
        namespace: str,
        state: BaseModel,
        *,
        changed_keys: tuple[str, ...] | None = None,
    ) -> None:
        """Publish an in-memory hint only after the same state is durable."""
        self.integration.publish_committed_state(
            namespace,
            state,
            changed_keys=changed_keys,
        )

    @property
    def run_log_path(self) -> Path:
        return self._paths.run_log_path

    @property
    def run_log_file(self) -> TextIO:
        """The current open log file handle (owned by ``RunLogger``)."""
        return self.logger.writer

    @property
    def skill_source_paths(self) -> list[Path]:
        """Skill source directories copied into the workspace for agents."""
        return self._skill_source_paths

    @property
    def gpu_monitor(self) -> "ContentionMonitor | None":
        """The active device monitor (owned by ``DeviceLease``)."""
        return self.device.monitor

    @gpu_monitor.setter
    def gpu_monitor(self, monitor: "ContentionMonitor | None") -> None:
        self.device.monitor = monitor

    def gpu_env(self) -> dict[str, str]:
        """Env vars for the CLI agent, empty when that agent runs in a container.

        The pin names a *host* device index (see :meth:`DeviceLease.gpu_env`).
        An editor container is started with ``--gpus device=N``, so inside it
        the selected GPU is device 0 and the container env already says so;
        forwarding the host index there would point the agent at a device the
        container cannot see. Keeping the pin out of the session spec also
        keeps a mid-run device reselect from changing the session fingerprint
        and evicting the live conversation.
        """
        if self.agent_client.capabilities.container_execution:
            return {}
        return self.device.gpu_env()

    @contextmanager
    def progress(self, progress: AgentProgress) -> Generator[None]:
        """Temporarily attach loop progress to agent invocations in this context."""
        self._progress_stack.append(progress)
        try:
            yield
        finally:
            self._progress_stack.pop()

    def current_progress(self) -> AgentProgress | None:
        """Return the active loop progress, if a loop has scoped one."""
        if not self._progress_stack:
            return None
        return self._progress_stack[-1]

    def invoke(  # noqa: PLR0913  # tracked: #288
        self,
        *,
        kind: str,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str = "",
        progress: AgentProgress | None = None,
        **extra: Any,  # noqa: ANN401  # tracked: #288
    ) -> T:
        """Invoke an agent through ``self.agent_client`` with workspace+env defaults.

        Wraps ``self.agent_client.invoke(...)`` so the per-call boilerplate
        (``workspace=self.workspace``, ``env=self.gpu_env()``) doesn't have
        to be repeated at every call site.  Extra kwargs are forwarded to
        ``agent_client.invoke`` unchanged so loop-specific options
        (e.g. ``iteration=`` for plain-loop runner extensions) still work.
        """
        client = self.agent_client
        driver = client.driver_name
        provider = client.provider
        model = client.model_for_kind(kind)
        control = self.integration.control
        control.raise_if_stopped()
        control.wait_while_paused()
        steer_texts = control.take_pending_steer()
        user_prompt = splice_steering(user_prompt, steer_texts)
        execution_id = uuid.uuid4().hex
        if steer_texts:
            control.notify_steer_consumed(
                agent_kind=kind, round_label=round_label, execution_id=execution_id
            )
        event_fields: dict[str, Any] = {
            "agent_kind": kind,
            "round_label": round_label,
            "execution_id": execution_id,
        }
        attempt = _attempt_from_label(round_label)
        self.events.emit(
            CoreEventType.AGENT_EXECUTION_STARTED,
            status=EventStatus.ACTIVE,
            data=AgentExecutionStartedData(
                stage=kind,
                attempt=attempt,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                activity=AgentExecutionActivityData(
                    mode="thinking",
                    summary=f"{kind.replace('_', ' ').title()} is working",
                ),
                driver=driver,
                provider=provider,
                model=model,
            ),
            **event_fields,
        )
        self.events.emit(
            CoreEventType.PHASE_STARTED,
            status=EventStatus.ACTIVE,
            data=PhaseData(phase=kind, attempt=attempt),
            **event_fields,
        )
        self.events.emit(
            CoreEventType.INVOCATION_STARTED,
            status=EventStatus.ACTIVE,
            data=InvocationStartedData(system_prompt=system_prompt, user_prompt=user_prompt),
            **event_fields,
        )
        result: T | None = None
        error: BaseException | None = None
        try:
            result = self.agent_client.invoke(
                kind=kind,
                workspace=self.workspace,
                system_prompt=system_prompt,
                env=self.gpu_env(),
                user_prompt=user_prompt,
                response_cls=response_cls,
                fallback_factory=fallback_factory,
                round_label=round_label,
                invocation_id=execution_id,
                progress=progress if progress is not None else self.current_progress(),
                **extra,
            )
            return result  # noqa: RET504, TRY300  # tracked: #288
        except BaseException as exc:
            error = exc
            raise
        finally:
            status = _execution_status(error)
            error_text = f"{type(error).__name__}: {error}" if error is not None else None
            self.events.emit(
                CoreEventType.AGENT_EXECUTION_FINISHED,
                status=status,
                data=AgentExecutionFinishedData(result=json_value(result), error=error_text),
                **event_fields,
            )
            self.events.emit(
                CoreEventType.INVOCATION_FINISHED,
                status=status,
                data=InvocationFinishedData(result=json_value(result), error=error_text),
                **event_fields,
            )
            self.events.emit(
                CoreEventType.PHASE_FINISHED,
                status=status,
                data=PhaseData(phase=kind, attempt=attempt),
                **event_fields,
            )

    def wait_for_debug(self, step: str) -> None:
        if self.debug:
            input(f"\n[debug] {step}. Press Enter to continue...")

    def snapshot_workspace(self, label: str) -> None:
        self.git.snapshot(label)

    def trusted_input_changes(self) -> list[str]:
        """Return evaluator-owned paths changed since the trusted baseline."""
        return self.git.trusted_input_changes()

    # -- command passthroughs -------------------------------------------------
    # Canonical values live in the frozen ``RunCommands`` snapshot; these
    # properties keep existing ``ctx.judge_accuracy_command``-style call
    # sites working.

    @property
    def objective_location(self) -> str:
        """Return the framework-owned effective objective path seen by agents."""
        return self.run_environment_view.paths.objective

    @property
    def judge_accuracy_command(self) -> str | None:
        """Return the accuracy command as seen by the judge agent."""
        return self.commands.judge_accuracy_command

    @property
    def judge_benchmark_command(self) -> str | None:
        """Return the benchmark command as seen by the judge agent."""
        return self.commands.judge_benchmark_command

    @property
    def profiler_support_agent_path(self) -> str | None:
        """Return the selected profiler support path as seen by its agent."""
        return self.commands.profiler_support_agent_path

    @property
    def profiler_benchmark_command(self) -> str | None:
        """Return the benchmark command as seen by the profiler agent."""
        return self.commands.profiler_benchmark_command

    def lprint(self, text: str) -> None:
        self.logger.lprint(text)

    def switch_log_file(self, label: int | str) -> None:
        """Switch to a per-phase log file — see :meth:`RunLogger.switch`."""
        self.logger.switch(label)
        self._paths = replace(self._paths, run_log_path=self.logger.path)
        # Update the agent client's log file handle so subsequent
        # invoke() calls write to the new step log.
        if self._agent_client is not None:
            self._agent_client.set_log_file(self.logger.writer)

    def reselect_gpu(self) -> None:
        """Delegate mid-run device rebalance — see :meth:`DeviceLease.reselect`."""
        self.device.reselect()
        # Mirror backend state on _RunContext for legacy callers/tests.
        self.selected_gpu = self.device.selected_device

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Unwinds in reverse construction order: device monitor stop,
        # environment teardown, run-environment exit, and log closure.
        self._teardown_stack.close()

    def __enter__(self) -> "_RunContext":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
