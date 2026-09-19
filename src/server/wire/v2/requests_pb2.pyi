import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ChatDriver(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CHAT_DRIVER_UNSPECIFIED: _ClassVar[ChatDriver]
    CHAT_DRIVER_AGENTSHIM: _ClassVar[ChatDriver]
    CHAT_DRIVER_OMNIGENT: _ClassVar[ChatDriver]
CHAT_DRIVER_UNSPECIFIED: ChatDriver
CHAT_DRIVER_AGENTSHIM: ChatDriver
CHAT_DRIVER_OMNIGENT: ChatDriver

class PauseCommand(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ResumeCommand(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SteerCommand(_message.Message):
    __slots__ = ("text",)
    TEXT_FIELD_NUMBER: _ClassVar[int]
    text: str
    def __init__(self, text: _Optional[str] = ...) -> None: ...

class StopCommand(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SnapshotQuery(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ChatQuery(_message.Message):
    __slots__ = ("text", "thread_id")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    text: str
    thread_id: str
    def __init__(self, text: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class ChatThreadCreateQuery(_message.Message):
    __slots__ = ("driver", "provider", "model", "title")
    DRIVER_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    driver: ChatDriver
    provider: str
    model: str
    title: str
    def __init__(self, driver: _Optional[_Union[ChatDriver, str]] = ..., provider: _Optional[str] = ..., model: _Optional[str] = ..., title: _Optional[str] = ...) -> None: ...

class ChatOptionsQuery(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class TuiDefaultsQuery(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HistoryQuery(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PerformanceQuery(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ExperimentCursor(_message.Message):
    __slots__ = ("run_id", "projection_id", "revision")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    projection_id: str
    revision: int
    def __init__(self, run_id: _Optional[str] = ..., projection_id: _Optional[str] = ..., revision: _Optional[int] = ...) -> None: ...

class ExperimentQuery(_message.Message):
    __slots__ = ("after",)
    AFTER_FIELD_NUMBER: _ClassVar[int]
    after: ExperimentCursor
    def __init__(self, after: _Optional[_Union[ExperimentCursor, _Mapping]] = ...) -> None: ...

class DesignQuery(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class DesignPatchQuery(_message.Message):
    __slots__ = ("base", "head", "path")
    BASE_FIELD_NUMBER: _ClassVar[int]
    HEAD_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    base: str
    head: str
    path: str
    def __init__(self, base: _Optional[str] = ..., head: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class EventsQuery(_message.Message):
    __slots__ = ("after_sequence", "before_sequence", "timeout_ms")
    AFTER_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    BEFORE_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    after_sequence: int
    before_sequence: int
    timeout_ms: int
    def __init__(self, after_sequence: _Optional[int] = ..., before_sequence: _Optional[int] = ..., timeout_ms: _Optional[int] = ...) -> None: ...

class SubscribeRequest(_message.Message):
    __slots__ = ("after_sequence", "tail", "store_id")
    AFTER_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    TAIL_FIELD_NUMBER: _ClassVar[int]
    STORE_ID_FIELD_NUMBER: _ClassVar[int]
    after_sequence: int
    tail: int
    store_id: str
    def __init__(self, after_sequence: _Optional[int] = ..., tail: _Optional[int] = ..., store_id: _Optional[str] = ...) -> None: ...

class Request(_message.Message):
    __slots__ = ("protocol_version", "request_id", "timestamp", "pause", "resume", "steer", "stop", "snapshot", "chat", "chat_thread_create", "chat_options", "tui_defaults", "history", "performance", "experiments", "design", "design_patch", "events", "subscribe")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    PAUSE_FIELD_NUMBER: _ClassVar[int]
    RESUME_FIELD_NUMBER: _ClassVar[int]
    STEER_FIELD_NUMBER: _ClassVar[int]
    STOP_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    CHAT_FIELD_NUMBER: _ClassVar[int]
    CHAT_THREAD_CREATE_FIELD_NUMBER: _ClassVar[int]
    CHAT_OPTIONS_FIELD_NUMBER: _ClassVar[int]
    TUI_DEFAULTS_FIELD_NUMBER: _ClassVar[int]
    HISTORY_FIELD_NUMBER: _ClassVar[int]
    PERFORMANCE_FIELD_NUMBER: _ClassVar[int]
    EXPERIMENTS_FIELD_NUMBER: _ClassVar[int]
    DESIGN_FIELD_NUMBER: _ClassVar[int]
    DESIGN_PATCH_FIELD_NUMBER: _ClassVar[int]
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    SUBSCRIBE_FIELD_NUMBER: _ClassVar[int]
    protocol_version: int
    request_id: str
    timestamp: _timestamp_pb2.Timestamp
    pause: PauseCommand
    resume: ResumeCommand
    steer: SteerCommand
    stop: StopCommand
    snapshot: SnapshotQuery
    chat: ChatQuery
    chat_thread_create: ChatThreadCreateQuery
    chat_options: ChatOptionsQuery
    tui_defaults: TuiDefaultsQuery
    history: HistoryQuery
    performance: PerformanceQuery
    experiments: ExperimentQuery
    design: DesignQuery
    design_patch: DesignPatchQuery
    events: EventsQuery
    subscribe: SubscribeRequest
    def __init__(self, protocol_version: _Optional[int] = ..., request_id: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., pause: _Optional[_Union[PauseCommand, _Mapping]] = ..., resume: _Optional[_Union[ResumeCommand, _Mapping]] = ..., steer: _Optional[_Union[SteerCommand, _Mapping]] = ..., stop: _Optional[_Union[StopCommand, _Mapping]] = ..., snapshot: _Optional[_Union[SnapshotQuery, _Mapping]] = ..., chat: _Optional[_Union[ChatQuery, _Mapping]] = ..., chat_thread_create: _Optional[_Union[ChatThreadCreateQuery, _Mapping]] = ..., chat_options: _Optional[_Union[ChatOptionsQuery, _Mapping]] = ..., tui_defaults: _Optional[_Union[TuiDefaultsQuery, _Mapping]] = ..., history: _Optional[_Union[HistoryQuery, _Mapping]] = ..., performance: _Optional[_Union[PerformanceQuery, _Mapping]] = ..., experiments: _Optional[_Union[ExperimentQuery, _Mapping]] = ..., design: _Optional[_Union[DesignQuery, _Mapping]] = ..., design_patch: _Optional[_Union[DesignPatchQuery, _Mapping]] = ..., events: _Optional[_Union[EventsQuery, _Mapping]] = ..., subscribe: _Optional[_Union[SubscribeRequest, _Mapping]] = ...) -> None: ...
