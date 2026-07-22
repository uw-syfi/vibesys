"""Versioned transport-neutral contracts for supervision clients."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vibesys.server.events import RunEvent

PROTOCOL_VERSION = 1


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Request(ProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PauseCommand(Request):
    type: Literal["command.pause"] = "command.pause"
    mode: Literal["after_current_agent_call"] = "after_current_agent_call"


class ResumeCommand(Request):
    type: Literal["command.resume"] = "command.resume"


class SnapshotQuery(Request):
    type: Literal["query.snapshot"] = "query.snapshot"


class ChatQuery(Request):
    type: Literal["query.chat"] = "query.chat"
    text: str


class HistoryQuery(Request):
    type: Literal["query.history"] = "query.history"


class PerformanceQuery(Request):
    type: Literal["query.performance"] = "query.performance"


class EventsQuery(Request):
    type: Literal["query.events"] = "query.events"
    after_sequence: int = Field(default=0, ge=0)
    timeout_ms: int = Field(default=0, ge=0, le=30_000)


class SubscribeRequest(Request):
    type: Literal["subscribe"] = "subscribe"
    after_sequence: int = Field(default=0, ge=0)


ProtocolRequest = Annotated[
    PauseCommand
    | ResumeCommand
    | SnapshotQuery
    | ChatQuery
    | HistoryQuery
    | PerformanceQuery
    | EventsQuery
    | SubscribeRequest,
    Field(discriminator="type"),
]


class RunSnapshot(ProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    run_id: str
    sequence: int
    status: str
    agent_kind: str | None = None
    round_label: str | None = None


class CommandAck(ProtocolModel):
    action: Literal["pause", "resume"]
    status: Literal["pending", "consumed"]


class ChatResult(ProtocolModel):
    question: str
    answer: str
    effect: Literal["none"] = "none"


class PerformanceRound(ProtocolModel):
    round: int
    perf_metric: FiniteFloat
    perf_unit: str
    passed: bool
    profile_skipped: bool = False


class Response(ProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ok: bool = True
    error: str | None = None
    ack: CommandAck | None = None
    chat: ChatResult | None = None
    snapshot: RunSnapshot | None = None
    events: list[RunEvent] = Field(default_factory=list)
    performance: list[PerformanceRound] = Field(default_factory=list)


class SubscribedMessage(ProtocolModel):
    type: Literal["subscribed"] = "subscribed"
    request_id: str
    run_id: str
    latest_sequence: int


class EventMessage(ProtocolModel):
    type: Literal["event"] = "event"
    event: RunEvent


class EventBatchMessage(ProtocolModel):
    type: Literal["event_batch"] = "event_batch"
    events: list[RunEvent]


class ProtocolErrorMessage(ProtocolModel):
    type: Literal["protocol_error"] = "protocol_error"
    request_id: str | None = None
    code: str
    message: str


ServerMessage = Annotated[
    SubscribedMessage | EventMessage | EventBatchMessage | ProtocolErrorMessage,
    Field(discriminator="type"),
]
