"""Presentation-neutral events emitted by the VibeSys execution core."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class CoreEventType(StrEnum):
    """Closed set of observations produced by a core run."""

    RUN_STARTED = "run_started"
    EXPERIMENTS_CHANGED = "experiments_changed"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    INVOCATION_STARTED = "invocation_started"
    INVOCATION_FINISHED = "invocation_finished"
    AGENT_EXECUTION_STARTED = "agent_execution_started"
    AGENT_EXECUTION_ACTIVITY_CHANGED = "agent_execution_activity_changed"
    AGENT_EXECUTION_FINISHED = "agent_execution_finished"
    PHASE_STARTED = "phase_started"
    PHASE_FINISHED = "phase_finished"
    AGENT_OUTPUT_CHUNK = "agent_output_chunk"
    SUBPROCESS_OUTPUT = "subprocess_output"
    JUDGE_RESULT = "judge_result"
    BENCHMARK_RESULT = "benchmark_result"
    ROUND_FINISHED = "round_finished"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TODO_UPDATE = "todo_update"
    USAGE_UPDATE = "usage_update"


class EventStatus(StrEnum):
    """Lifecycle status attached to a core event when applicable."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


OutputStream = Literal["stdout", "stderr"]
AgentOutputChannel = Literal["assistant", "analysis", "tool", "diagnostic", "prompt"]
ExecutionActivityMode = Literal["thinking", "responding", "tool", "waiting"]


class EventPayload(BaseModel):
    """Immutable base for structured core event payloads."""

    model_config = ConfigDict(frozen=True)


class InvocationStartedData(EventPayload):  # noqa: D101
    kind: Literal["invocation_started"] = "invocation_started"
    system_prompt: str
    user_prompt: str


class InvocationFinishedData(EventPayload):  # noqa: D101
    kind: Literal["invocation_finished"] = "invocation_finished"
    result: Any = None
    error: str | None = None


class AgentExecutionActivityData(EventPayload):  # noqa: D101
    kind: Literal["agent_execution_activity_changed"] = "agent_execution_activity_changed"
    mode: ExecutionActivityMode
    summary: str
    tool: str | None = None


class AgentExecutionStartedData(EventPayload):  # noqa: D101
    kind: Literal["agent_execution_started"] = "agent_execution_started"
    stage: str
    attempt: int | None = None
    system_prompt: str = ""
    user_prompt: str = ""
    activity: AgentExecutionActivityData
    driver: str | None = None
    provider: str | None = None
    model: str | None = None


class AgentExecutionFinishedData(EventPayload):  # noqa: D101
    kind: Literal["agent_execution_finished"] = "agent_execution_finished"
    result: Any = None
    error: str | None = None


class RunStartedData(EventPayload):  # noqa: D101
    kind: Literal["run_started"] = "run_started"
    outer_loop: str
    input: str
    max_rounds: int
    # The agent roles the loop can run per round (vibesys.loops.roles), so
    # frontends seed placeholders from the contract instead of a client-side
    # table. Empty on events recorded before the field existed.
    expected_roles: tuple[str, ...] = ()


class ExperimentsChangedData(EventPayload):  # noqa: D101
    kind: Literal["experiments_changed"] = "experiments_changed"
    reason: Literal["project_attached", "active_hypothesis_changed", "round_persisted"]
    # Persisted experiment projection revision. None preserves events recorded
    # before revisioned experiment queries existed.
    revision: int | None = Field(default=None, ge=0)


class PhaseData(EventPayload):  # noqa: D101
    kind: Literal["phase"] = "phase"
    phase: str
    attempt: int | None = None


class AgentStatusData(EventPayload):  # noqa: D101
    progress: str | None = None
    agent_label: str | None = None
    elapsed_seconds: float = 0.0
    input_tokens: int = 0
    context_window: int | None = None


class AgentOutputChunkData(EventPayload):  # noqa: D101
    kind: Literal["agent_output_chunk"] = "agent_output_chunk"
    channel: AgentOutputChannel
    content: str
    status: AgentStatusData | None = None


class ToolCallData(EventPayload):  # noqa: D101
    kind: Literal["tool_call"] = "tool_call"
    tool: str
    call_id: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    status: AgentStatusData | None = None


class CommandResultPayload(EventPayload):  # noqa: D101
    kind: Literal["command"] = "command"
    stdout: str
    stderr: str
    exit_code: int | None = None
    duration: float | None = None


class JsonResultPayload(EventPayload):  # noqa: D101
    kind: Literal["json"] = "json"
    value: dict[str, Any] | list[Any]


ToolResultPayload = Annotated[
    CommandResultPayload | JsonResultPayload,
    Field(discriminator="kind"),
]


class ToolResultData(EventPayload):  # noqa: D101
    kind: Literal["tool_result"] = "tool_result"
    tool: str
    call_id: str | None = None
    content: str
    is_error: bool = False
    payload: ToolResultPayload | None = None


class TodoItemData(EventPayload):  # noqa: D101
    content: str
    status: str


class TodoUpdateData(EventPayload):  # noqa: D101
    kind: Literal["todo_update"] = "todo_update"
    todos: list[TodoItemData] = Field(default_factory=list)


class UsageUpdateData(EventPayload):  # noqa: D101
    kind: Literal["usage_update"] = "usage_update"
    input_tokens: int
    context_window: int | None = None
    model: str | None = None


class SubprocessOutputData(EventPayload):  # noqa: D101
    kind: Literal["subprocess_output"] = "subprocess_output"
    process_id: str
    process_kind: str
    stream: OutputStream
    content: str


class JudgeResultData(EventPayload):  # noqa: D101
    kind: Literal["judge_result"] = "judge_result"
    verdict: Literal["pass", "fail"]
    feedback: str
    attempt: int


class BenchmarkResultData(EventPayload):  # noqa: D101
    kind: Literal["benchmark_result"] = "benchmark_result"
    metric: str
    value: FiniteFloat
    unit: str


class RoundFinishedData(EventPayload):  # noqa: D101
    kind: Literal["round_finished"] = "round_finished"
    attempts: int
    judge_verdict: Literal["pass", "fail", "skipped"]
    perf_metric: FiniteFloat | None = None
    perf_unit: str | None = None
    # True when no fresh profile ran this round; such a round records no perf
    # reading (perf_metric stays None). Defaults False so legacy persisted
    # events stay valid.
    profile_skipped: bool = False


CoreEventData = Annotated[
    InvocationStartedData
    | InvocationFinishedData
    | AgentExecutionStartedData
    | AgentExecutionActivityData
    | AgentExecutionFinishedData
    | RunStartedData
    | ExperimentsChangedData
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


class CoreEvent(BaseModel):
    """One immutable core observation, optionally assigned a durable cursor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(default=0, ge=0)
    run_id: str = ""
    timestamp: datetime
    type: CoreEventType
    text: str = ""
    status: EventStatus | None = None
    round_label: str | None = None
    agent_kind: str | None = None
    execution_id: str | None = None
    data: CoreEventData | None = None


def make_core_event(
    event_type: CoreEventType,
    text: str = "",
    **fields: Any,  # noqa: ANN401
) -> CoreEvent:
    """Create an unrecorded event using the current UTC time."""
    return CoreEvent(timestamp=datetime.now(UTC), type=event_type, text=text, **fields)


def json_value(value: Any) -> Any:  # noqa: ANN401
    """Return a JSON-compatible value without losing useful diagnostics."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    try:
        json.dumps(value)
    except TypeError:
        return repr(value)
    else:
        return value
