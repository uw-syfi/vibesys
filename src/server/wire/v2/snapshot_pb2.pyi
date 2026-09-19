import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from server.wire.v2 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ExecutionActivityMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EXECUTION_ACTIVITY_MODE_UNSPECIFIED: _ClassVar[ExecutionActivityMode]
    EXECUTION_ACTIVITY_MODE_THINKING: _ClassVar[ExecutionActivityMode]
    EXECUTION_ACTIVITY_MODE_RESPONDING: _ClassVar[ExecutionActivityMode]
    EXECUTION_ACTIVITY_MODE_TOOL: _ClassVar[ExecutionActivityMode]
    EXECUTION_ACTIVITY_MODE_WAITING: _ClassVar[ExecutionActivityMode]
EXECUTION_ACTIVITY_MODE_UNSPECIFIED: ExecutionActivityMode
EXECUTION_ACTIVITY_MODE_THINKING: ExecutionActivityMode
EXECUTION_ACTIVITY_MODE_RESPONDING: ExecutionActivityMode
EXECUTION_ACTIVITY_MODE_TOOL: ExecutionActivityMode
EXECUTION_ACTIVITY_MODE_WAITING: ExecutionActivityMode

class AgentExecutionActivityData(_message.Message):
    __slots__ = ("mode", "summary", "tool")
    MODE_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    mode: ExecutionActivityMode
    summary: str
    tool: str
    def __init__(self, mode: _Optional[_Union[ExecutionActivityMode, str]] = ..., summary: _Optional[str] = ..., tool: _Optional[str] = ...) -> None: ...

class ActiveAgentExecution(_message.Message):
    __slots__ = ("execution_id", "agent_kind", "round_label", "stage", "attempt", "assignment", "started_at", "activity", "driver", "provider", "model")
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_KIND_FIELD_NUMBER: _ClassVar[int]
    ROUND_LABEL_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    ASSIGNMENT_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_FIELD_NUMBER: _ClassVar[int]
    DRIVER_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    agent_kind: str
    round_label: str
    stage: str
    attempt: int
    assignment: str
    started_at: _timestamp_pb2.Timestamp
    activity: AgentExecutionActivityData
    driver: str
    provider: str
    model: str
    def __init__(self, execution_id: _Optional[str] = ..., agent_kind: _Optional[str] = ..., round_label: _Optional[str] = ..., stage: _Optional[str] = ..., attempt: _Optional[int] = ..., assignment: _Optional[str] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., activity: _Optional[_Union[AgentExecutionActivityData, _Mapping]] = ..., driver: _Optional[str] = ..., provider: _Optional[str] = ..., model: _Optional[str] = ...) -> None: ...

class ChatThreadInfo(_message.Message):
    __slots__ = ("thread_id", "title", "driver", "provider", "model")
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    DRIVER_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    thread_id: str
    title: str
    driver: str
    provider: str
    model: str
    def __init__(self, thread_id: _Optional[str] = ..., title: _Optional[str] = ..., driver: _Optional[str] = ..., provider: _Optional[str] = ..., model: _Optional[str] = ...) -> None: ...

class RunSnapshot(_message.Message):
    __slots__ = ("protocol_version", "run_id", "sequence", "status", "agent_kind", "round_label", "active_executions", "chat_threads")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    AGENT_KIND_FIELD_NUMBER: _ClassVar[int]
    ROUND_LABEL_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_EXECUTIONS_FIELD_NUMBER: _ClassVar[int]
    CHAT_THREADS_FIELD_NUMBER: _ClassVar[int]
    protocol_version: int
    run_id: str
    sequence: int
    status: _common_pb2.RunStatus
    agent_kind: str
    round_label: str
    active_executions: _containers.RepeatedCompositeFieldContainer[ActiveAgentExecution]
    chat_threads: _containers.RepeatedCompositeFieldContainer[ChatThreadInfo]
    def __init__(self, protocol_version: _Optional[int] = ..., run_id: _Optional[str] = ..., sequence: _Optional[int] = ..., status: _Optional[_Union[_common_pb2.RunStatus, str]] = ..., agent_kind: _Optional[str] = ..., round_label: _Optional[str] = ..., active_executions: _Optional[_Iterable[_Union[ActiveAgentExecution, _Mapping]]] = ..., chat_threads: _Optional[_Iterable[_Union[ChatThreadInfo, _Mapping]]] = ...) -> None: ...
