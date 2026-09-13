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
    GATE_STARTED = "gate_started"
    GATE_FINISHED = "gate_finished"
    WORKSPACE_SNAPSHOT = "workspace_snapshot"
    RUN_CONFIGURED = "run_configured"
    FRAMEWORK_WARNING = "framework_warning"


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


class GateKind(StrEnum):
    """Closed set of framework-owned gates a candidate passes through."""

    VALIDATION = "validation"
    ACCURACY = "accuracy"
    BENCHMARK = "benchmark"


class FrameworkSource(StrEnum):
    """Closed set of framework subsystems that emit framework events.

    ``source_label`` on the payloads carries a finer free-text origin (for
    example ``"skills"`` within ``LOOP``) without widening this set.
    """

    GATES = "gates"
    GIT_TRACKING = "git_tracking"
    LOOP = "loop"
    GPU = "gpu"
    SKYPILOT = "skypilot"
    OTHER = "other"


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


class GateStartedData(EventPayload):
    """One framework gate began evaluating the current candidate.

    Every ``gate_started`` is followed by exactly one ``gate_finished`` for
    the same gate (and recipe, for validation), including reused results.
    """

    kind: Literal["gate_started"] = "gate_started"
    gate: GateKind
    # The validation recipe being executed; None for accuracy and benchmark.
    recipe: str | None = None
    # The trusted command the gate runs, when one is configured.
    command: str | None = None
    source: FrameworkSource = FrameworkSource.GATES
    source_label: str | None = None


class GateFinishedData(EventPayload):
    """Outcome of one framework gate; envelope status carries pass or fail.

    ``metric``/``value``/``unit`` are set only on a passing benchmark gate.
    ``unit`` keeps the historical fallback of the metric name when the
    contract declares no unit. ``output_tail`` carries the trailing command
    output on failure.
    """

    kind: Literal["gate_finished"] = "gate_finished"
    gate: GateKind
    recipe: str | None = None
    # True when a prior PASS for the exact same input was reused instead of
    # re-running the command.
    reused: bool = False
    metric: str | None = None
    value: FiniteFloat | None = None
    unit: str | None = None
    output_tail: str | None = None
    source: FrameworkSource = FrameworkSource.GATES
    source_label: str | None = None


class WorkspaceSnapshotData(EventPayload):
    """A Git tracker outcome: a snapshot, baseline, or exclusion change.

    Exactly one aspect is populated per event: a snapshot attempt carries
    ``label`` (``commit`` is None when there was nothing to commit), a
    trusted-input baseline carries ``baseline``, and a snapshot-exclusion
    change carries ``excluded_paths``.
    """

    kind: Literal["workspace_snapshot"] = "workspace_snapshot"
    label: str = ""
    commit: str | None = None
    baseline: str | None = None
    excluded_paths: tuple[str, ...] = ()
    source: FrameworkSource = FrameworkSource.GIT_TRACKING


class RunConfiguredData(EventPayload):
    """One per run: the resolved configuration a loop starts with."""

    kind: Literal["run_configured"] = "run_configured"
    run_log_path: str
    project_root: str
    model: str | None = None
    # First line of the objective only; the full text lives in run state.
    objective: str | None = None
    search_policy: str | None = None
    benchmark_contract: bool = False
    pareto_objectives: str | None = None
    source: FrameworkSource = FrameworkSource.LOOP


class FrameworkWarningData(EventPayload):
    """A non-fatal framework fault an operator should see."""

    kind: Literal["framework_warning"] = "framework_warning"
    summary: str
    detail: str | None = None
    source: FrameworkSource = FrameworkSource.OTHER
    source_label: str | None = None


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
    | UsageUpdateData
    | GateStartedData
    | GateFinishedData
    | WorkspaceSnapshotData
    | RunConfiguredData
    | FrameworkWarningData,
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
