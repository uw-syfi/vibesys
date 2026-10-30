"""Run store and host API contracts generated from the root proto tree.

Messages encode with ``to_binary`` and decode with ``from_binary``. Call
``validate`` at input boundaries; it raises ``ValidationError`` when schema
rules fail. Decoding preserves unknown fields for additive store evolution.
The two schema hashes identify the canonical descriptors, not generated code.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_proto._generated.vibesys.testing.v1.client_replay_pb import (
        ClientReplay,
        ClientReplayBatch,
    )

from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp
from protovalidate import ValidationError

from vs_proto._generated import host_operations
from vs_proto._generated.buf.validate import validate_pb as validation_rules
from vs_proto._generated.framing import RUN_LOG_MAGIC, RUN_LOG_MAX_RECORD_BYTES
from vs_proto._generated.vibesys.api.v1 import host_operations_pb as host_operation_descriptors
from vs_proto._generated.vibesys.api.v1.host_call_pb import (
    CancelAttemptIntent,
    CancelItemIntent,
    ChatMessageRequest,
    ChatMessageResponse,
    ConfigurationFailed,
    CreateChatSessionRequest,
    CreateChatSessionResponse,
    HostCallRequest,
    HostCallResponse,
    PauseIntent,
    PauseNowIntent,
    PostIntentRequest,
    PostIntentResponse,
    RelaunchRefusal,
    RelaunchRequest,
    RelaunchResponse,
    SetParallelismIntent,
    SteerIntent,
    WithdrawIntent,
)
from vs_proto._generated.vibesys.api.v1.host_mutate_generated_pb import (
    MutateRequest,
    MutateResponse,
)
from vs_proto._generated.vibesys.api.v1.host_pb import (
    BlobResult,
    DescriptorSet,
    GetBlobsRequest,
    GetBlobsResponse,
    HelloRequest,
    HelloResponse,
    RunLiveness,
    SkeletonRequest,
    SkeletonResponse,
    TailRequest,
    TailResponse,
    TailStreamRequest,
    TailStreamResponse,
    WatchRunsRequest,
    WatchRunsResponse,
)
from vs_proto._generated.vibesys.api.v1.host_read_generated_pb import (
    ReadRequest,
    ReadResponse,
)
from vs_proto._generated.vibesys.api.v1.host_watch_generated_pb import (
    WatchRequest,
    WatchResponse,
)
from vs_proto._generated.vibesys.core.testing.v1 import values_pb as compiler_values
from vs_proto._generated.vibesys.core.v1 import agent_facts_pb as core_agent_facts
from vs_proto._generated.vibesys.core.v1 import common_pb as core_common
from vs_proto._generated.vibesys.core.v1 import contracts_pb as core_contracts
from vs_proto._generated.vibesys.core.v1 import control_pb as core_control
from vs_proto._generated.vibesys.core.v1 import failures_pb as core_failures
from vs_proto._generated.vibesys.core.v1 import imported_facts_pb as imported_facts
from vs_proto._generated.vibesys.core.v1 import iterations_pb as core_iterations
from vs_proto._generated.vibesys.core.v1 import leaves_pb as core_leaves
from vs_proto._generated.vibesys.core.v1 import replay_agent_pb as replay_agent
from vs_proto._generated.vibesys.core.v1 import replay_evaluation_pb as replay_evaluation
from vs_proto._generated.vibesys.core.v1 import replay_workspace_pb as replay_workspace
from vs_proto._generated.vibesys.core.v1 import value_options_pb as core_value_options
from vs_proto._generated.vibesys.core.v1 import vocabulary_pb as core_vocabulary
from vs_proto._generated.vibesys.core.v1.vocabulary_pb import CheckoutStatus
from vs_proto._generated.vibesys.desktop.v1 import gateway_pb as desktop_gateway
from vs_proto._generated.vibesys.desktop.v1 import settings_pb as desktop_settings
from vs_proto._generated.vibesys.host.v1 import admission_receipt_generated_pb as admission
from vs_proto._generated.vibesys.host.v1 import evaluation_receipts_pb as evaluation_receipts
from vs_proto._generated.vibesys.host.v1 import operation_options_pb as operation_options
from vs_proto._generated.vibesys.host.v1 import operations_pb as host_payloads
from vs_proto._generated.vibesys.host.v1.failure_pb import FailureCategory, OperationFailure
from vs_proto._generated.vibesys.search.v1 import serial_effects_pb as serial_effects
from vs_proto._generated.vibesys.search.v1.attrs_pb import (
    AdoptionAttrs,
    BaselineAttrs,
    BaselineStatus,
    ComparisonAttrs,
    ComparisonVerdict,
    EvidenceKind,
    FailedCheck,
    FileChange,
    FrameworkSource,
    GateAttrs,
    GateStage,
    GateSubject,
    InputFailure,
    InputFailureKind,
    PlanningAttrs,
    Selection,
    WorkstreamAttrs,
    WorkstreamFailure,
    WorkstreamFailureKind,
)
from vs_proto._generated.vibesys.search.v1.run_pb import (
    EndReason,
    SearchEnded,
    SearchRunDescription,
    SearchRunFailure,
)
from vs_proto._generated.vibesys.store.v1 import replay_pb as replay
from vs_proto._generated.vibesys.store.v1.agent_attrs_pb import (
    AgentClient,
    AgentTurnAttrs,
    TokenUsage,
)
from vs_proto._generated.vibesys.store.v1.chat_pb import (
    ChatMessage,
    ChatRecord,
    ChatRole,
    ChatSessionCreated,
    ChatSteerProposed,
    ChatToken,
)
from vs_proto._generated.vibesys.store.v1.common_pb import (
    Direction,
    Progress,
    SeriesRole,
)
from vs_proto._generated.vibesys.store.v1.content_pb import (
    BlobAttached,
    BlobRef,
    GateStreamOwner,
    LogGate,
    MetricPoint,
    StreamKind,
    StreamOwner,
    StreamRef,
    StreamState,
)
from vs_proto._generated.vibesys.store.v1.core_input_pb import (
    AttemptInterrupted,
    CoreInput,
    InterruptionReason,
    StepCancelReason,
    StepFinished,
    StepStarted,
)
from vs_proto._generated.vibesys.store.v1.environment_attrs_pb import (
    CapacityCondition,
    CapacityPolicy,
    CapacityWaitAttrs,
    EnvironmentProbeAttrs,
    ProbeState,
    ProviderSwitchAttrs,
    ProviderSwitchReason,
)
from vs_proto._generated.vibesys.store.v1.epoch_pb import (
    Evaluator,
    InputMeasurement,
    MeasuredInput,
    MetricSeriesDecl,
    Objective,
    RunDescription,
    SpanKindDecl,
    UnmeasuredInput,
)
from vs_proto._generated.vibesys.store.v1.intent_pb import (
    IntentAction,
    IntentState,
    IntentStep,
    SteerMode,
    SteerTarget,
    SteerTargetKind,
)
from vs_proto._generated.vibesys.store.v1.lifecycle_pb import (
    DrainGoal,
    Draining,
    Ended,
    Hold,
    Lifecycle,
    OperatorHold,
    Outcome,
    Parked,
    RunFailure,
    RunFailureKind,
    Running,
    RunOutcomePresentation,
    StageState,
    Starting,
    StartupStage,
    WaitCondition,
)
from vs_proto._generated.vibesys.store.v1.notice_pb import (
    Notice,
    NoticeCode,
    Severity,
)
from vs_proto._generated.vibesys.store.v1.record_pb import (
    EpochStarted,
    LegacyEvent,
    Record,
    ReplayStore,
)
from vs_proto._generated.vibesys.store.v1.skeleton_pb import (
    EpochVersion,
    HeadlineObjective,
    IdeaDecision,
    IdeaOutcome,
    IdeaPerformance,
    Keyframe,
    LifecycleVersion,
    MetricHistory,
    MetricVersion,
    PerformanceRefusal,
    RunAddress,
    RunHead,
    RunProcess,
    SeriesPerformance,
    SpanHistory,
    SpanVersion,
)
from vs_proto._generated.vibesys.store.v1.span_pb import (
    Attempt,
    AttemptOutcome,
    Change,
    Failure,
    Label,
    RuntimeType,
    Span,
    SpanFailureKind,
    SpanLink,
    SpanRecord,
    SpanStatus,
    Stop,
    WaitingOn,
    WaitKind,
)
from vs_proto._generated.vibesys.store.v1.transcript_pb import (
    TranscriptChannel,
    TranscriptContent,
    TranscriptEntry,
    TranscriptText,
    TranscriptToolCall,
    TranscriptToolResult,
)
from vs_proto._operation_projection import from_mapping, to_mapping
from vs_proto._operations import AcceptedRequest, Operation
from vs_proto._operations import checked as validated_operation_bytes
from vs_proto._outcomes import outcome_presentation
from vs_proto._schema_hashes import (
    API_SCHEMA_HASH,
    HOST_SCHEMA_HASH,
    STORE_SCHEMA_HASH,
    schema_hash,
)
from vs_proto._validation import validate

__all__ = [
    "API_SCHEMA_HASH",
    "HOST_SCHEMA_HASH",
    "RUN_LOG_MAGIC",
    "RUN_LOG_MAX_RECORD_BYTES",
    "STORE_SCHEMA_HASH",
    "AcceptedRequest",
    "AdoptionAttrs",
    "AgentClient",
    "AgentTurnAttrs",
    "Attempt",
    "AttemptInterrupted",
    "AttemptOutcome",
    "BaselineAttrs",
    "BaselineStatus",
    "BlobAttached",
    "BlobRef",
    "BlobResult",
    "CancelAttemptIntent",
    "CancelItemIntent",
    "CapacityCondition",
    "CapacityPolicy",
    "CapacityWaitAttrs",
    "Change",
    "ChatMessage",
    "ChatMessageRequest",
    "ChatMessageResponse",
    "ChatRecord",
    "ChatRole",
    "ChatSessionCreated",
    "ChatSteerProposed",
    "ChatToken",
    "CheckoutStatus",
    "ClientReplay",
    "ClientReplayBatch",
    "ComparisonAttrs",
    "ComparisonVerdict",
    "ConfigurationFailed",
    "CoreInput",
    "CreateChatSessionRequest",
    "CreateChatSessionResponse",
    "DescriptorSet",
    "Direction",
    "DrainGoal",
    "Draining",
    "EndReason",
    "Ended",
    "EnvironmentProbeAttrs",
    "EpochStarted",
    "EpochVersion",
    "Evaluator",
    "EvidenceKind",
    "FailedCheck",
    "Failure",
    "FailureCategory",
    "FileChange",
    "FrameworkSource",
    "GateAttrs",
    "GateStage",
    "GateStreamOwner",
    "GateSubject",
    "GetBlobsRequest",
    "GetBlobsResponse",
    "HeadlineObjective",
    "HelloRequest",
    "HelloResponse",
    "Hold",
    "HostCallRequest",
    "HostCallResponse",
    "IdeaDecision",
    "IdeaOutcome",
    "IdeaPerformance",
    "InputFailure",
    "InputFailureKind",
    "InputMeasurement",
    "IntentAction",
    "IntentState",
    "IntentStep",
    "InterruptionReason",
    "Keyframe",
    "Label",
    "LegacyEvent",
    "Lifecycle",
    "LifecycleVersion",
    "LogGate",
    "MeasuredInput",
    "MetricHistory",
    "MetricPoint",
    "MetricSeriesDecl",
    "MetricVersion",
    "MutateRequest",
    "MutateResponse",
    "Notice",
    "NoticeCode",
    "Objective",
    "Oneof",
    "Operation",
    "OperationFailure",
    "OperatorHold",
    "Outcome",
    "Parked",
    "PauseIntent",
    "PauseNowIntent",
    "PerformanceRefusal",
    "PlanningAttrs",
    "PostIntentRequest",
    "PostIntentResponse",
    "ProbeState",
    "Progress",
    "ProviderSwitchAttrs",
    "ProviderSwitchReason",
    "ReadRequest",
    "ReadResponse",
    "Record",
    "RelaunchRefusal",
    "RelaunchRequest",
    "RelaunchResponse",
    "ReplayStore",
    "RunAddress",
    "RunDescription",
    "RunFailure",
    "RunFailureKind",
    "RunHead",
    "RunLiveness",
    "RunOutcomePresentation",
    "RunProcess",
    "Running",
    "RuntimeType",
    "SearchEnded",
    "SearchRunDescription",
    "SearchRunFailure",
    "Selection",
    "SeriesPerformance",
    "SeriesRole",
    "SetParallelismIntent",
    "Severity",
    "SkeletonRequest",
    "SkeletonResponse",
    "Span",
    "SpanFailureKind",
    "SpanHistory",
    "SpanKindDecl",
    "SpanLink",
    "SpanRecord",
    "SpanStatus",
    "SpanVersion",
    "StageState",
    "Starting",
    "StartupStage",
    "SteerIntent",
    "SteerMode",
    "SteerTarget",
    "SteerTargetKind",
    "StepCancelReason",
    "StepFinished",
    "StepStarted",
    "Stop",
    "StreamKind",
    "StreamOwner",
    "StreamRef",
    "StreamState",
    "TailRequest",
    "TailResponse",
    "TailStreamRequest",
    "TailStreamResponse",
    "Timestamp",
    "TokenUsage",
    "TranscriptChannel",
    "TranscriptContent",
    "TranscriptEntry",
    "TranscriptText",
    "TranscriptToolCall",
    "TranscriptToolResult",
    "UnmeasuredInput",
    "ValidationError",
    "WaitCondition",
    "WaitKind",
    "WaitingOn",
    "WatchRequest",
    "WatchResponse",
    "WatchRunsRequest",
    "WatchRunsResponse",
    "WithdrawIntent",
    "WorkstreamAttrs",
    "WorkstreamFailure",
    "WorkstreamFailureKind",
    "admission",
    "compiler_values",
    "core_agent_facts",
    "core_common",
    "core_contracts",
    "core_control",
    "core_failures",
    "core_iterations",
    "core_leaves",
    "core_value_options",
    "core_vocabulary",
    "desktop_gateway",
    "desktop_settings",
    "evaluation_receipts",
    "from_mapping",
    "host_operation_descriptors",
    "host_operations",
    "host_payloads",
    "imported_facts",
    "operation_options",
    "outcome_presentation",
    "replay",
    "replay_agent",
    "replay_evaluation",
    "replay_workspace",
    "schema_hash",
    "serial_effects",
    "to_mapping",
    "validate",
    "validated_operation_bytes",
    "validation_rules",
]


def __getattr__(name: str) -> object:
    # Fixture-only codecs must not enter the host hello's production import graph.
    if name in {"ClientReplay", "ClientReplayBatch"}:
        return getattr(
            import_module("vs_proto._generated.vibesys.testing.v1.client_replay_pb"), name
        )
    raise AttributeError(name)
