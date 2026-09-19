from server.wire.v2 import common_pb2 as _common_pb2
from server.wire.v2 import events_pb2 as _events_pb2
from server.wire.v2 import snapshot_pb2 as _snapshot_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SubscribedMessage(_message.Message):
    __slots__ = ("request_id", "run_id", "latest_sequence")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    LATEST_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    run_id: str
    latest_sequence: int
    def __init__(self, request_id: _Optional[str] = ..., run_id: _Optional[str] = ..., latest_sequence: _Optional[int] = ...) -> None: ...

class EventBatchMessage(_message.Message):
    __slots__ = ("events", "through_sequence", "active_executions", "store_id", "history_after_sequence")
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    THROUGH_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_EXECUTIONS_FIELD_NUMBER: _ClassVar[int]
    STORE_ID_FIELD_NUMBER: _ClassVar[int]
    HISTORY_AFTER_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    events: _containers.RepeatedCompositeFieldContainer[_events_pb2.RunEvent]
    through_sequence: int
    active_executions: _containers.RepeatedCompositeFieldContainer[_snapshot_pb2.ActiveAgentExecution]
    store_id: str
    history_after_sequence: int
    def __init__(self, events: _Optional[_Iterable[_Union[_events_pb2.RunEvent, _Mapping]]] = ..., through_sequence: _Optional[int] = ..., active_executions: _Optional[_Iterable[_Union[_snapshot_pb2.ActiveAgentExecution, _Mapping]]] = ..., store_id: _Optional[str] = ..., history_after_sequence: _Optional[int] = ...) -> None: ...

class ProtocolErrorMessage(_message.Message):
    __slots__ = ("request_id", "code", "message", "diagnostic")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DIAGNOSTIC_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    code: str
    message: str
    diagnostic: _common_pb2.Diagnostic
    def __init__(self, request_id: _Optional[str] = ..., code: _Optional[str] = ..., message: _Optional[str] = ..., diagnostic: _Optional[_Union[_common_pb2.Diagnostic, _Mapping]] = ...) -> None: ...

class ServerMessage(_message.Message):
    __slots__ = ("subscribed", "event", "event_batch", "protocol_error")
    SUBSCRIBED_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    EVENT_BATCH_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_ERROR_FIELD_NUMBER: _ClassVar[int]
    subscribed: SubscribedMessage
    event: _events_pb2.RunEvent
    event_batch: EventBatchMessage
    protocol_error: ProtocolErrorMessage
    def __init__(self, subscribed: _Optional[_Union[SubscribedMessage, _Mapping]] = ..., event: _Optional[_Union[_events_pb2.RunEvent, _Mapping]] = ..., event_batch: _Optional[_Union[EventBatchMessage, _Mapping]] = ..., protocol_error: _Optional[_Union[ProtocolErrorMessage, _Mapping]] = ...) -> None: ...
