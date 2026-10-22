"""The public surface of VibeSys core: the only module other packages import.

Everything else under `vibesys.*` is private to core. `server.*` and
`entrypoints` talk to core only through this package: the package root is the
policy-neutral run/observe contract, while explicitly named submodules expose
built-in policy projections and configuration. `vibesys.api.request` is the
surface for assembling a `RunRequest`.

Most symbols here are contracts and Protocols; the rest are re-exports of
core-owned types that consumers legitimately need, so they never import their
private home modules.

Exports load on first use so host control commands do not load run execution machinery.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.api.auxiliary import (
        STEER_PROPOSAL_KIND,
        AuxiliaryAgentLaunch,
        AuxiliaryAgents,
        AuxiliaryReadableInput,
        AuxiliarySkills,
        ManagedAgent,
        RunReady,
        SteerProposal,
        SteerProposalReceipt,
    )
    from vibesys.api.contracts import (
        ArgumentsRejected,
        ArgumentsUnrecognized,
        Config,
        ConfigInvalid,
        ConfigurationCause,
        CoreEvent,
        EventStatus,
        GitHubUnavailable,
        IterationKind,
        IterationMetric,
        IterationProjection,
        IterationStatus,
        IterationSummary,
        IterationSupport,
        IterationTree,
        IterationUnavailable,
        IterationUnavailableReason,
        ModelNotVetted,
        OrchestrationDescriptor,
        PluginProjection,
        ProfilerKind,
        ResumeRef,
        RolesNotDeclared,
        RunFailure,
        RunFailureKind,
        RunRequest,
        RunResult,
        RunStatus,
        RunView,
        TaskImageUnsupported,
        TaskNeedsProvisioning,
        TaskNotFound,
        TaskUnusable,
        cli_wording,
        configuration_error,
    )
    from vibesys.api.domains import domain_modalities
    from vibesys.api.entry import load_config
    from vibesys.api.reader import RunReader, RunReaders
    from vibesys.api.runs import RunHandle, Runs, relaunch
    from vibesys.api.session import RunControl, RunSession, create_session
    from vibesys.api.store import (
        RunDocument,
        RunRecord,
        RunRecordFacts,
        RunRecordReadError,
        RunStore,
        WorkspaceChange,
        WorkspaceChangeKind,
        open_run_store,
    )
    from vibesys.composition import agent_spec_from_config
    from vibesys.constants import KNOWN_COMPUTE_BACKENDS, ComputeBackend, DomainName
    from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
    from vibesys.events import (
        AgentExecutionStartedData,
        AgentOutputChannel,
        AgentOutputChunkData,
        AgentReply,
        AgentStatusData,
        CommandResultPayload,
        ComparisonReadingData,
        ComparisonsReadData,
        ComparisonStatus,
        CoreEventType,
        FrameworkWarningData,
        GateFinishedData,
        GateKind,
        GateStartedData,
        InputMeasurementFailedData,
        InputMeasurementFailureKind,
        JsonResultPayload,
        MeasurementComparedData,
        ProviderSwitchedData,
        QuotaAbandonedData,
        QuotaPausedData,
        QuotaResumedData,
        QuotaWaitCancelledData,
        QuotaWaitingData,
        RateLimitUpdateData,
        RunConfiguredData,
        RunEndingData,
        RunFailedData,
        RunOverview,
        SpanEndedData,
        SpanId,
        SpanStartedData,
        SpanUpdatedData,
        SteerDeliveryData,
        TodoItemData,
        TodoUpdateData,
        ToolCallData,
        ToolResultData,
        ToolResultPayload,
        WinnerSelectedData,
        WireSpan,
        WorkspaceRootProbeData,
        WorkspaceSnapshotData,
    )
    from vibesys.log_owners import GateStage, LogOwner, RunNarration, StartupStep, ToolBuild
    from vibesys.plugin_catalog import OrchestrationRegistry
    from vibesys.repository import RepositoryVisibility
    from vibesys.run import CoreAgentEventSink
    from vs_agent.api import (
        AgentBackend,
        AgentSpec,
        Harness,
        check_harness,
        harnesses,
        vetted_models,
    )
    from vs_runtime.api import boot_trace
    from vs_runtime.api.control import (
        CONTROL_REQUEST,
        ControlAction,
        recorded_ending,
        recorded_epoch,
    )
    from vs_runtime.api.infrastructure import (
        CONTROL_ID_PATTERN,
        REFUSAL_TEXT,
        ActionRequest,
        CancelAttemptRequest,
        CancelItemRequest,
        ControlAck,
        ControlRequest,
        ControlStatus,
        PauseNowRequest,
        PauseRequest,
        RefusalReason,
        ResumeRequest,
        ResumeWhen,
        RunStopped,
        SetMaxParallelRequest,
        SteerableAttempt,
        SteerDelivery,
        SteerDeliveryState,
        SteerMode,
        SteerRequest,
        SteerTarget,
        SteerTargetKind,
        SteerTargets,
        SteerTurn,
        StopMode,
        WithdrawRequest,
        check_one_way_to_resume,
        describe_seconds,
        escalate_stop,
    )

_MODULES: dict[str, str] = {
    name: module
    for module, names in (
        (
            "vs_runtime.api.control",
            ("CONTROL_REQUEST ControlAction recorded_ending recorded_epoch "),
        ),
        (
            "vibesys.api.auxiliary",
            (
                "STEER_PROPOSAL_KIND AuxiliaryAgentLaunch AuxiliaryAgents AuxiliaryReadableInput "
                "AuxiliarySkills ManagedAgent RunReady SteerProposal SteerProposalReceipt "
            ),
        ),
        (
            "vibesys.api.contracts",
            (
                "ArgumentsRejected ArgumentsUnrecognized Config ConfigInvalid ConfigurationCause "
                "CoreEvent EventStatus GitHubUnavailable "
                "IterationKind IterationMetric IterationProjection IterationStatus IterationSummary "
                "IterationSupport IterationTree IterationUnavailable IterationUnavailableReason "
                "ModelNotVetted OrchestrationDescriptor PluginProjection ProfilerKind ResumeRef "
                "RolesNotDeclared RunFailure RunFailureKind RunRequest RunResult RunStatus RunView "
                "TaskImageUnsupported TaskNeedsProvisioning TaskNotFound TaskUnusable cli_wording "
                "configuration_error "
            ),
        ),
        ("vibesys.errors", "ConfigurationDiagnostic ConfigurationError "),
        ("vibesys.api.domains", ("domain_modalities ")),
        ("vibesys.api.entry", ("load_config ")),
        ("vibesys.api.reader", ("RunReader RunReaders ")),
        ("vibesys.api.runs", ("RunHandle Runs relaunch ")),
        ("vibesys.api.session", ("RunControl RunSession create_session ")),
        (
            "vibesys.api.store",
            (
                "RunDocument RunRecord RunRecordFacts RunRecordReadError RunStore WorkspaceChange "
                "WorkspaceChangeKind open_run_store "
            ),
        ),
        ("vibesys.composition", ("agent_spec_from_config ")),
        ("vibesys.constants", ("KNOWN_COMPUTE_BACKENDS ComputeBackend DomainName ")),
        (
            "vibesys.events",
            (
                "AgentExecutionStartedData AgentOutputChannel AgentOutputChunkData AgentReply "
                "AgentStatusData CommandResultPayload ComparisonReadingData ComparisonsReadData "
                "ComparisonStatus CoreEventType FrameworkWarningData GateFinishedData GateKind "
                "GateStartedData InputMeasurementFailedData InputMeasurementFailureKind "
                "JsonResultPayload MeasurementComparedData ProviderSwitchedData QuotaAbandonedData "
                "QuotaPausedData QuotaResumedData QuotaWaitCancelledData QuotaWaitingData "
                "RateLimitUpdateData RunConfiguredData RunEndingData RunFailedData RunOverview "
                "SpanEndedData SpanId SpanStartedData SpanUpdatedData SteerDeliveryData TodoItemData "
                "TodoUpdateData ToolCallData ToolResultData ToolResultPayload WinnerSelectedData "
                "WireSpan WorkspaceRootProbeData WorkspaceSnapshotData "
            ),
        ),
        ("vibesys.log_owners", ("GateStage LogOwner RunNarration StartupStep ToolBuild ")),
        ("vibesys.plugin_catalog", ("OrchestrationRegistry ")),
        ("vibesys.repository", ("RepositoryVisibility ")),
        ("vibesys.run", ("CoreAgentEventSink ")),
        ("vs_agent.api", ("AgentBackend AgentSpec Harness check_harness harnesses vetted_models ")),
        (
            "vs_runtime.api.infrastructure",
            (
                "CONTROL_ID_PATTERN REFUSAL_TEXT ActionRequest CancelAttemptRequest CancelItemRequest "
                "ControlAck ControlRequest ControlStatus PauseNowRequest PauseRequest RefusalReason "
                "ResumeRequest ResumeWhen RunStopped SetMaxParallelRequest SteerableAttempt "
                "SteerDelivery SteerDeliveryState SteerMode SteerRequest SteerTarget SteerTargetKind "
                "SteerTargets SteerTurn StopMode WithdrawRequest check_one_way_to_resume "
                "describe_seconds escalate_stop "
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
    "CONTROL_ID_PATTERN",
    "CONTROL_REQUEST",
    "KNOWN_COMPUTE_BACKENDS",
    "REFUSAL_TEXT",
    "STEER_PROPOSAL_KIND",
    "ActionRequest",
    "AgentBackend",
    "AgentExecutionStartedData",
    "AgentOutputChannel",
    "AgentOutputChunkData",
    "AgentReply",
    "AgentSpec",
    "AgentStatusData",
    "ArgumentsRejected",
    "ArgumentsUnrecognized",
    "AuxiliaryAgentLaunch",
    "AuxiliaryAgents",
    "AuxiliaryReadableInput",
    "AuxiliarySkills",
    "CancelAttemptRequest",
    "CancelItemRequest",
    "CommandResultPayload",
    "ComparisonReadingData",
    "ComparisonStatus",
    "ComparisonsReadData",
    "ComputeBackend",
    "Config",
    "ConfigInvalid",
    "ConfigurationCause",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "ControlAck",
    "ControlAction",
    "ControlRequest",
    "ControlStatus",
    "CoreAgentEventSink",
    "CoreEvent",
    "CoreEventType",
    "DomainName",
    "EventStatus",
    "FrameworkWarningData",
    "GateFinishedData",
    "GateKind",
    "GateStage",
    "GateStartedData",
    "GitHubUnavailable",
    "Harness",
    "InputMeasurementFailedData",
    "InputMeasurementFailureKind",
    "IterationKind",
    "IterationMetric",
    "IterationProjection",
    "IterationStatus",
    "IterationSummary",
    "IterationSupport",
    "IterationTree",
    "IterationUnavailable",
    "IterationUnavailableReason",
    "JsonResultPayload",
    "LogOwner",
    "ManagedAgent",
    "MeasurementComparedData",
    "ModelNotVetted",
    "OrchestrationDescriptor",
    "OrchestrationRegistry",
    "PauseNowRequest",
    "PauseRequest",
    "PluginProjection",
    "ProfilerKind",
    "ProviderSwitchedData",
    "QuotaAbandonedData",
    "QuotaPausedData",
    "QuotaResumedData",
    "QuotaWaitCancelledData",
    "QuotaWaitingData",
    "RateLimitUpdateData",
    "RefusalReason",
    "RepositoryVisibility",
    "ResumeRef",
    "ResumeRequest",
    "ResumeWhen",
    "RolesNotDeclared",
    "RunConfiguredData",
    "RunControl",
    "RunDocument",
    "RunEndingData",
    "RunFailedData",
    "RunFailure",
    "RunFailureKind",
    "RunHandle",
    "RunNarration",
    "RunOverview",
    "RunReader",
    "RunReaders",
    "RunReady",
    "RunRecord",
    "RunRecordFacts",
    "RunRecordReadError",
    "RunRequest",
    "RunResult",
    "RunSession",
    "RunStatus",
    "RunStopped",
    "RunStore",
    "RunView",
    "Runs",
    "SetMaxParallelRequest",
    "SpanEndedData",
    "SpanId",
    "SpanStartedData",
    "SpanUpdatedData",
    "StartupStep",
    "SteerDelivery",
    "SteerDeliveryData",
    "SteerDeliveryState",
    "SteerMode",
    "SteerProposal",
    "SteerProposalReceipt",
    "SteerRequest",
    "SteerTarget",
    "SteerTargetKind",
    "SteerTargets",
    "SteerTurn",
    "SteerableAttempt",
    "StopMode",
    "TaskImageUnsupported",
    "TaskNeedsProvisioning",
    "TaskNotFound",
    "TaskUnusable",
    "TodoItemData",
    "TodoUpdateData",
    "ToolBuild",
    "ToolCallData",
    "ToolResultData",
    "ToolResultPayload",
    "WinnerSelectedData",
    "WireSpan",
    "WithdrawRequest",
    "WorkspaceChange",
    "WorkspaceChangeKind",
    "WorkspaceRootProbeData",
    "WorkspaceSnapshotData",
    "agent_spec_from_config",
    "boot_trace",
    "check_harness",
    "check_one_way_to_resume",
    "cli_wording",
    "configuration_error",
    "create_session",
    "describe_seconds",
    "domain_modalities",
    "escalate_stop",
    "harnesses",
    "load_config",
    "open_run_store",
    "recorded_ending",
    "recorded_epoch",
    "relaunch",
    "vetted_models",
]
