"""Versioned transport-neutral contracts for supervision clients."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from vibe_serve.server.events import RunEvent

PROTOCOL_VERSION = 1


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Request(ProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SteerCommand(Request):
    type: Literal["command.steer"] = "command.steer"
    text: str
    target: Literal["next_safe_point"] = "next_safe_point"


class PauseCommand(Request):
    type: Literal["command.pause"] = "command.pause"
    mode: Literal["after_current_agent_call"] = "after_current_agent_call"


class ResumeCommand(Request):
    type: Literal["command.resume"] = "command.resume"


class StatusQuery(Request):
    type: Literal["query.status"] = "query.status"


class SnapshotQuery(Request):
    type: Literal["query.snapshot"] = "query.snapshot"


class ChatQuery(Request):
    type: Literal["query.chat"] = "query.chat"
    text: str


class HistoryQuery(Request):
    type: Literal["query.history"] = "query.history"


class RoundQuery(Request):
    type: Literal["query.round"] = "query.round"
    round_number: int = Field(ge=1)


class InvocationQuery(Request):
    type: Literal["query.invocation"] = "query.invocation"
    invocation_id: str


class ArtifactQuery(Request):
    type: Literal["query.artifact"] = "query.artifact"
    path: str


class EventsQuery(Request):
    type: Literal["query.events"] = "query.events"
    after_sequence: int = Field(default=0, ge=0)
    timeout_ms: int = Field(default=0, ge=0, le=30_000)


ProtocolRequest = Annotated[
    SteerCommand
    | PauseCommand
    | ResumeCommand
    | StatusQuery
    | SnapshotQuery
    | ChatQuery
    | HistoryQuery
    | RoundQuery
    | InvocationQuery
    | ArtifactQuery
    | EventsQuery,
    Field(discriminator="type"),
]


class RunSnapshot(ProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    run_id: str
    sequence: int
    status: str
    agent_kind: str | None = None
    round_label: str | None = None
    pending_steering: int = 0
    last_consumed_steering: str | None = None


class CommandAck(ProtocolModel):
    action: Literal["steer", "pause", "resume"]
    status: Literal["pending", "consumed"]
    resource_id: str | None = None


class ChatResult(ProtocolModel):
    question: str
    answer: str
    effect: Literal["none"] = "none"


class TextBlock(ProtocolModel):
    source: str
    content: str


class RoundResult(ProtocolModel):
    round_number: int
    blocks: list[TextBlock] = Field(default_factory=list)


class ArtifactResult(ProtocolModel):
    path: str
    content: str


class Response(ProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ok: bool = True
    error: str | None = None
    ack: CommandAck | None = None
    chat: ChatResult | None = None
    round: RoundResult | None = None
    artifact: ArtifactResult | None = None
    snapshot: RunSnapshot | None = None
    events: list[RunEvent] = Field(default_factory=list)
