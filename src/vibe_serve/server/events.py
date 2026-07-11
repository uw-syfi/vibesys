"""Typed, append-only event contract for interactive VibeServe runs."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class EventType(StrEnum):
    SERVER_STARTED = "server_started"
    SERVER_READY = "server_ready"
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


class EventStatus(StrEnum):
    ACTIVE = "active"
    ANSWERED = "answered"
    PENDING = "pending"
    CONSUMED = "consumed"
    COMPLETED = "completed"
    FAILED = "failed"


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
    stream: Literal["stdout", "stderr"]
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


class PhaseData(BaseModel):
    kind: Literal["phase"] = "phase"
    phase: str
    attempt: int | None = None


class AgentOutputChunkData(BaseModel):
    kind: Literal["agent_output_chunk"] = "agent_output_chunk"
    channel: Literal["assistant", "analysis", "tool", "diagnostic", "prompt"]
    content: str


class SubprocessOutputData(BaseModel):
    kind: Literal["subprocess_output"] = "subprocess_output"
    process_id: str
    process_kind: str
    stream: Literal["stdout", "stderr"]
    content: str


class JudgeResultData(BaseModel):
    kind: Literal["judge_result"] = "judge_result"
    verdict: Literal["pass", "fail"]
    feedback: str
    attempt: int


class BenchmarkResultData(BaseModel):
    kind: Literal["benchmark_result"] = "benchmark_result"
    metric: str
    value: float
    unit: str


class RoundFinishedData(BaseModel):
    kind: Literal["round_finished"] = "round_finished"
    attempts: int
    judge_verdict: Literal["pass", "fail"]
    perf_metric: float | None = None
    perf_unit: str | None = None


EventData = Annotated[
    ChatData
    | InvocationStartedData
    | InvocationFinishedData
    | OutputData
    | ServerReadyData
    | RunStartedData
    | RunInterruptedData
    | PhaseData
    | AgentOutputChunkData
    | SubprocessOutputData
    | JudgeResultData
    | BenchmarkResultData
    | RoundFinishedData,
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
        self._next_sequence = (
            max((event.sequence for event in self._read_unlocked()), default=0) + 1
        )

    def append(self, event: RunEvent) -> RunEvent:
        with self._changed, self.path.open("a", encoding="utf-8") as stream:
            event = event.model_copy(
                update={"sequence": self._next_sequence, "run_id": self.run_id}
            )
            self._next_sequence += 1
            stream.write(event.model_dump_json() + "\n")
            self._changed.notify_all()
            return event

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._next_sequence - 1

    def read(self, after_sequence: int = 0) -> list[RunEvent]:
        with self._lock:
            return [event for event in self._read_unlocked() if event.sequence > after_sequence]

    def wait(self, after_sequence: int, timeout: float | None = None) -> list[RunEvent]:
        """Block until replayable events exist after a client's cursor."""
        with self._changed:
            events = [event for event in self._read_unlocked() if event.sequence > after_sequence]
            if events:
                return events
            self._changed.wait(timeout)
            return [event for event in self._read_unlocked() if event.sequence > after_sequence]

    def _read_unlocked(self) -> list[RunEvent]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        events = []
        for index, line in enumerate(lines):
            try:
                event = RunEvent.model_validate_json(line)
                events.append(event)
            except ValidationError:
                # Preserve access to earlier audit history if a process was
                # interrupted during its final append.
                if index == len(lines) - 1:
                    continue
                raise
        return events


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
