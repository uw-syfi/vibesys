"""Composition-only factories for production runtime infrastructure.

Orchestration plugins use :mod:`vs_runtime.api`, not this module. VibeSys
composition imports this factory to bind lower-library effects to the private
runtime implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Protocol

from vs_runtime._agent_execution import (
    AgentExecutionConfiguration,
    AgentExecutionEnvironment,
    AgentExecutionScope,
    ScopedAgentEnvironment,
    SharedAgentEnvironmentConflictError,
    open_agent_execution_environment,
)
from vs_runtime._agent_lifecycle import (
    AgentExecutionFinished,
    AgentExecutionLifecycleEvent,
    AgentExecutionLifecycleSink,
    AgentExecutionStarted,
    AgentExecutionStatus,
)
from vs_runtime._bounded_stop import (
    StopGraceError,
    StopTimer,
    bounded_stop,
    stop_gated_evaluation,
)
from vs_runtime._bundled_paths import (
    BundledResources,
    resolve_bundled_tree,
    resolve_packaged_tree,
)
from vs_runtime._capacity_gate import CapacityHandling, CapacityTimer, install_capacity_gate
from vs_runtime._container_runtime_policy import DockerInDockerUnsupportedError
from vs_runtime._docker_evaluator_tools import prepare_docker_evaluator_resources
from vs_runtime._environment_startup import (
    EnvironmentStartup,
    EnvironmentStartupSink,
    EnvironmentStartupStage,
)
from vs_runtime._environment_system import EnvironmentSystem
from vs_runtime._evaluator_packages import (
    PACKAGE_ROOT_TOKEN,
    TOOL_TOKEN_PREFIX,
    EvaluatorPackageError,
    EvaluatorPackageMetadata,
    EvaluatorPackageNotFoundError,
    EvaluatorPackageRequirement,
    ResolvedEvaluatorPackage,
    load_evaluator_package,
    resolve_evaluator_package,
)
from vs_runtime._event_journal import DurableEventJournal, EventCodec
from vs_runtime._failure_classification import (
    RecordState,
    TerminalSignal,
    classify,
    job_failure,
    signal_of,
)
from vs_runtime._fallback import FallbackTarget, ProviderFallback
from vs_runtime._image_features import RUN_ENVIRONMENT_FEATURES_PROJECT, image_features
from vs_runtime._input_project import InputDependency, materialize_input_project
from vs_runtime._job_tools import TOOLKIT_BIN_PATTERNS, find_job_tool, off_path_tools
from vs_runtime._linux_cpu_profiler import (
    Capability as LinuxProfilerCapability,
)
from vs_runtime._linux_cpu_profiler import (
    CollectionResult as LinuxProfileResult,
)
from vs_runtime._linux_cpu_profiler import (
    DiagnosticCode as LinuxProfilerDiagnostic,
)
from vs_runtime._linux_cpu_profiler import LinuxProfilerEffects, LinuxProfilerTool
from vs_runtime._linux_cpu_profiler import collect as collect_linux_profile
from vs_runtime._linux_cpu_profiler import detect_capability as detect_linux_profiler
from vs_runtime._linux_cpu_profiler import parse_command as parse_profile_command
from vs_runtime._linux_cpu_profiler import summarize as summarize_linux_profile
from vs_runtime._local_validation import (
    FrameworkValidationResult,
    LocalValidationEvents,
    LocalValidationRecipeError,
    LocalValidationRecipeErrorKind,
    LocalValidator,
    ValidationRecipe,
)
from vs_runtime._macos_cpu_profiler import (
    Capability as MacOSProfilerCapability,
)
from vs_runtime._macos_cpu_profiler import (
    CollectionResult as MacOSProfileResult,
)
from vs_runtime._macos_cpu_profiler import (
    DiagnosticCode as MacOSProfilerDiagnostic,
)
from vs_runtime._macos_cpu_profiler import MacOSProfilerEffects, MacOSProfilerTool
from vs_runtime._macos_cpu_profiler import collect as collect_macos_profile
from vs_runtime._macos_cpu_profiler import detect_capability as detect_macos_profiler
from vs_runtime._managed_conversation import (
    ManagedConversation,
    ManagedConversationSpec,
    create_managed_conversation,
    open_managed_conversation,
)
from vs_runtime._model_artifacts import (
    ModelArtifactDownloader,
    ModelArtifactRequest,
    PreparedModelArtifacts,
    prepare_model_artifacts,
)
from vs_runtime._model_requests import ModelRequestError, _ModelRequestReconciler
from vs_runtime._objective_document import materialize_objective_document
from vs_runtime._project_materialization import (
    FreshProjectError,
    FreshProjectErrorKind,
    GitSourceMaterialization,
    InputProjectMaterialization,
    ProjectMaterializationEffects,
    ProjectMaterializationStep,
    ProjectMaterializer,
    ProjectTreeCopy,
    WorkspaceSourceValue,
)
from vs_runtime._project_run import (
    ProjectRunBaselineMissingError,
    ProjectRunDirtyResumeError,
    ProjectRunEffects,
    ProjectRunError,
    ProjectRunMismatchError,
    ProjectRunMismatchKind,
    ProjectRunRequest,
    ProjectRunResources,
    open_project_run_resources,
)
from vs_runtime._quota_policy import (
    DEFAULT_RETRY_SECONDS,
    PAUSE_FOR_OPERATOR,
    RESET_MARGIN_SECONDS,
    GiveUp,
    Hold,
    QuotaAction,
    QuotaDecision,
    QuotaPolicy,
    Switch,
    WaitFor,
    decide_quota,
)
from vs_runtime._recorded_environment import migrate_recorded_run_environment
from vs_runtime._release import release_all
from vs_runtime._run_control import (
    RunControlChannel,
    RunControlEventSink,
    RunControlTransition,
    RunControlTransitionKind,
    RunStopped,
    RuntimeRunControlChannel,
)
from vs_runtime._run_environment import (
    AgentPaths,
    ClusterEvaluationFacts,
    DockerEnvironment,
    DockerEnvironmentConfig,
    DockerEnvironmentFacts,
    HostEnvironmentFacts,
    ModalEnvironmentFacts,
    ParallelCandidateObstacle,
    RunEnvironment,
    RunEnvironmentPresentation,
    RunEnvironmentRequest,
    RunEnvironmentResources,
    RunEnvironmentSession,
    RunEnvironmentSpec,
    RunEnvironmentView,
    SkyPilotEnvironment,
    SkyPilotEnvironmentFacts,
    SlurmEnvironmentFacts,
    open_run_environment_resources,
    open_workspace_environment_resources,
)
from vs_runtime._run_environment_selection import (
    HostEnvironment,
    build_run_environment,
    make_run_environment_spec,
    resolve_run_environment_spec,
    run_environment_deprecation,
    run_environment_record,
    validate_run_environment_profile,
)
from vs_runtime._run_host import (
    BlockingOperations,
    RunHostComponents,
    RuntimeRunHost,
    open_run_host,
)
from vs_runtime._run_state import RunState
from vs_runtime._sdk_paths import (
    InputProjectError,
    SDKRoots,
    relative_sdk_source,
    resolve_sdk_source,
)
from vs_runtime._semantic_slurm_evaluation import (
    SemanticSlurmEvaluationExecutor,
    StageFailureText,
)
from vs_runtime._skills import (
    SkillCatalogEntry,
    SkillMetadataError,
    build_skill_catalog,
    discover_skill_dirs,
    load_skill_frontmatter,
    offered_skill_facts,
)
from vs_runtime._slurm_environment import SlurmEnvironment
from vs_runtime._tool_location import ToolLocation, ToolSite
from vs_runtime._trusted_evaluation import (
    ProtocolBenchmarkContract,
    ScalarBenchmarkContract,
    TrustedAccuracyResult,
    TrustedBenchmarkContract,
    TrustedBenchmarkDecoding,
    TrustedBenchmarkResult,
    TrustedEvaluationExecutor,
    TrustedEvaluationPlan,
    TrustedMetricDeclaration,
    build_trusted_benchmark_command,
    create_trusted_evaluation_executor,
    decode_trusted_benchmark_output,
    decode_trusted_benchmark_run,
)
from vs_runtime._trusted_evaluation_preparation import (
    REMOTE_EVALUATOR_TOOLS_ROOT,
    SANDBOX_EVALUATOR_TOOLS_ROOT,
    PreparedTrustedEvaluator,
    TrustedEvaluationCommandPaths,
    TrustedEvaluatorRequirements,
    docker_evaluator_tools_root,
    evaluator_agent_toolchains,
    evaluator_container_setup,
    prepare_trusted_evaluation_plan,
    prepare_trusted_evaluator,
    remote_evaluator_setup_command,
    required_evaluator_tools_root,
)
from vs_runtime._validation_passes import StateValidationPasses, ValidationPasses
from vs_runtime._workspace_resources import (
    RootBaselineDrifted,
    WorkspaceResourceEvent,
    WorkspaceResourceEventSink,
    WorkspaceResourceFactory,
    WorkspaceRestoreFailed,
)
from vs_runtime._workspace_runtime import (
    CommandExecutionResult,
    RuntimeAccuracyRun,
    RuntimeBenchmarkRun,
    RuntimeCommands,
    RuntimeWorkspaceEvaluation,
    WorkspaceEvaluationSpec,
    WorkspaceRuntime,
)
from vs_runtime._workspaces import (
    OwnedWorkspaces,
    RuntimeWorkspace,
    RuntimeWorkspaces,
    WorkspaceResource,
    WorkspaceResourceProvider,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from vs_runtime.api import AgentRole
    from vs_sim.api import Threads


def create_run_control_channel(events: RunControlEventSink, threads: Threads) -> RunControlChannel:
    """Create one thread-safe cooperative run-control channel, locked by ``threads``."""
    return RuntimeRunControlChannel(events, threads)


type AgentConfigurationResolver = Callable[[AgentRole], AgentExecutionConfiguration]


def create_workspace_runtime(
    *,
    workspace_resources: WorkspaceResourceProvider,
    blocking: BlockingOperations,
    validation_passes: ValidationPasses,
) -> WorkspaceRuntime:
    """Create one owner for workspace handles and the commands and evaluation over them.

    ``validation_passes`` is the runtime-owned record of trusted local-validation passes;
    it must live outside every workspace.
    """
    workspaces = RuntimeWorkspaces(workspace_resources, runner=blocking.runner)
    commands = RuntimeCommands(workspaces, blocking)
    return WorkspaceRuntime(
        workspaces=workspaces,
        commands=commands,
        evaluation=RuntimeWorkspaceEvaluation(workspaces, commands, validation_passes),
    )


class ModelVolumeProvisioner(Protocol):
    """Ensure one requested model volume and return its stable name."""

    def __call__(
        self,
        model_id: str,
        *,
        revision: str | None = None,
        log: Callable[[str], object] = print,
    ) -> str:
        """Ensure one requested model volume and return its stable name."""
        ...


class ModelRequestReconciler(Protocol):
    """Reconcile candidate model requests without exposing manifest mechanics."""

    def reconcile(
        self,
        workspace: Path,
        *,
        log: Callable[[str], object] = print,
    ) -> tuple[str, ...]:
        """Validate and provision candidate requests in manifest order."""
        ...


class NativeCpuProfilerKind(StrEnum):
    """Native CPU profiler mechanism requested by product composition."""

    LINUX = "linux"
    MACOS = "macos"


@dataclass(frozen=True)
class NativeCpuProfilerPreflight:
    """Host capability facts needed by product profiler policy."""

    selected_tool: str
    usable: bool
    diagnostics: tuple[str, ...]
    details: tuple[str, ...]


def preflight_native_cpu_profiler(
    kind: NativeCpuProfilerKind,
    *,
    detect_linux: Callable[[], LinuxProfilerCapability],
    detect_macos: Callable[[], MacOSProfilerCapability] = detect_macos_profiler,
) -> NativeCpuProfilerPreflight:
    """Detect one native CPU profiler and return policy-neutral host facts.

    ``detect_linux`` probes ``perf``; `detect_linux_profiler` over the host's processes is
    the real probe.
    """
    if not isinstance(kind, NativeCpuProfilerKind):
        message = f"kind must be a NativeCpuProfilerKind, got {type(kind).__name__}."
        raise TypeError(message)
    if kind is NativeCpuProfilerKind.LINUX:
        capability = detect_linux()
        blocking = {
            LinuxProfilerDiagnostic.NOT_LINUX,
            LinuxProfilerDiagnostic.PERF_UNAVAILABLE,
            LinuxProfilerDiagnostic.PERF_STAT_UNAVAILABLE,
        }
        return NativeCpuProfilerPreflight(
            selected_tool=capability.tool.value,
            usable=capability.tool is LinuxProfilerTool.PERF
            and not any(item in blocking for item in capability.diagnostics),
            diagnostics=tuple(item.value for item in capability.diagnostics),
            details=(
                f"perf_path={capability.perf_path or 'missing'}",
                f"perf_event_paranoid={capability.perf_event_paranoid}",
                f"kptr_restrict={capability.kptr_restrict}",
            ),
        )

    capability = detect_macos()
    return NativeCpuProfilerPreflight(
        selected_tool=capability.tool.value,
        usable=capability.tool is not MacOSProfilerTool.NONE,
        diagnostics=tuple(item.value for item in capability.diagnostics),
        details=(
            f"xcode_path={capability.xcode_path or 'missing'}",
            f"xctrace_path={capability.xctrace_path or 'missing'}",
            f"sample_path={capability.sample_path or 'missing'}",
        ),
    )


def _ensure_model_volume(
    model_id: str,
    *,
    revision: str | None = None,
    log: Callable[[str], object] = print,
) -> str:
    """Load the optional Modal implementation only when reconciliation needs it."""
    ensure_model_volume = import_module("vs_sandbox.api").ensure_model_volume
    return ensure_model_volume(model_id, revision=revision, log=log)


def create_model_request_reconciler(
    *,
    provisioner: ModelVolumeProvisioner = _ensure_model_volume,
    environment: Mapping[str, str] | None = None,
) -> ModelRequestReconciler:
    """Bind model-volume and operator-environment effects once at composition."""
    return _ModelRequestReconciler(provisioner, environment)


__all__ = [
    "DEFAULT_RETRY_SECONDS",
    "PACKAGE_ROOT_TOKEN",
    "PAUSE_FOR_OPERATOR",
    "REMOTE_EVALUATOR_TOOLS_ROOT",
    "RESET_MARGIN_SECONDS",
    "RUN_ENVIRONMENT_FEATURES_PROJECT",
    "SANDBOX_EVALUATOR_TOOLS_ROOT",
    "TOOLKIT_BIN_PATTERNS",
    "TOOL_TOKEN_PREFIX",
    "AgentConfigurationResolver",
    "AgentExecutionConfiguration",
    "AgentExecutionEnvironment",
    "AgentExecutionFinished",
    "AgentExecutionLifecycleEvent",
    "AgentExecutionLifecycleSink",
    "AgentExecutionScope",
    "AgentExecutionStarted",
    "AgentExecutionStatus",
    "AgentPaths",
    "BlockingOperations",
    "BundledResources",
    "CapacityHandling",
    "CapacityTimer",
    "ClusterEvaluationFacts",
    "CommandExecutionResult",
    "DockerEnvironment",
    "DockerEnvironmentConfig",
    "DockerEnvironmentFacts",
    "DockerInDockerUnsupportedError",
    "DurableEventJournal",
    "EnvironmentStartup",
    "EnvironmentStartupSink",
    "EnvironmentStartupStage",
    "EnvironmentSystem",
    "EvaluatorPackageError",
    "EvaluatorPackageMetadata",
    "EvaluatorPackageNotFoundError",
    "EvaluatorPackageRequirement",
    "EventCodec",
    "FallbackTarget",
    "FrameworkValidationResult",
    "FreshProjectError",
    "FreshProjectErrorKind",
    "GitSourceMaterialization",
    "GiveUp",
    "Hold",
    "HostEnvironment",
    "HostEnvironmentFacts",
    "InputDependency",
    "InputProjectError",
    "InputProjectMaterialization",
    "LinuxProfileResult",
    "LinuxProfilerCapability",
    "LinuxProfilerDiagnostic",
    "LinuxProfilerEffects",
    "LinuxProfilerTool",
    "LocalValidationEvents",
    "LocalValidationRecipeError",
    "LocalValidationRecipeErrorKind",
    "LocalValidator",
    "MacOSProfileResult",
    "MacOSProfilerCapability",
    "MacOSProfilerDiagnostic",
    "MacOSProfilerEffects",
    "MacOSProfilerTool",
    "ManagedConversation",
    "ManagedConversationSpec",
    "ModalEnvironmentFacts",
    "ModelArtifactDownloader",
    "ModelArtifactRequest",
    "ModelRequestError",
    "ModelRequestReconciler",
    "ModelVolumeProvisioner",
    "NativeCpuProfilerKind",
    "NativeCpuProfilerPreflight",
    "OwnedWorkspaces",
    "ParallelCandidateObstacle",
    "PreparedModelArtifacts",
    "PreparedTrustedEvaluator",
    "ProjectMaterializationEffects",
    "ProjectMaterializationStep",
    "ProjectMaterializer",
    "ProjectRunBaselineMissingError",
    "ProjectRunDirtyResumeError",
    "ProjectRunEffects",
    "ProjectRunError",
    "ProjectRunMismatchError",
    "ProjectRunMismatchKind",
    "ProjectRunRequest",
    "ProjectRunResources",
    "ProjectTreeCopy",
    "ProtocolBenchmarkContract",
    "ProviderFallback",
    "QuotaAction",
    "QuotaDecision",
    "QuotaPolicy",
    "RecordState",
    "ResolvedEvaluatorPackage",
    "RootBaselineDrifted",
    "RunControlChannel",
    "RunControlEventSink",
    "RunControlTransition",
    "RunControlTransitionKind",
    "RunEnvironment",
    "RunEnvironmentPresentation",
    "RunEnvironmentRequest",
    "RunEnvironmentResources",
    "RunEnvironmentSession",
    "RunEnvironmentSpec",
    "RunEnvironmentView",
    "RunHostComponents",
    "RunState",
    "RunStopped",
    "RuntimeAccuracyRun",
    "RuntimeBenchmarkRun",
    "RuntimeRunHost",
    "RuntimeWorkspace",
    "RuntimeWorkspaceEvaluation",
    "SDKRoots",
    "ScalarBenchmarkContract",
    "ScopedAgentEnvironment",
    "SemanticSlurmEvaluationExecutor",
    "SharedAgentEnvironmentConflictError",
    "SkillCatalogEntry",
    "SkillMetadataError",
    "SkyPilotEnvironment",
    "SkyPilotEnvironmentFacts",
    "SlurmEnvironment",
    "SlurmEnvironmentFacts",
    "StageFailureText",
    "StateValidationPasses",
    "StopGraceError",
    "StopTimer",
    "Switch",
    "TerminalSignal",
    "ToolLocation",
    "ToolSite",
    "TrustedAccuracyResult",
    "TrustedBenchmarkContract",
    "TrustedBenchmarkDecoding",
    "TrustedBenchmarkResult",
    "TrustedEvaluationCommandPaths",
    "TrustedEvaluationExecutor",
    "TrustedEvaluationPlan",
    "TrustedEvaluatorRequirements",
    "TrustedMetricDeclaration",
    "ValidationPasses",
    "ValidationRecipe",
    "WaitFor",
    "WorkspaceEvaluationSpec",
    "WorkspaceResource",
    "WorkspaceResourceEvent",
    "WorkspaceResourceEventSink",
    "WorkspaceResourceFactory",
    "WorkspaceResourceProvider",
    "WorkspaceRestoreFailed",
    "WorkspaceRuntime",
    "WorkspaceSourceValue",
    "bounded_stop",
    "build_run_environment",
    "build_skill_catalog",
    "build_trusted_benchmark_command",
    "classify",
    "collect_linux_profile",
    "collect_macos_profile",
    "create_managed_conversation",
    "create_model_request_reconciler",
    "create_run_control_channel",
    "create_trusted_evaluation_executor",
    "create_workspace_runtime",
    "decide_quota",
    "decode_trusted_benchmark_output",
    "decode_trusted_benchmark_run",
    "detect_linux_profiler",
    "detect_macos_profiler",
    "discover_skill_dirs",
    "docker_evaluator_tools_root",
    "evaluator_agent_toolchains",
    "evaluator_container_setup",
    "find_job_tool",
    "image_features",
    "install_capacity_gate",
    "job_failure",
    "load_evaluator_package",
    "load_skill_frontmatter",
    "make_run_environment_spec",
    "materialize_input_project",
    "materialize_objective_document",
    "migrate_recorded_run_environment",
    "off_path_tools",
    "offered_skill_facts",
    "open_agent_execution_environment",
    "open_managed_conversation",
    "open_project_run_resources",
    "open_run_environment_resources",
    "open_run_host",
    "open_workspace_environment_resources",
    "parse_profile_command",
    "preflight_native_cpu_profiler",
    "prepare_docker_evaluator_resources",
    "prepare_model_artifacts",
    "prepare_trusted_evaluation_plan",
    "prepare_trusted_evaluator",
    "relative_sdk_source",
    "release_all",
    "remote_evaluator_setup_command",
    "required_evaluator_tools_root",
    "resolve_bundled_tree",
    "resolve_evaluator_package",
    "resolve_packaged_tree",
    "resolve_run_environment_spec",
    "resolve_sdk_source",
    "run_environment_deprecation",
    "run_environment_record",
    "signal_of",
    "stop_gated_evaluation",
    "summarize_linux_profile",
    "validate_run_environment_profile",
]
