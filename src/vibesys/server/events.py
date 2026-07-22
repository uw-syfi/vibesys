"""Typed, append-only event contract for interactive VibeSys runs."""

from __future__ import annotations

import json
import threading
from bisect import bisect_right
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError


class EventType(StrEnum):
    SERVER_STARTED = "server_started"
    SERVER_READY = "server_ready"
    CONFIGURATION_FAILED = "configuration_failed"
    RUN_STARTED = "run_started"
    RUN_INTERRUPTED = "run_interrupted"
    CHAT = "chat"
    STATUS_QUERY = "status_query"
    CONTROL = "control"
    INVOCATION_STARTED = "invocation_started"
    INVOCATION_FINISHED = "invocation_finished"
    PHASE_STARTED = "phase_started"
    PHASE_FINISHED = "phase_finished"
    AGENT_OUTPUT_CHUNK = "agent_output_chunk"
    SUBPROCESS_OUTPUT = "subprocess_output"
    JUDGE_RESULT = "judge_result"
    BENCHMARK_RESULT = "benchmark_result"
    ROUND_FINISHED = "round_finished"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    OUTPUT = "output"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TODO_UPDATE = "todo_update"
    USAGE_UPDATE = "usage_update"


class EventStatus(StrEnum):
    ACTIVE = "active"
    ANSWERED = "answered"
    PENDING = "pending"
    CONSUMED = "consumed"
    COMPLETED = "completed"
    FAILED = "failed"


OutputStream = Literal["stdout", "stderr"]
"""Which host stream a captured line of backend output came from."""

AgentOutputChannel = Literal["assistant", "analysis", "tool", "diagnostic", "prompt"]
"""Presentation channel for streamed agent output."""


class ChatData(BaseModel):
    kind: Literal["chat"] = "chat"
    answer: str


class InvocationStartedData(BaseModel):
    kind: Literal["invocation_started"] = "invocation_started"
    system_prompt: str
    user_prompt: str


class InvocationFinishedData(BaseModel):
    kind: Literal["invocation_finished"] = "invocation_finished"
    result: Any = None
    error: str | None = None


class OutputData(BaseModel):
    kind: Literal["output"] = "output"
    stream: OutputStream
    source: str = "backend"
    content: str


class ServerReadyData(BaseModel):
    kind: Literal["server_ready"] = "server_ready"
    socket_protocol: Literal["jsonl"] = "jsonl"


class RunStartedData(BaseModel):
    kind: Literal["run_started"] = "run_started"
    outer_loop: str
    input: str
    max_rounds: int


class RunInterruptedData(BaseModel):
    kind: Literal["run_interrupted"] = "run_interrupted"
    reason: str
    signal: str | None = None


class ConfigurationFailedData(BaseModel):
    kind: Literal["configuration_failed"] = "configuration_failed"
    code: str
    stage: str
    message: str
    usage: str | None = None
    exit_code: int


class PhaseData(BaseModel):
    kind: Literal["phase"] = "phase"
    phase: str
    attempt: int | None = None


class AgentStatusData(BaseModel):
    """Structured progress readings for one agent invocation.

    Carried on presentation events so renderers can format their own status
    prefix (e.g. ``[Round 3/24 | Implementer | 12.3s | 20k/1.0M]``) without
    the backend baking any layout or styling into the payload.
    """

    progress: str | None = None
    agent_label: str | None = None
    elapsed_seconds: float = 0.0
    input_tokens: int = 0
    context_window: int | None = None


class AgentOutputChunkData(BaseModel):
    kind: Literal["agent_output_chunk"] = "agent_output_chunk"
    channel: AgentOutputChannel
    content: str
    status: AgentStatusData | None = None


class ToolCallData(BaseModel):
    kind: Literal["tool_call"] = "tool_call"
    tool: str
    call_id: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    status: AgentStatusData | None = None


class ToolResultData(BaseModel):
    kind: Literal["tool_result"] = "tool_result"
    tool: str
    call_id: str | None = None
    content: str
    is_error: bool = False


class TodoItemData(BaseModel):
    content: str
    # Expected values are "pending" / "in_progress" / "completed", but the
    # field stays open: todo payloads originate from agent tool calls, and an
    # unknown status must degrade in the renderer, not fail event emission.
    status: str


class TodoUpdateData(BaseModel):
    kind: Literal["todo_update"] = "todo_update"
    todos: list[TodoItemData] = Field(default_factory=list)


class UsageUpdateData(BaseModel):
    kind: Literal["usage_update"] = "usage_update"
    input_tokens: int
    context_window: int | None = None
    model: str | None = None


class SubprocessOutputData(BaseModel):
    kind: Literal["subprocess_output"] = "subprocess_output"
    process_id: str
    process_kind: str
    stream: OutputStream
    content: str


class JudgeResultData(BaseModel):
    kind: Literal["judge_result"] = "judge_result"
    verdict: Literal["pass", "fail"]
    feedback: str
    attempt: int


class BenchmarkResultData(BaseModel):
    kind: Literal["benchmark_result"] = "benchmark_result"
    metric: str
    value: FiniteFloat
    unit: str


class RoundFinishedData(BaseModel):
    kind: Literal["round_finished"] = "round_finished"
    attempts: int
    judge_verdict: Literal["pass", "fail"]
    perf_metric: FiniteFloat | None = None
    perf_unit: str | None = None


EventData = Annotated[
    ChatData
    | InvocationStartedData
    | InvocationFinishedData
    | OutputData
    | ServerReadyData
    | RunStartedData
    | RunInterruptedData
    | ConfigurationFailedData
    | PhaseData
    | AgentOutputChunkData
    | SubprocessOutputData
    | JudgeResultData
    | BenchmarkResultData
    | RoundFinishedData
    | ToolCallData
    | ToolResultData
    | TodoUpdateData
    | UsageUpdateData,
    Field(discriminator="kind"),
]


class RunEvent(BaseModel):
    """One reproducible human, control, or invocation event."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1] = 1
    sequence: int = Field(default=0, ge=0)
    run_id: str = ""
    timestamp: datetime
    type: EventType
    text: str = ""
    status: EventStatus | None = None
    round_label: str | None = None
    agent_kind: str | None = None
    invocation_id: str | None = None
    data: EventData | None = None


class EventStore:
    """Serialize event access so readers never observe partial JSONL writes."""

    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._events, self._malformed_tail_offset = self._read_unlocked()
        self._sequences = [event.sequence for event in self._events]
        self._sequences_monotonic = all(
            previous <= current
            for previous, current in zip(self._sequences, self._sequences[1:], strict=False)
        )
        self._next_sequence = max(self._sequences, default=0) + 1

    def append(self, event: RunEvent) -> RunEvent:
        with self._changed:
            if self._malformed_tail_offset is not None:
                with self.path.open("r+b") as stream:
                    stream.truncate(self._malformed_tail_offset)
                self._malformed_tail_offset = None
            event = event.model_copy(
                update={"sequence": self._next_sequence, "run_id": self.run_id}
            )
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(event.model_dump_json() + "\n")
            self._next_sequence += 1
            self._events.append(event.model_copy(deep=True))
            self._sequences.append(event.sequence)
            self._changed.notify_all()
            return event

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._next_sequence - 1

    def read(self, after_sequence: int = 0) -> list[RunEvent]:
        with self._lock:
            return self._events_after_unlocked(after_sequence)

    def wait(self, after_sequence: int, timeout: float | None = None) -> list[RunEvent]:
        """Block until replayable events exist after a client's cursor."""
        with self._changed:
            events = self._events_after_unlocked(after_sequence)
            if events:
                return events
            self._changed.wait(timeout)
            return self._events_after_unlocked(after_sequence)

    def _events_after_unlocked(self, after_sequence: int) -> list[RunEvent]:
        if self._sequences_monotonic:
            start = bisect_right(self._sequences, after_sequence)
            events = self._events[start:]
        else:
            # Preserve the historical file-order filtering semantics for a
            # manually edited or legacy log whose sequences are not sorted.
            events = [event for event in self._events if event.sequence > after_sequence]
        return [event.model_copy(deep=True) for event in events]

    def _read_unlocked(self) -> tuple[list[RunEvent], int | None]:
        if not self.path.exists():
            return [], None
        lines = self.path.read_bytes().splitlines(keepends=True)
        events: list[RunEvent] = []
        offset = 0
        for index, line in enumerate(lines):
            record_offset = offset
            offset += len(line)
            try:
                event = RunEvent.model_validate_json(line)
                events.append(event)
            except ValidationError:
                # Preserve access to earlier audit history if a process was
                # interrupted during its final append.
                if index == len(lines) - 1:
                    return events, record_offset
                raise
        return events, None


def make_event(event_type: EventType, text: str = "", **fields: Any) -> RunEvent:
    return RunEvent(timestamp=datetime.now(UTC), type=event_type, text=text, **fields)


def json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)
