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

from vs_agent.api import (
    AgentClientProtocol,
    AgentEventSink,
    SessionStore,
    ToolServerDescriptor,
    build_agent_client,
)
from vs_runtime._agent_execution import (
    AgentExecutionConfiguration,
    AgentExecutionEnvironment,
    AgentExecutionFinished,
    AgentExecutionLifecycleEvent,
    AgentExecutionLifecycleSink,
    AgentExecutionScope,
    AgentExecutionStarted,
    AgentExecutionStatus,
    AgentMessageRouter,
    ScopedAgentEnvironment,
    SharedAgentEnvironmentConflictError,
    open_agent_execution_environment,
)
from vs_runtime._agent_sessions import RuntimeAgentSessions
from vs_runtime._bundled_paths import (
    BundledResources,
    resolve_bundled_tree,
    resolve_packaged_tree,
)
from vs_runtime._checkpoint import (
    CompletedRound,
    MultiSlotRoundTransaction,
    MultiSlotRoundTransactionCoordinator,
    RoundRecoveryOutcome,
    RoundTransactionError,
)
from vs_runtime._docker_evaluator_tools import prepare_docker_evaluator_resources
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
from vs_runtime._input_project import InputDependency, materialize_input_project
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
    ValidationRecipe,
    run_local_validation,
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
    ProjectStateDeclaration,
    open_project_run_resources,
)
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
    DockerEnvironmentFacts,
    LocalEnvironment,
    LocalEnvironmentFacts,
    ModalEnvironmentFacts,
    RunEnvironment,
    RunEnvironmentPresentation,
    RunEnvironmentRequest,
    RunEnvironmentResources,
    RunEnvironmentSession,
    RunEnvironmentSpec,
    RunEnvironmentView,
    SkyPilotEnvironment,
    SkyPilotEnvironmentFacts,
    build_run_environment,
    make_run_environment_spec,
    open_run_environment_resources,
    open_workspace_environment_resources,
    run_environment_record,
)
from vs_runtime._run_host import (
    BlockingOperations,
    RunHostComponents,
    RunHostResourceOwner,
    RuntimeRunHost,
    create_runtime_control,
    open_run_host,
)
from vs_runtime._run_state import RunState
from vs_runtime._sdk_paths import (
    InputProjectError,
    SDKRoots,
    relative_sdk_source,
    resolve_sdk_source,
)
from vs_runtime._skills import (
    SkillCatalogEntry,
    SkillMetadataError,
    build_skill_catalog,
    discover_skill_dirs,
    load_skill_frontmatter,
    resolve_skill_resources,
)
from vs_runtime._state import CommittedStateObserver, create_state
from vs_runtime._trusted_evaluation import (
    ProtocolBenchmarkContract,
    ScalarBenchmarkContract,
    TrustedAccuracyResult,
    TrustedBenchmarkContract,
    TrustedBenchmarkResult,
    TrustedEvaluationExecutor,
    TrustedEvaluationPlan,
    TrustedMetricDeclaration,
    create_trusted_evaluation_executor,
)
from vs_runtime._trusted_evaluation_preparation import (
    REMOTE_EVALUATOR_TOOLS_ROOT,
    SANDBOX_EVALUATOR_TOOLS_ROOT,
    TrustedEvaluationCommandPaths,
    TrustedEvaluatorRequirements,
    docker_evaluator_tools_root,
    evaluator_agent_toolchains,
    evaluator_container_setup,
    prepare_trusted_evaluation_plan,
    remote_evaluator_setup_command,
    required_evaluator_tools_root,
)
from vs_runtime._workspace_runtime import (
    CommandExecutionResult,
    RuntimeCommands,
    RuntimeWorkspaceEvaluation,
    WorkspaceEvaluationSpec,
    WorkspaceRuntime,
)
from vs_runtime._workspaces import (
    OwnedWorkspaces,
    RuntimeWorkspaces,
    WorkspaceResource,
)
from vs_runtime.contracts import Workspace

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from vs_runtime.api import AgentRole


def create_run_control_channel(events: RunControlEventSink) -> RunControlChannel:
    """Create one thread-safe cooperative run-control channel."""
    return RuntimeRunControlChannel(events)


type AgentConfigurationResolver = Callable[[AgentRole], AgentExecutionConfiguration]
type AgentToolResolver = Callable[[Workspace], tuple[ToolServerDescriptor, ...]]
type CandidateWorkspaceResourceFactory = Callable[[str, str], WorkspaceResource]


def create_workspace_runtime(  # noqa: PLR0913  # lint-waiver: LW-837213 [PLR0913]; composition fixes independent execution and workspace effects behind focused plugin-facing capabilities.
    roles: tuple[AgentRole, ...],
    *,
    root_resource: WorkspaceResource,
    supports_parallel_candidates: bool,
    create_candidate_resource: CandidateWorkspaceResourceFactory,
    resolve_configuration: AgentConfigurationResolver,
    session_store: Callable[[], SessionStore | None],
    control: RunControlChannel,
    lifecycle_events: AgentExecutionLifecycleSink,
    agent_events: AgentEventSink,
    route_message: AgentMessageRouter,
    blocking: BlockingOperations,
    client_factory: Callable[..., AgentClientProtocol] | None = None,
    tool_bindings: Mapping[str, AgentToolResolver] | None = None,
    log: Callable[[str], None] = print,
) -> WorkspaceRuntime:
    """Create one owner for workspace handles and their bound agent sessions."""
    workspaces = RuntimeWorkspaces(
        root_resource,
        supports_parallel_candidates=supports_parallel_candidates,
        create_candidate_resource=create_candidate_resource,
    )
    agents = RuntimeAgentSessions(
        roles,
        workspaces=workspaces,
        resolve_configuration=resolve_configuration,
        session_store=session_store,
        control=control,
        lifecycle_events=lifecycle_events,
        agent_events=agent_events,
        route_message=route_message,
        client_factory=client_factory or build_agent_client,
        tool_bindings=tool_bindings,
        log=log,
    )
    workspaces._attach_sessions(agents)  # noqa: SLF001  # lint-waiver: LW-837221 [SLF001]; this sole factory completes the private ownership cycle before either capability escapes.
    return WorkspaceRuntime(
        agents=agents,
        workspaces=workspaces,
        commands=RuntimeCommands(workspaces, blocking),
        evaluation=RuntimeWorkspaceEvaluation(workspaces),
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
    detect_linux: Callable[[], LinuxProfilerCapability] = detect_linux_profiler,
    detect_macos: Callable[[], MacOSProfilerCapability] = detect_macos_profiler,
) -> NativeCpuProfilerPreflight:
    """Detect one native CPU profiler and return policy-neutral host facts."""
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
    "PACKAGE_ROOT_TOKEN",
    "REMOTE_EVALUATOR_TOOLS_ROOT",
    "SANDBOX_EVALUATOR_TOOLS_ROOT",
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
    "AgentMessageRouter",
    "AgentPaths",
    "AgentToolResolver",
    "BlockingOperations",
    "BundledResources",
    "CommandExecutionResult",
    "CommittedStateObserver",
    "CompletedRound",
    "DockerEnvironmentFacts",
    "DurableEventJournal",
    "EvaluatorPackageError",
    "EvaluatorPackageMetadata",
    "EvaluatorPackageNotFoundError",
    "EvaluatorPackageRequirement",
    "EventCodec",
    "FrameworkValidationResult",
    "GitSourceMaterialization",
    "InputDependency",
    "InputProjectError",
    "InputProjectMaterialization",
    "LinuxProfileResult",
    "LinuxProfilerCapability",
    "LinuxProfilerDiagnostic",
    "LinuxProfilerEffects",
    "LinuxProfilerTool",
    "LocalEnvironment",
    "LocalEnvironmentFacts",
    "LocalValidationEvents",
    "LocalValidationRecipeError",
    "LocalValidationRecipeErrorKind",
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
    "MultiSlotRoundTransaction",
    "MultiSlotRoundTransactionCoordinator",
    "NativeCpuProfilerKind",
    "NativeCpuProfilerPreflight",
    "OwnedWorkspaces",
    "PreparedModelArtifacts",
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
    "ProjectStateDeclaration",
    "ProjectTreeCopy",
    "ProtocolBenchmarkContract",
    "ResolvedEvaluatorPackage",
    "RoundRecoveryOutcome",
    "RoundTransactionError",
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
    "RunHostResourceOwner",
    "RunState",
    "RunStopped",
    "RuntimeRunHost",
    "RuntimeWorkspaceEvaluation",
    "SDKRoots",
    "ScalarBenchmarkContract",
    "ScopedAgentEnvironment",
    "SharedAgentEnvironmentConflictError",
    "SkillCatalogEntry",
    "SkillMetadataError",
    "SkyPilotEnvironment",
    "SkyPilotEnvironmentFacts",
    "TrustedAccuracyResult",
    "TrustedBenchmarkContract",
    "TrustedBenchmarkResult",
    "TrustedEvaluationCommandPaths",
    "TrustedEvaluationExecutor",
    "TrustedEvaluationPlan",
    "TrustedEvaluatorRequirements",
    "TrustedMetricDeclaration",
    "ValidationRecipe",
    "WorkspaceEvaluationSpec",
    "WorkspaceResource",
    "WorkspaceRuntime",
    "WorkspaceSourceValue",
    "build_run_environment",
    "build_skill_catalog",
    "collect_linux_profile",
    "collect_macos_profile",
    "create_managed_conversation",
    "create_model_request_reconciler",
    "create_run_control_channel",
    "create_runtime_control",
    "create_state",
    "create_trusted_evaluation_executor",
    "create_workspace_runtime",
    "detect_linux_profiler",
    "detect_macos_profiler",
    "discover_skill_dirs",
    "docker_evaluator_tools_root",
    "evaluator_agent_toolchains",
    "evaluator_container_setup",
    "load_evaluator_package",
    "load_skill_frontmatter",
    "make_run_environment_spec",
    "materialize_input_project",
    "materialize_objective_document",
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
    "relative_sdk_source",
    "remote_evaluator_setup_command",
    "required_evaluator_tools_root",
    "resolve_bundled_tree",
    "resolve_evaluator_package",
    "resolve_packaged_tree",
    "resolve_sdk_source",
    "resolve_skill_resources",
    "run_environment_record",
    "run_local_validation",
    "summarize_linux_profile",
]
