"""Public contracts for the reusable VibeSys runtime.

Exports load on first use so host control commands do not load run execution machinery.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_agent.api import (
        AgentOutputSchemaError,
        AgentSpawnError,
        Completed,
        InvalidResponse,
        InvocationConflictError,
        InvocationOutcome,
        SessionConfigurationError,
        SessionPersistenceError,
        SessionResumeError,
        Unknown,
    )
    from vs_evaluator_protocol.api import PartialMeasurement, ProfileField, Progress
    from vs_project.api import OrchestrationDescriptor
    from vs_runtime._artifact_store import (
        ArtifactCorruptionError,
        ArtifactReceipt,
        ArtifactStore,
        ArtifactStoreError,
    )
    from vs_runtime._evaluation_failure_text import render_evaluation_failure, render_stage_failure
    from vs_runtime._local_validation import (
        VALIDATION_RECIPE_ARTIFACT_DESCRIPTION,
        ValidationRecipe,
        ValidationRecipeArtifact,
        ValidationRecipeArtifactPath,
    )
    from vs_runtime._polling_evaluation import PollingEvaluationExecutor
    from vs_runtime._retrying_evaluation import (
        ClosableEvaluationExecutor,
        EvaluationRetries,
        RetryingEvaluationExecutor,
        TryHandles,
    )
    from vs_runtime._run_control import RunStopped
    from vs_runtime._runs import RunExecution
    from vs_runtime._slot_meter import SlotLease, SlotMeter, SlotMeterError
    from vs_runtime.api import boot_trace as boot_trace
    from vs_runtime.contracts import (
        AccuracyEvaluation,
        AccuracyReceipt,
        AgentCapability,
        AgentId,
        AgentRole,
        AgentRoleId,
        AgentTool,
        AgentTurnTimeoutError,
        AgentWorkspaceRouteError,
        BenchmarkEvaluation,
        BenchmarkFailureKind,
        BenchmarkObjective,
        BuildEvaluation,
        CandidateWorkspace,
        CaptureCommands,
        CaptureStopUnconfirmedError,
        CommandResult,
        Commands,
        CoreOperation,
        CorePlan,
        CorePolicy,
        CoreRunContext,
        Evaluation,
        LocalValidationEvaluation,
        MetricDirection,
        OrchestrationPlugin,
        OrchestrationResumeDecision,
        OwnedOperationResources,
        ProfileExecution,
        RevisionLedger,
        RunCleanupError,
        RunFacts,
        RunStatus,
        RuntimeContractError,
        SessionClosedError,
        SessionTransportUnavailableError,
        SkillFact,
        StructuredResponseError,
        UnknownAgentRoleError,
        Workspace,
        WorkspaceAccess,
        WorkspaceRef,
        WorkspaceRestoreError,
        Workspaces,
        WorkspaceSourceFact,
        member_workspace_id,
        validate_command,
        validate_member_id,
        validate_trusted_shell_command,
        validate_workspace_writable_paths,
    )

_MODULES: dict[str, str] = {
    name: module
    for module, names in (
        (
            "vs_agent.api",
            (
                "AgentOutputSchemaError AgentSpawnError Completed InvalidResponse "
                "InvocationConflictError InvocationOutcome SessionConfigurationError "
                "SessionPersistenceError SessionResumeError Unknown "
            ),
        ),
        ("vs_evaluator_protocol.api", ("PartialMeasurement ProfileField Progress ")),
        ("vs_project.api", ("OrchestrationDescriptor ")),
        (
            "vs_runtime._artifact_store",
            ("ArtifactCorruptionError ArtifactReceipt ArtifactStore ArtifactStoreError "),
        ),
        (
            "vs_runtime._evaluation_failure_text",
            ("render_evaluation_failure render_stage_failure "),
        ),
        (
            "vs_runtime._local_validation",
            (
                "VALIDATION_RECIPE_ARTIFACT_DESCRIPTION ValidationRecipe ValidationRecipeArtifact "
                "ValidationRecipeArtifactPath "
            ),
        ),
        ("vs_runtime._polling_evaluation", ("PollingEvaluationExecutor ")),
        (
            "vs_runtime._retrying_evaluation",
            ("ClosableEvaluationExecutor EvaluationRetries RetryingEvaluationExecutor TryHandles "),
        ),
        ("vs_runtime._run_control", ("RunStopped ")),
        ("vs_runtime._runs", ("RunExecution ")),
        ("vs_runtime._slot_meter", ("SlotLease SlotMeter SlotMeterError ")),
        (
            "vs_runtime.contracts",
            (
                "AccuracyEvaluation AccuracyReceipt AgentCapability AgentId AgentRole AgentRoleId "
                "AgentTool AgentTurnTimeoutError AgentWorkspaceRouteError BenchmarkEvaluation "
                "BenchmarkFailureKind BenchmarkObjective BuildEvaluation CandidateWorkspace "
                "CaptureCommands CaptureStopUnconfirmedError CommandResult Commands CoreOperation "
                "CorePlan CorePolicy CoreRunContext Evaluation LocalValidationEvaluation "
                "MetricDirection OrchestrationPlugin OrchestrationResumeDecision "
                "OwnedOperationResources ProfileExecution RevisionLedger RunCleanupError RunFacts "
                "RunStatus RuntimeContractError SessionClosedError SessionTransportUnavailableError "
                "SkillFact StructuredResponseError UnknownAgentRoleError Workspace WorkspaceAccess "
                "WorkspaceRef WorkspaceRestoreError Workspaces WorkspaceSourceFact member_workspace_id "
                "validate_command validate_member_id validate_trusted_shell_command "
                "validate_workspace_writable_paths "
            ),
        ),
    )
    for name in names.split()
}

_ALIASES: dict[str, str] = {}

_SUBMODULES: dict[str, str] = {"boot_trace": "vs_runtime.api.boot_trace"}


def __getattr__(name: str) -> object:
    """Load and cache the declared public export on first use."""
    if name in _SUBMODULES:
        value = importlib.import_module(_SUBMODULES[name])
    else:
        module = _MODULES.get(name)
        if module is None:
            message = f"module {__name__!r} has no attribute {name!r}"
            raise AttributeError(message)
        value = getattr(importlib.import_module(module), _ALIASES.get(name, name))
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "VALIDATION_RECIPE_ARTIFACT_DESCRIPTION",
    "AccuracyEvaluation",
    "AccuracyReceipt",
    "AgentCapability",
    "AgentId",
    "AgentOutputSchemaError",
    "AgentRole",
    "AgentRoleId",
    "AgentSpawnError",
    "AgentTool",
    "AgentTurnTimeoutError",
    "AgentWorkspaceRouteError",
    "ArtifactCorruptionError",
    "ArtifactReceipt",
    "ArtifactStore",
    "ArtifactStoreError",
    "BenchmarkEvaluation",
    "BenchmarkFailureKind",
    "BenchmarkObjective",
    "BuildEvaluation",
    "CandidateWorkspace",
    "CaptureCommands",
    "CaptureStopUnconfirmedError",
    "ClosableEvaluationExecutor",
    "CommandResult",
    "Commands",
    "Completed",
    "CoreOperation",
    "CorePlan",
    "CorePolicy",
    "CoreRunContext",
    "Evaluation",
    "EvaluationRetries",
    "InvalidResponse",
    "InvocationConflictError",
    "InvocationOutcome",
    "LocalValidationEvaluation",
    "MetricDirection",
    "OrchestrationDescriptor",
    "OrchestrationPlugin",
    "OrchestrationResumeDecision",
    "OwnedOperationResources",
    "PartialMeasurement",
    "PollingEvaluationExecutor",
    "ProfileExecution",
    "ProfileField",
    "Progress",
    "RetryingEvaluationExecutor",
    "RevisionLedger",
    "RunCleanupError",
    "RunExecution",
    "RunFacts",
    "RunStatus",
    "RunStopped",
    "RuntimeContractError",
    "SessionClosedError",
    "SessionConfigurationError",
    "SessionPersistenceError",
    "SessionResumeError",
    "SessionTransportUnavailableError",
    "SkillFact",
    "SlotLease",
    "SlotMeter",
    "SlotMeterError",
    "StructuredResponseError",
    "TryHandles",
    "Unknown",
    "UnknownAgentRoleError",
    "ValidationRecipe",
    "ValidationRecipeArtifact",
    "ValidationRecipeArtifactPath",
    "Workspace",
    "WorkspaceAccess",
    "WorkspaceRef",
    "WorkspaceRestoreError",
    "WorkspaceSourceFact",
    "Workspaces",
    "boot_trace",
    "member_workspace_id",
    "render_evaluation_failure",
    "render_stage_failure",
    "validate_command",
    "validate_member_id",
    "validate_trusted_shell_command",
    "validate_workspace_writable_paths",
]
