import datetime

from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from server.wire.v2 import common_pb2 as _common_pb2
from server.wire.v2 import snapshot_pb2 as _snapshot_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EventType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EVENT_TYPE_UNSPECIFIED: _ClassVar[EventType]
    EVENT_TYPE_SERVER_STARTED: _ClassVar[EventType]
    EVENT_TYPE_SERVER_READY: _ClassVar[EventType]
    EVENT_TYPE_CONFIGURATION_FAILED: _ClassVar[EventType]
    EVENT_TYPE_RUN_STARTED: _ClassVar[EventType]
    EVENT_TYPE_EXPERIMENTS_CHANGED: _ClassVar[EventType]
    EVENT_TYPE_RUN_INTERRUPTED: _ClassVar[EventType]
    EVENT_TYPE_RUN_STATUS_CHANGED: _ClassVar[EventType]
    EVENT_TYPE_CHAT: _ClassVar[EventType]
    EVENT_TYPE_CHAT_THREAD_CREATED: _ClassVar[EventType]
    EVENT_TYPE_STATUS_QUERY: _ClassVar[EventType]
    EVENT_TYPE_CONTROL: _ClassVar[EventType]
    EVENT_TYPE_INVOCATION_STARTED: _ClassVar[EventType]
    EVENT_TYPE_INVOCATION_FINISHED: _ClassVar[EventType]
    EVENT_TYPE_AGENT_EXECUTION_STARTED: _ClassVar[EventType]
    EVENT_TYPE_AGENT_EXECUTION_ACTIVITY_CHANGED: _ClassVar[EventType]
    EVENT_TYPE_AGENT_EXECUTION_FINISHED: _ClassVar[EventType]
    EVENT_TYPE_PHASE_STARTED: _ClassVar[EventType]
    EVENT_TYPE_PHASE_FINISHED: _ClassVar[EventType]
    EVENT_TYPE_AGENT_OUTPUT_CHUNK: _ClassVar[EventType]
    EVENT_TYPE_SUBPROCESS_OUTPUT: _ClassVar[EventType]
    EVENT_TYPE_JUDGE_RESULT: _ClassVar[EventType]
    EVENT_TYPE_BENCHMARK_RESULT: _ClassVar[EventType]
    EVENT_TYPE_ROUND_FINISHED: _ClassVar[EventType]
    EVENT_TYPE_RUN_FINISHED: _ClassVar[EventType]
    EVENT_TYPE_RUN_FAILED: _ClassVar[EventType]
    EVENT_TYPE_OUTPUT: _ClassVar[EventType]
    EVENT_TYPE_TOOL_CALL: _ClassVar[EventType]
    EVENT_TYPE_TOOL_RESULT: _ClassVar[EventType]
    EVENT_TYPE_TODO_UPDATE: _ClassVar[EventType]
    EVENT_TYPE_USAGE_UPDATE: _ClassVar[EventType]
    EVENT_TYPE_GATE_STARTED: _ClassVar[EventType]
    EVENT_TYPE_GATE_FINISHED: _ClassVar[EventType]
    EVENT_TYPE_WORKSPACE_SNAPSHOT: _ClassVar[EventType]
    EVENT_TYPE_RUN_CONFIGURED: _ClassVar[EventType]
    EVENT_TYPE_FRAMEWORK_WARNING: _ClassVar[EventType]

class EventStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EVENT_STATUS_UNSPECIFIED: _ClassVar[EventStatus]
    EVENT_STATUS_ACTIVE: _ClassVar[EventStatus]
    EVENT_STATUS_ANSWERED: _ClassVar[EventStatus]
    EVENT_STATUS_PENDING: _ClassVar[EventStatus]
    EVENT_STATUS_CONSUMED: _ClassVar[EventStatus]
    EVENT_STATUS_COMPLETED: _ClassVar[EventStatus]
    EVENT_STATUS_FAILED: _ClassVar[EventStatus]
    EVENT_STATUS_CANCELLED: _ClassVar[EventStatus]
    EVENT_STATUS_INTERRUPTED: _ClassVar[EventStatus]

class OutputStream(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OUTPUT_STREAM_UNSPECIFIED: _ClassVar[OutputStream]
    OUTPUT_STREAM_STDOUT: _ClassVar[OutputStream]
    OUTPUT_STREAM_STDERR: _ClassVar[OutputStream]

class AgentOutputChannel(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AGENT_OUTPUT_CHANNEL_UNSPECIFIED: _ClassVar[AgentOutputChannel]
    AGENT_OUTPUT_CHANNEL_ASSISTANT: _ClassVar[AgentOutputChannel]
    AGENT_OUTPUT_CHANNEL_ANALYSIS: _ClassVar[AgentOutputChannel]
    AGENT_OUTPUT_CHANNEL_TOOL: _ClassVar[AgentOutputChannel]
    AGENT_OUTPUT_CHANNEL_DIAGNOSTIC: _ClassVar[AgentOutputChannel]
    AGENT_OUTPUT_CHANNEL_PROMPT: _ClassVar[AgentOutputChannel]

class GateKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    GATE_KIND_UNSPECIFIED: _ClassVar[GateKind]
    GATE_KIND_VALIDATION: _ClassVar[GateKind]
    GATE_KIND_ACCURACY: _ClassVar[GateKind]
    GATE_KIND_BENCHMARK: _ClassVar[GateKind]

class FrameworkSource(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FRAMEWORK_SOURCE_UNSPECIFIED: _ClassVar[FrameworkSource]
    FRAMEWORK_SOURCE_GATES: _ClassVar[FrameworkSource]
    FRAMEWORK_SOURCE_GIT_TRACKING: _ClassVar[FrameworkSource]
    FRAMEWORK_SOURCE_LOOP: _ClassVar[FrameworkSource]
    FRAMEWORK_SOURCE_GPU: _ClassVar[FrameworkSource]
    FRAMEWORK_SOURCE_SKYPILOT: _ClassVar[FrameworkSource]
    FRAMEWORK_SOURCE_OTHER: _ClassVar[FrameworkSource]

class ExperimentsChangeReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EXPERIMENTS_CHANGE_REASON_UNSPECIFIED: _ClassVar[ExperimentsChangeReason]
    EXPERIMENTS_CHANGE_REASON_PROJECT_ATTACHED: _ClassVar[ExperimentsChangeReason]
    EXPERIMENTS_CHANGE_REASON_ACTIVE_HYPOTHESIS_CHANGED: _ClassVar[ExperimentsChangeReason]
    EXPERIMENTS_CHANGE_REASON_ROUND_PERSISTED: _ClassVar[ExperimentsChangeReason]

class JudgeVerdict(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    JUDGE_VERDICT_UNSPECIFIED: _ClassVar[JudgeVerdict]
    JUDGE_VERDICT_PASS: _ClassVar[JudgeVerdict]
    JUDGE_VERDICT_FAIL: _ClassVar[JudgeVerdict]

class RoundJudgeVerdict(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ROUND_JUDGE_VERDICT_UNSPECIFIED: _ClassVar[RoundJudgeVerdict]
    ROUND_JUDGE_VERDICT_PASS: _ClassVar[RoundJudgeVerdict]
    ROUND_JUDGE_VERDICT_FAIL: _ClassVar[RoundJudgeVerdict]
    ROUND_JUDGE_VERDICT_SKIPPED: _ClassVar[RoundJudgeVerdict]
EVENT_TYPE_UNSPECIFIED: EventType
EVENT_TYPE_SERVER_STARTED: EventType
EVENT_TYPE_SERVER_READY: EventType
EVENT_TYPE_CONFIGURATION_FAILED: EventType
EVENT_TYPE_RUN_STARTED: EventType
EVENT_TYPE_EXPERIMENTS_CHANGED: EventType
EVENT_TYPE_RUN_INTERRUPTED: EventType
EVENT_TYPE_RUN_STATUS_CHANGED: EventType
EVENT_TYPE_CHAT: EventType
EVENT_TYPE_CHAT_THREAD_CREATED: EventType
EVENT_TYPE_STATUS_QUERY: EventType
EVENT_TYPE_CONTROL: EventType
EVENT_TYPE_INVOCATION_STARTED: EventType
EVENT_TYPE_INVOCATION_FINISHED: EventType
EVENT_TYPE_AGENT_EXECUTION_STARTED: EventType
EVENT_TYPE_AGENT_EXECUTION_ACTIVITY_CHANGED: EventType
EVENT_TYPE_AGENT_EXECUTION_FINISHED: EventType
EVENT_TYPE_PHASE_STARTED: EventType
EVENT_TYPE_PHASE_FINISHED: EventType
EVENT_TYPE_AGENT_OUTPUT_CHUNK: EventType
EVENT_TYPE_SUBPROCESS_OUTPUT: EventType
EVENT_TYPE_JUDGE_RESULT: EventType
EVENT_TYPE_BENCHMARK_RESULT: EventType
EVENT_TYPE_ROUND_FINISHED: EventType
EVENT_TYPE_RUN_FINISHED: EventType
EVENT_TYPE_RUN_FAILED: EventType
EVENT_TYPE_OUTPUT: EventType
EVENT_TYPE_TOOL_CALL: EventType
EVENT_TYPE_TOOL_RESULT: EventType
EVENT_TYPE_TODO_UPDATE: EventType
EVENT_TYPE_USAGE_UPDATE: EventType
EVENT_TYPE_GATE_STARTED: EventType
EVENT_TYPE_GATE_FINISHED: EventType
EVENT_TYPE_WORKSPACE_SNAPSHOT: EventType
EVENT_TYPE_RUN_CONFIGURED: EventType
EVENT_TYPE_FRAMEWORK_WARNING: EventType
EVENT_STATUS_UNSPECIFIED: EventStatus
EVENT_STATUS_ACTIVE: EventStatus
EVENT_STATUS_ANSWERED: EventStatus
EVENT_STATUS_PENDING: EventStatus
EVENT_STATUS_CONSUMED: EventStatus
EVENT_STATUS_COMPLETED: EventStatus
EVENT_STATUS_FAILED: EventStatus
EVENT_STATUS_CANCELLED: EventStatus
EVENT_STATUS_INTERRUPTED: EventStatus
OUTPUT_STREAM_UNSPECIFIED: OutputStream
OUTPUT_STREAM_STDOUT: OutputStream
OUTPUT_STREAM_STDERR: OutputStream
AGENT_OUTPUT_CHANNEL_UNSPECIFIED: AgentOutputChannel
AGENT_OUTPUT_CHANNEL_ASSISTANT: AgentOutputChannel
AGENT_OUTPUT_CHANNEL_ANALYSIS: AgentOutputChannel
AGENT_OUTPUT_CHANNEL_TOOL: AgentOutputChannel
AGENT_OUTPUT_CHANNEL_DIAGNOSTIC: AgentOutputChannel
AGENT_OUTPUT_CHANNEL_PROMPT: AgentOutputChannel
GATE_KIND_UNSPECIFIED: GateKind
GATE_KIND_VALIDATION: GateKind
GATE_KIND_ACCURACY: GateKind
GATE_KIND_BENCHMARK: GateKind
FRAMEWORK_SOURCE_UNSPECIFIED: FrameworkSource
FRAMEWORK_SOURCE_GATES: FrameworkSource
FRAMEWORK_SOURCE_GIT_TRACKING: FrameworkSource
FRAMEWORK_SOURCE_LOOP: FrameworkSource
FRAMEWORK_SOURCE_GPU: FrameworkSource
FRAMEWORK_SOURCE_SKYPILOT: FrameworkSource
FRAMEWORK_SOURCE_OTHER: FrameworkSource
EXPERIMENTS_CHANGE_REASON_UNSPECIFIED: ExperimentsChangeReason
EXPERIMENTS_CHANGE_REASON_PROJECT_ATTACHED: ExperimentsChangeReason
EXPERIMENTS_CHANGE_REASON_ACTIVE_HYPOTHESIS_CHANGED: ExperimentsChangeReason
EXPERIMENTS_CHANGE_REASON_ROUND_PERSISTED: ExperimentsChangeReason
JUDGE_VERDICT_UNSPECIFIED: JudgeVerdict
JUDGE_VERDICT_PASS: JudgeVerdict
JUDGE_VERDICT_FAIL: JudgeVerdict
ROUND_JUDGE_VERDICT_UNSPECIFIED: RoundJudgeVerdict
ROUND_JUDGE_VERDICT_PASS: RoundJudgeVerdict
ROUND_JUDGE_VERDICT_FAIL: RoundJudgeVerdict
ROUND_JUDGE_VERDICT_SKIPPED: RoundJudgeVerdict

class ChatData(_message.Message):
    __slots__ = ("answer", "thread_title", "invocation_id")
    ANSWER_FIELD_NUMBER: _ClassVar[int]
    THREAD_TITLE_FIELD_NUMBER: _ClassVar[int]
    INVOCATION_ID_FIELD_NUMBER: _ClassVar[int]
    answer: str
    thread_title: str
    invocation_id: str
    def __init__(self, answer: _Optional[str] = ..., thread_title: _Optional[str] = ..., invocation_id: _Optional[str] = ...) -> None: ...

class ChatThreadCreatedData(_message.Message):
    __slots__ = ("thread_id", "title", "driver", "provider", "model", "created_at")
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    DRIVER_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    thread_id: str
    title: str
    driver: str
    provider: str
    model: str
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, thread_id: _Optional[str] = ..., title: _Optional[str] = ..., driver: _Optional[str] = ..., provider: _Optional[str] = ..., model: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class InvocationStartedData(_message.Message):
    __slots__ = ("system_prompt", "user_prompt")
    SYSTEM_PROMPT_FIELD_NUMBER: _ClassVar[int]
    USER_PROMPT_FIELD_NUMBER: _ClassVar[int]
    system_prompt: str
    user_prompt: str
    def __init__(self, system_prompt: _Optional[str] = ..., user_prompt: _Optional[str] = ...) -> None: ...

class InvocationFinishedData(_message.Message):
    __slots__ = ("result", "error")
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    result: _struct_pb2.Value
    error: str
    def __init__(self, result: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., error: _Optional[str] = ...) -> None: ...

class AgentExecutionStartedData(_message.Message):
    __slots__ = ("stage", "attempt", "system_prompt", "user_prompt", "activity", "driver", "provider", "model")
    STAGE_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_PROMPT_FIELD_NUMBER: _ClassVar[int]
    USER_PROMPT_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_FIELD_NUMBER: _ClassVar[int]
    DRIVER_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    stage: str
    attempt: int
    system_prompt: str
    user_prompt: str
    activity: _snapshot_pb2.AgentExecutionActivityData
    driver: str
    provider: str
    model: str
    def __init__(self, stage: _Optional[str] = ..., attempt: _Optional[int] = ..., system_prompt: _Optional[str] = ..., user_prompt: _Optional[str] = ..., activity: _Optional[_Union[_snapshot_pb2.AgentExecutionActivityData, _Mapping]] = ..., driver: _Optional[str] = ..., provider: _Optional[str] = ..., model: _Optional[str] = ...) -> None: ...

class AgentExecutionFinishedData(_message.Message):
    __slots__ = ("result", "error")
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    result: _struct_pb2.Value
    error: str
    def __init__(self, result: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., error: _Optional[str] = ...) -> None: ...

class OutputData(_message.Message):
    __slots__ = ("stream", "source", "content")
    STREAM_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    stream: OutputStream
    source: str
    content: str
    def __init__(self, stream: _Optional[_Union[OutputStream, str]] = ..., source: _Optional[str] = ..., content: _Optional[str] = ...) -> None: ...

class ServerReadyData(_message.Message):
    __slots__ = ("socket_protocol",)
    SOCKET_PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    socket_protocol: str
    def __init__(self, socket_protocol: _Optional[str] = ...) -> None: ...

class RunStartedData(_message.Message):
    __slots__ = ("outer_loop", "input", "max_rounds", "expected_roles")
    OUTER_LOOP_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    MAX_ROUNDS_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_ROLES_FIELD_NUMBER: _ClassVar[int]
    outer_loop: str
    input: str
    max_rounds: int
    expected_roles: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, outer_loop: _Optional[str] = ..., input: _Optional[str] = ..., max_rounds: _Optional[int] = ..., expected_roles: _Optional[_Iterable[str]] = ...) -> None: ...

class RunInterruptedData(_message.Message):
    __slots__ = ("reason", "signal")
    REASON_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    reason: str
    signal: str
    def __init__(self, reason: _Optional[str] = ..., signal: _Optional[str] = ...) -> None: ...

class RunStatusChangedData(_message.Message):
    __slots__ = ("status", "previous")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_FIELD_NUMBER: _ClassVar[int]
    status: _common_pb2.RunStatus
    previous: _common_pb2.RunStatus
    def __init__(self, status: _Optional[_Union[_common_pb2.RunStatus, str]] = ..., previous: _Optional[_Union[_common_pb2.RunStatus, str]] = ...) -> None: ...

class ExperimentsChangedData(_message.Message):
    __slots__ = ("reason", "revision")
    REASON_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    reason: ExperimentsChangeReason
    revision: int
    def __init__(self, reason: _Optional[_Union[ExperimentsChangeReason, str]] = ..., revision: _Optional[int] = ...) -> None: ...

class ConfigurationFailedData(_message.Message):
    __slots__ = ("code", "stage", "message", "usage", "exit_code")
    CODE_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    code: str
    stage: str
    message: str
    usage: str
    exit_code: int
    def __init__(self, code: _Optional[str] = ..., stage: _Optional[str] = ..., message: _Optional[str] = ..., usage: _Optional[str] = ..., exit_code: _Optional[int] = ...) -> None: ...

class PhaseData(_message.Message):
    __slots__ = ("phase", "attempt")
    PHASE_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    phase: str
    attempt: int
    def __init__(self, phase: _Optional[str] = ..., attempt: _Optional[int] = ...) -> None: ...

class AgentStatusData(_message.Message):
    __slots__ = ("progress", "agent_label", "elapsed_seconds", "input_tokens", "context_window")
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    AGENT_LABEL_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_SECONDS_FIELD_NUMBER: _ClassVar[int]
    INPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_WINDOW_FIELD_NUMBER: _ClassVar[int]
    progress: str
    agent_label: str
    elapsed_seconds: float
    input_tokens: int
    context_window: int
    def __init__(self, progress: _Optional[str] = ..., agent_label: _Optional[str] = ..., elapsed_seconds: _Optional[float] = ..., input_tokens: _Optional[int] = ..., context_window: _Optional[int] = ...) -> None: ...

class AgentOutputChunkData(_message.Message):
    __slots__ = ("channel", "content", "status")
    CHANNEL_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    channel: AgentOutputChannel
    content: str
    status: AgentStatusData
    def __init__(self, channel: _Optional[_Union[AgentOutputChannel, str]] = ..., content: _Optional[str] = ..., status: _Optional[_Union[AgentStatusData, _Mapping]] = ...) -> None: ...

class ToolCallData(_message.Message):
    __slots__ = ("tool", "call_id", "args", "status")
    TOOL_FIELD_NUMBER: _ClassVar[int]
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    tool: str
    call_id: str
    args: _struct_pb2.Struct
    status: AgentStatusData
    def __init__(self, tool: _Optional[str] = ..., call_id: _Optional[str] = ..., args: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., status: _Optional[_Union[AgentStatusData, _Mapping]] = ...) -> None: ...

class CommandResultPayload(_message.Message):
    __slots__ = ("stdout", "stderr", "exit_code", "duration")
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    stdout: str
    stderr: str
    exit_code: int
    duration: float
    def __init__(self, stdout: _Optional[str] = ..., stderr: _Optional[str] = ..., exit_code: _Optional[int] = ..., duration: _Optional[float] = ...) -> None: ...

class JsonResultPayload(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: _struct_pb2.Value
    def __init__(self, value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...) -> None: ...

class ToolResultData(_message.Message):
    __slots__ = ("tool", "call_id", "content", "is_error", "command", "json")
    TOOL_FIELD_NUMBER: _ClassVar[int]
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    IS_ERROR_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    JSON_FIELD_NUMBER: _ClassVar[int]
    tool: str
    call_id: str
    content: str
    is_error: bool
    command: CommandResultPayload
    json: JsonResultPayload
    def __init__(self, tool: _Optional[str] = ..., call_id: _Optional[str] = ..., content: _Optional[str] = ..., is_error: _Optional[bool] = ..., command: _Optional[_Union[CommandResultPayload, _Mapping]] = ..., json: _Optional[_Union[JsonResultPayload, _Mapping]] = ...) -> None: ...

class TodoItemData(_message.Message):
    __slots__ = ("content", "status")
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    content: str
    status: str
    def __init__(self, content: _Optional[str] = ..., status: _Optional[str] = ...) -> None: ...

class TodoUpdateData(_message.Message):
    __slots__ = ("todos",)
    TODOS_FIELD_NUMBER: _ClassVar[int]
    todos: _containers.RepeatedCompositeFieldContainer[TodoItemData]
    def __init__(self, todos: _Optional[_Iterable[_Union[TodoItemData, _Mapping]]] = ...) -> None: ...

class UsageUpdateData(_message.Message):
    __slots__ = ("input_tokens", "context_window", "model")
    INPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_WINDOW_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    input_tokens: int
    context_window: int
    model: str
    def __init__(self, input_tokens: _Optional[int] = ..., context_window: _Optional[int] = ..., model: _Optional[str] = ...) -> None: ...

class SubprocessOutputData(_message.Message):
    __slots__ = ("process_id", "process_kind", "stream", "content")
    PROCESS_ID_FIELD_NUMBER: _ClassVar[int]
    PROCESS_KIND_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    process_id: str
    process_kind: str
    stream: OutputStream
    content: str
    def __init__(self, process_id: _Optional[str] = ..., process_kind: _Optional[str] = ..., stream: _Optional[_Union[OutputStream, str]] = ..., content: _Optional[str] = ...) -> None: ...

class JudgeResultData(_message.Message):
    __slots__ = ("verdict", "feedback", "attempt")
    VERDICT_FIELD_NUMBER: _ClassVar[int]
    FEEDBACK_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    verdict: JudgeVerdict
    feedback: str
    attempt: int
    def __init__(self, verdict: _Optional[_Union[JudgeVerdict, str]] = ..., feedback: _Optional[str] = ..., attempt: _Optional[int] = ...) -> None: ...

class BenchmarkResultData(_message.Message):
    __slots__ = ("metric", "value", "unit")
    METRIC_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    metric: str
    value: float
    unit: str
    def __init__(self, metric: _Optional[str] = ..., value: _Optional[float] = ..., unit: _Optional[str] = ...) -> None: ...

class RoundFinishedData(_message.Message):
    __slots__ = ("attempts", "judge_verdict", "perf_metric", "perf_unit", "profile_skipped")
    ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    JUDGE_VERDICT_FIELD_NUMBER: _ClassVar[int]
    PERF_METRIC_FIELD_NUMBER: _ClassVar[int]
    PERF_UNIT_FIELD_NUMBER: _ClassVar[int]
    PROFILE_SKIPPED_FIELD_NUMBER: _ClassVar[int]
    attempts: int
    judge_verdict: RoundJudgeVerdict
    perf_metric: float
    perf_unit: str
    profile_skipped: bool
    def __init__(self, attempts: _Optional[int] = ..., judge_verdict: _Optional[_Union[RoundJudgeVerdict, str]] = ..., perf_metric: _Optional[float] = ..., perf_unit: _Optional[str] = ..., profile_skipped: _Optional[bool] = ...) -> None: ...

class GateStartedData(_message.Message):
    __slots__ = ("gate", "recipe", "command", "source", "source_label")
    GATE_FIELD_NUMBER: _ClassVar[int]
    RECIPE_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_LABEL_FIELD_NUMBER: _ClassVar[int]
    gate: GateKind
    recipe: str
    command: str
    source: FrameworkSource
    source_label: str
    def __init__(self, gate: _Optional[_Union[GateKind, str]] = ..., recipe: _Optional[str] = ..., command: _Optional[str] = ..., source: _Optional[_Union[FrameworkSource, str]] = ..., source_label: _Optional[str] = ...) -> None: ...

class GateFinishedData(_message.Message):
    __slots__ = ("gate", "recipe", "reused", "metric", "value", "unit", "output_tail", "source", "source_label")
    GATE_FIELD_NUMBER: _ClassVar[int]
    RECIPE_FIELD_NUMBER: _ClassVar[int]
    REUSED_FIELD_NUMBER: _ClassVar[int]
    METRIC_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_TAIL_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_LABEL_FIELD_NUMBER: _ClassVar[int]
    gate: GateKind
    recipe: str
    reused: bool
    metric: str
    value: float
    unit: str
    output_tail: str
    source: FrameworkSource
    source_label: str
    def __init__(self, gate: _Optional[_Union[GateKind, str]] = ..., recipe: _Optional[str] = ..., reused: _Optional[bool] = ..., metric: _Optional[str] = ..., value: _Optional[float] = ..., unit: _Optional[str] = ..., output_tail: _Optional[str] = ..., source: _Optional[_Union[FrameworkSource, str]] = ..., source_label: _Optional[str] = ...) -> None: ...

class WorkspaceSnapshotData(_message.Message):
    __slots__ = ("label", "commit", "baseline", "excluded_paths", "source")
    LABEL_FIELD_NUMBER: _ClassVar[int]
    COMMIT_FIELD_NUMBER: _ClassVar[int]
    BASELINE_FIELD_NUMBER: _ClassVar[int]
    EXCLUDED_PATHS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    label: str
    commit: str
    baseline: str
    excluded_paths: _containers.RepeatedScalarFieldContainer[str]
    source: FrameworkSource
    def __init__(self, label: _Optional[str] = ..., commit: _Optional[str] = ..., baseline: _Optional[str] = ..., excluded_paths: _Optional[_Iterable[str]] = ..., source: _Optional[_Union[FrameworkSource, str]] = ...) -> None: ...

class RunConfiguredData(_message.Message):
    __slots__ = ("run_log_path", "project_root", "model", "objective", "search_policy", "benchmark_contract", "pareto_objectives", "source")
    RUN_LOG_PATH_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ROOT_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    OBJECTIVE_FIELD_NUMBER: _ClassVar[int]
    SEARCH_POLICY_FIELD_NUMBER: _ClassVar[int]
    BENCHMARK_CONTRACT_FIELD_NUMBER: _ClassVar[int]
    PARETO_OBJECTIVES_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    run_log_path: str
    project_root: str
    model: str
    objective: str
    search_policy: str
    benchmark_contract: bool
    pareto_objectives: str
    source: FrameworkSource
    def __init__(self, run_log_path: _Optional[str] = ..., project_root: _Optional[str] = ..., model: _Optional[str] = ..., objective: _Optional[str] = ..., search_policy: _Optional[str] = ..., benchmark_contract: _Optional[bool] = ..., pareto_objectives: _Optional[str] = ..., source: _Optional[_Union[FrameworkSource, str]] = ...) -> None: ...

class FrameworkWarningData(_message.Message):
    __slots__ = ("summary", "detail", "source", "source_label")
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_LABEL_FIELD_NUMBER: _ClassVar[int]
    summary: str
    detail: str
    source: FrameworkSource
    source_label: str
    def __init__(self, summary: _Optional[str] = ..., detail: _Optional[str] = ..., source: _Optional[_Union[FrameworkSource, str]] = ..., source_label: _Optional[str] = ...) -> None: ...

class RunEvent(_message.Message):
    __slots__ = ("protocol_version", "sequence", "run_id", "timestamp", "type", "text", "diagnostic", "status", "round_label", "agent_kind", "execution_id", "chat_thread_id", "chat", "chat_thread_created", "invocation_started", "invocation_finished", "agent_execution_started", "agent_execution_activity_changed", "agent_execution_finished", "output", "server_ready", "run_started", "run_interrupted", "run_status_changed", "experiments_changed", "configuration_failed", "phase", "agent_output_chunk", "subprocess_output", "judge_result", "benchmark_result", "round_finished", "tool_call", "tool_result", "todo_update", "usage_update", "gate_started", "gate_finished", "workspace_snapshot", "run_configured", "framework_warning")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    DIAGNOSTIC_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ROUND_LABEL_FIELD_NUMBER: _ClassVar[int]
    AGENT_KIND_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    CHAT_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    CHAT_FIELD_NUMBER: _ClassVar[int]
    CHAT_THREAD_CREATED_FIELD_NUMBER: _ClassVar[int]
    INVOCATION_STARTED_FIELD_NUMBER: _ClassVar[int]
    INVOCATION_FINISHED_FIELD_NUMBER: _ClassVar[int]
    AGENT_EXECUTION_STARTED_FIELD_NUMBER: _ClassVar[int]
    AGENT_EXECUTION_ACTIVITY_CHANGED_FIELD_NUMBER: _ClassVar[int]
    AGENT_EXECUTION_FINISHED_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    SERVER_READY_FIELD_NUMBER: _ClassVar[int]
    RUN_STARTED_FIELD_NUMBER: _ClassVar[int]
    RUN_INTERRUPTED_FIELD_NUMBER: _ClassVar[int]
    RUN_STATUS_CHANGED_FIELD_NUMBER: _ClassVar[int]
    EXPERIMENTS_CHANGED_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_FAILED_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    AGENT_OUTPUT_CHUNK_FIELD_NUMBER: _ClassVar[int]
    SUBPROCESS_OUTPUT_FIELD_NUMBER: _ClassVar[int]
    JUDGE_RESULT_FIELD_NUMBER: _ClassVar[int]
    BENCHMARK_RESULT_FIELD_NUMBER: _ClassVar[int]
    ROUND_FINISHED_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_FIELD_NUMBER: _ClassVar[int]
    TOOL_RESULT_FIELD_NUMBER: _ClassVar[int]
    TODO_UPDATE_FIELD_NUMBER: _ClassVar[int]
    USAGE_UPDATE_FIELD_NUMBER: _ClassVar[int]
    GATE_STARTED_FIELD_NUMBER: _ClassVar[int]
    GATE_FINISHED_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    RUN_CONFIGURED_FIELD_NUMBER: _ClassVar[int]
    FRAMEWORK_WARNING_FIELD_NUMBER: _ClassVar[int]
    protocol_version: int
    sequence: int
    run_id: str
    timestamp: _timestamp_pb2.Timestamp
    type: EventType
    text: str
    diagnostic: _common_pb2.Diagnostic
    status: EventStatus
    round_label: str
    agent_kind: str
    execution_id: str
    chat_thread_id: str
    chat: ChatData
    chat_thread_created: ChatThreadCreatedData
    invocation_started: InvocationStartedData
    invocation_finished: InvocationFinishedData
    agent_execution_started: AgentExecutionStartedData
    agent_execution_activity_changed: _snapshot_pb2.AgentExecutionActivityData
    agent_execution_finished: AgentExecutionFinishedData
    output: OutputData
    server_ready: ServerReadyData
    run_started: RunStartedData
    run_interrupted: RunInterruptedData
    run_status_changed: RunStatusChangedData
    experiments_changed: ExperimentsChangedData
    configuration_failed: ConfigurationFailedData
    phase: PhaseData
    agent_output_chunk: AgentOutputChunkData
    subprocess_output: SubprocessOutputData
    judge_result: JudgeResultData
    benchmark_result: BenchmarkResultData
    round_finished: RoundFinishedData
    tool_call: ToolCallData
    tool_result: ToolResultData
    todo_update: TodoUpdateData
    usage_update: UsageUpdateData
    gate_started: GateStartedData
    gate_finished: GateFinishedData
    workspace_snapshot: WorkspaceSnapshotData
    run_configured: RunConfiguredData
    framework_warning: FrameworkWarningData
    def __init__(self, protocol_version: _Optional[int] = ..., sequence: _Optional[int] = ..., run_id: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., type: _Optional[_Union[EventType, str]] = ..., text: _Optional[str] = ..., diagnostic: _Optional[_Union[_common_pb2.Diagnostic, _Mapping]] = ..., status: _Optional[_Union[EventStatus, str]] = ..., round_label: _Optional[str] = ..., agent_kind: _Optional[str] = ..., execution_id: _Optional[str] = ..., chat_thread_id: _Optional[str] = ..., chat: _Optional[_Union[ChatData, _Mapping]] = ..., chat_thread_created: _Optional[_Union[ChatThreadCreatedData, _Mapping]] = ..., invocation_started: _Optional[_Union[InvocationStartedData, _Mapping]] = ..., invocation_finished: _Optional[_Union[InvocationFinishedData, _Mapping]] = ..., agent_execution_started: _Optional[_Union[AgentExecutionStartedData, _Mapping]] = ..., agent_execution_activity_changed: _Optional[_Union[_snapshot_pb2.AgentExecutionActivityData, _Mapping]] = ..., agent_execution_finished: _Optional[_Union[AgentExecutionFinishedData, _Mapping]] = ..., output: _Optional[_Union[OutputData, _Mapping]] = ..., server_ready: _Optional[_Union[ServerReadyData, _Mapping]] = ..., run_started: _Optional[_Union[RunStartedData, _Mapping]] = ..., run_interrupted: _Optional[_Union[RunInterruptedData, _Mapping]] = ..., run_status_changed: _Optional[_Union[RunStatusChangedData, _Mapping]] = ..., experiments_changed: _Optional[_Union[ExperimentsChangedData, _Mapping]] = ..., configuration_failed: _Optional[_Union[ConfigurationFailedData, _Mapping]] = ..., phase: _Optional[_Union[PhaseData, _Mapping]] = ..., agent_output_chunk: _Optional[_Union[AgentOutputChunkData, _Mapping]] = ..., subprocess_output: _Optional[_Union[SubprocessOutputData, _Mapping]] = ..., judge_result: _Optional[_Union[JudgeResultData, _Mapping]] = ..., benchmark_result: _Optional[_Union[BenchmarkResultData, _Mapping]] = ..., round_finished: _Optional[_Union[RoundFinishedData, _Mapping]] = ..., tool_call: _Optional[_Union[ToolCallData, _Mapping]] = ..., tool_result: _Optional[_Union[ToolResultData, _Mapping]] = ..., todo_update: _Optional[_Union[TodoUpdateData, _Mapping]] = ..., usage_update: _Optional[_Union[UsageUpdateData, _Mapping]] = ..., gate_started: _Optional[_Union[GateStartedData, _Mapping]] = ..., gate_finished: _Optional[_Union[GateFinishedData, _Mapping]] = ..., workspace_snapshot: _Optional[_Union[WorkspaceSnapshotData, _Mapping]] = ..., run_configured: _Optional[_Union[RunConfiguredData, _Mapping]] = ..., framework_warning: _Optional[_Union[FrameworkWarningData, _Mapping]] = ...) -> None: ...
