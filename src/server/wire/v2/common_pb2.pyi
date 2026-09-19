from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RunStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RUN_STATUS_UNSPECIFIED: _ClassVar[RunStatus]
    RUN_STATUS_STARTING: _ClassVar[RunStatus]
    RUN_STATUS_RUNNING: _ClassVar[RunStatus]
    RUN_STATUS_PAUSING: _ClassVar[RunStatus]
    RUN_STATUS_PAUSED: _ClassVar[RunStatus]
    RUN_STATUS_STOPPING: _ClassVar[RunStatus]
    RUN_STATUS_STOPPED: _ClassVar[RunStatus]
    RUN_STATUS_COMPLETED: _ClassVar[RunStatus]
    RUN_STATUS_FAILED: _ClassVar[RunStatus]

class DiagnosticScope(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DIAGNOSTIC_SCOPE_UNSPECIFIED: _ClassVar[DiagnosticScope]
    DIAGNOSTIC_SCOPE_CONFIGURATION: _ClassVar[DiagnosticScope]
    DIAGNOSTIC_SCOPE_INVOCATION: _ClassVar[DiagnosticScope]
    DIAGNOSTIC_SCOPE_PHASE: _ClassVar[DiagnosticScope]
    DIAGNOSTIC_SCOPE_RUN: _ClassVar[DiagnosticScope]
    DIAGNOSTIC_SCOPE_REQUEST: _ClassVar[DiagnosticScope]
    DIAGNOSTIC_SCOPE_PROTOCOL: _ClassVar[DiagnosticScope]
    DIAGNOSTIC_SCOPE_TRANSPORT: _ClassVar[DiagnosticScope]

class DiagnosticSeverity(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DIAGNOSTIC_SEVERITY_UNSPECIFIED: _ClassVar[DiagnosticSeverity]
    DIAGNOSTIC_SEVERITY_WARNING: _ClassVar[DiagnosticSeverity]
    DIAGNOSTIC_SEVERITY_ERROR: _ClassVar[DiagnosticSeverity]
    DIAGNOSTIC_SEVERITY_FATAL: _ClassVar[DiagnosticSeverity]

class DiagnosticRetryability(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DIAGNOSTIC_RETRYABILITY_UNSPECIFIED: _ClassVar[DiagnosticRetryability]
    DIAGNOSTIC_RETRYABILITY_AUTOMATIC: _ClassVar[DiagnosticRetryability]
    DIAGNOSTIC_RETRYABILITY_MANUAL: _ClassVar[DiagnosticRetryability]
    DIAGNOSTIC_RETRYABILITY_NEVER: _ClassVar[DiagnosticRetryability]
    DIAGNOSTIC_RETRYABILITY_UNKNOWN: _ClassVar[DiagnosticRetryability]
RUN_STATUS_UNSPECIFIED: RunStatus
RUN_STATUS_STARTING: RunStatus
RUN_STATUS_RUNNING: RunStatus
RUN_STATUS_PAUSING: RunStatus
RUN_STATUS_PAUSED: RunStatus
RUN_STATUS_STOPPING: RunStatus
RUN_STATUS_STOPPED: RunStatus
RUN_STATUS_COMPLETED: RunStatus
RUN_STATUS_FAILED: RunStatus
DIAGNOSTIC_SCOPE_UNSPECIFIED: DiagnosticScope
DIAGNOSTIC_SCOPE_CONFIGURATION: DiagnosticScope
DIAGNOSTIC_SCOPE_INVOCATION: DiagnosticScope
DIAGNOSTIC_SCOPE_PHASE: DiagnosticScope
DIAGNOSTIC_SCOPE_RUN: DiagnosticScope
DIAGNOSTIC_SCOPE_REQUEST: DiagnosticScope
DIAGNOSTIC_SCOPE_PROTOCOL: DiagnosticScope
DIAGNOSTIC_SCOPE_TRANSPORT: DiagnosticScope
DIAGNOSTIC_SEVERITY_UNSPECIFIED: DiagnosticSeverity
DIAGNOSTIC_SEVERITY_WARNING: DiagnosticSeverity
DIAGNOSTIC_SEVERITY_ERROR: DiagnosticSeverity
DIAGNOSTIC_SEVERITY_FATAL: DiagnosticSeverity
DIAGNOSTIC_RETRYABILITY_UNSPECIFIED: DiagnosticRetryability
DIAGNOSTIC_RETRYABILITY_AUTOMATIC: DiagnosticRetryability
DIAGNOSTIC_RETRYABILITY_MANUAL: DiagnosticRetryability
DIAGNOSTIC_RETRYABILITY_NEVER: DiagnosticRetryability
DIAGNOSTIC_RETRYABILITY_UNKNOWN: DiagnosticRetryability

class Diagnostic(_message.Message):
    __slots__ = ("id", "code", "summary", "detail", "hint", "scope", "severity", "retryability", "cause_id", "debug_ref", "source")
    ID_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    HINT_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    SEVERITY_FIELD_NUMBER: _ClassVar[int]
    RETRYABILITY_FIELD_NUMBER: _ClassVar[int]
    CAUSE_ID_FIELD_NUMBER: _ClassVar[int]
    DEBUG_REF_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    id: str
    code: str
    summary: str
    detail: str
    hint: str
    scope: DiagnosticScope
    severity: DiagnosticSeverity
    retryability: DiagnosticRetryability
    cause_id: str
    debug_ref: str
    source: str
    def __init__(self, id: _Optional[str] = ..., code: _Optional[str] = ..., summary: _Optional[str] = ..., detail: _Optional[str] = ..., hint: _Optional[str] = ..., scope: _Optional[_Union[DiagnosticScope, str]] = ..., severity: _Optional[_Union[DiagnosticSeverity, str]] = ..., retryability: _Optional[_Union[DiagnosticRetryability, str]] = ..., cause_id: _Optional[str] = ..., debug_ref: _Optional[str] = ..., source: _Optional[str] = ...) -> None: ...
