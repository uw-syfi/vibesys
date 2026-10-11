"""Presentation-neutral events emitted by the VibeSys execution core."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

import vs_agent.api as _agent_api

# AgentOutputChannel, AgentStatusData, TodoItemData, and ToolResultPayload are
# used directly below. CommandResultPayload and JsonResultPayload are only the
# ToolResultPayload union members; re-exported here so existing importers of
# vibesys.events keep working.
from vs_agent.api import (
    AgentOutputChannel,
    AgentStatusData,
    TodoItemData,
    ToolResultPayload,
)

CommandResultPayload = _agent_api.CommandResultPayload
JsonResultPayload = _agent_api.JsonResultPayload


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
    ASYNC_OPERATION_LIFECYCLE = "async_operation_lifecycle"
    WORKSTREAM_ATTEMPT_UPDATED = "workstream_attempt_updated"
    RETRY_WAIT_STARTED = "retry_wait_started"
    GPU_CONTENTION_CHANGED = "gpu_contention_changed"
    LOCAL_VALIDATION_SUMMARY = "local_validation_summary"
    TURN_USAGE_RECORDED = "turn_usage_recorded"

    # Run-control transitions (see `vs_runtime.api.infrastructure.RunControlChannel`):
    # request-time events (`*_REQUESTED`, `STEER_QUEUED`, `RESUMED`) come from
    # `RunControl` callers; boundary-consume-time events (`PAUSED`, `STOPPED`,
    # `STEER_CONSUMED`) come from the run boundary or an agent turn. A server
    # projects both onto its own status machine
    # and CONTROL journal; there is no frontend-visible wire event for them.
    STEER_QUEUED = "steer_queued"
    PAUSE_REQUESTED = "pause_requested"
    RESUMED = "resumed"
    STOP_REQUESTED = "stop_requested"
    STEER_CONSUMED = "steer_consumed"
    PAUSED = "paused"
    STOPPED = "stopped"


class EventStatus(StrEnum):
    """Lifecycle status attached to a core event when applicable."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


OutputStream = Literal["stdout", "stderr"]
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


class AsyncOperationKind(StrEnum):
    """Framework-owned categories of asynchronous operation."""

    EVALUATION = "evaluation"
    PROFILER = "profiler"


class AsyncOperationState(StrEnum):
    """Union of backend-neutral lifecycle states published by operation services."""

    SUBMITTED = "submitted"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"
    TIMED_OUT = "timed_out"


class EventPayload(BaseModel):
    """Immutable base for structured core event payloads."""

    model_config = ConfigDict(frozen=True)


class InvocationStartedData(EventPayload):
    """Prompts submitted at the start of a model invocation."""

    kind: Literal["invocation_started"] = "invocation_started"
    system_prompt: str
    user_prompt: str


class InvocationFinishedData(EventPayload):
    """Result or error recorded when a model invocation ends."""

    kind: Literal["invocation_finished"] = "invocation_finished"
    result: Any = None
    error: str | None = None


class AgentExecutionActivityData(EventPayload):
    """Current activity reported for an agent execution."""

    kind: Literal["agent_execution_activity_changed"] = "agent_execution_activity_changed"
    mode: ExecutionActivityMode
    summary: str
    tool: str | None = None


class AgentExecutionStartedData(EventPayload):
    """Semantic context for a prompt-to-result agent execution."""

    kind: Literal["agent_execution_started"] = "agent_execution_started"
    stage: str
    attempt: int | None = None
    system_prompt: str = ""
    user_prompt: str = ""
    activity: AgentExecutionActivityData
    driver: str | None = None
    provider: str | None = None
    model: str | None = None


class AgentExecutionFinishedData(EventPayload):
    """Terminal result or error for an agent execution."""

    kind: Literal["agent_execution_finished"] = "agent_execution_finished"
    result: Any = None
    error: str | None = None


class RunStartedData(EventPayload):
    """Initial input and loop settings for a core run."""

    kind: Literal["run_started"] = "run_started"
    outer_loop: str
    input: str
    max_rounds: int | None = None
    # Policy-owned role hints let frontends seed per-round placeholders.
    # Empty when the policy does not declare roles.
    expected_roles: tuple[str, ...] = ()


ExperimentsChangeReason = Literal[
    "project_attached", "active_hypothesis_changed", "round_persisted"
]


class ExperimentsChangedData(EventPayload):
    """Reason and revision for a changed experiment projection."""

    kind: Literal["experiments_changed"] = "experiments_changed"
    reason: ExperimentsChangeReason
    # Persisted experiment projection revision. None preserves events recorded
    # before revisioned experiment queries existed.
    revision: int | None = Field(default=None, ge=0)


class PhaseData(EventPayload):
    """Name and optional attempt number for a loop phase."""

    kind: Literal["phase"] = "phase"
    phase: str
    attempt: int | None = None


class AgentOutputChunkData(EventPayload):
    """Incremental text emitted during agent execution."""

    kind: Literal["agent_output_chunk"] = "agent_output_chunk"
    channel: AgentOutputChannel
    content: str
    status: AgentStatusData | None = None


class ToolCallData(EventPayload):
    """Tool name, call identity, and arguments emitted by an agent."""

    kind: Literal["tool_call"] = "tool_call"
    tool: str
    call_id: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    status: AgentStatusData | None = None


class ToolResultData(EventPayload):
    """Raw tool result and optional structured payload."""

    kind: Literal["tool_result"] = "tool_result"
    tool: str
    call_id: str | None = None
    content: str
    is_error: bool = False
    payload: ToolResultPayload | None = None


class TodoUpdateData(EventPayload):
    """Current todo list reported by an agent."""

    kind: Literal["todo_update"] = "todo_update"
    todos: list[TodoItemData] = Field(default_factory=list)


class UsageUpdateData(EventPayload):
    """Token usage reported by the active model."""

    kind: Literal["usage_update"] = "usage_update"
    input_tokens: int
    context_window: int | None = None
    model: str | None = None


class SubprocessOutputData(EventPayload):
    """Captured output from a managed subprocess."""

    kind: Literal["subprocess_output"] = "subprocess_output"
    process_id: str
    process_kind: str
    stream: OutputStream
    content: str


class JudgeResultData(EventPayload):
    """One workstream's review assessment: a judge verdict or a skip reason.

    ``skipped`` means no judge turn ran this assessment (the workstream's own
    correlation, carried by the event envelope's ``round_label``, is what
    repeat assessments of the same workstream share); ``skip_reason`` then
    names why. ``implementer_outcome`` is the implementer's own self-declared
    readiness for this attempt, present regardless of whether a review ran.
    It defaults to empty so a journal record written before this field
    existed still parses; a reader should treat an empty string as unknown.
    """

    kind: Literal["judge_result"] = "judge_result"
    verdict: Literal["pass", "fail", "skipped"]
    analysis: str = ""
    feedback: str = ""
    skip_reason: Literal["blocked", "unchanged", "not_due"] | None = None
    implementer_outcome: str = ""


class BenchmarkResultData(EventPayload):
    """Metric result emitted by a benchmark stage."""

    kind: Literal["benchmark_result"] = "benchmark_result"
    metric: str
    value: FiniteFloat
    unit: str


class RoundFinishedData(EventPayload):
    """Summary of attempt, judge, and performance outcomes for a round."""

    kind: Literal["round_finished"] = "round_finished"
    attempts: int
    judge_verdict: Literal["pass", "fail", "skipped"]
    perf_metric: FiniteFloat | None = None
    perf_unit: str | None = None
    # True when no fresh profile ran this round; such a round records no perf
    # reading (perf_metric stays None).
    profile_skipped: bool


class WorkstreamAttemptData(EventPayload):
    """Live retry-budget diagnostics for one dynamic-portfolio workstream.

    Emitted whenever a workstream's attempt bookkeeping changes: a new
    implementer turn spent an attempt, an attempt failed, or an interrupted
    turn was refunded. ``repeated_error`` is true only when this failure is a
    setup failure (no agent turn started) whose error text is identical to
    the workstream's previous setup failure, the deterministic case where
    retrying spends budget on the same outcome every time.
    """

    kind: Literal["workstream_attempt_updated"] = "workstream_attempt_updated"
    hypothesis_id: str
    attempt: int
    attempt_limit: int
    refunded: int
    last_error: str | None = None
    setup_failure: bool = False
    repeated_error: bool = False


class RetryWaitData(EventPayload):
    """One wait before retrying an agent turn that failed on a transient provider error."""

    kind: Literal["retry_wait_started"] = "retry_wait_started"
    attempt: int
    delay_s: FiniteFloat
    error_kind: str


class GpuContentionData(EventPayload):
    """One contention-state transition observed by a GPU contention monitor.

    Emitted only when ``is_contended`` flips (see
    ``vs_sandbox.gpu_monitor.GpuContentionMonitor``), not on every poll.
    ``gpu_index``/``memory_used_mib``/``memory_total_mib``/``utilization_pct``
    are None when no device reading was available for this sample.
    """

    kind: Literal["gpu_contention_changed"] = "gpu_contention_changed"
    is_contended: bool
    new_process_count: int = Field(ge=0)
    gpu_index: int | None = None
    memory_used_mib: int | None = None
    memory_total_mib: int | None = None
    utilization_pct: int | None = None


class LocalValidationSummaryData(EventPayload):
    """Recipe pass-fraction for one ``validate_local`` call.

    ``attempted`` counts recipes that actually ran, stopping at the first
    failure; it is not the catalog size. Emitted once per call, alongside the
    existing per-recipe ``gate_started``/``gate_finished`` events.
    """

    kind: Literal["local_validation_summary"] = "local_validation_summary"
    attempted: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    passed: bool


class TurnUsageData(EventPayload):
    """Token and cost accounting for one completed agent turn.

    Mirrors ``vs_agent.contracts.AgentUsage``; a ``None`` field means the
    provider did not report that figure for this turn.
    """

    kind: Literal["turn_usage_recorded"] = "turn_usage_recorded"
    input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None


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


class AsyncOperationLifecycleData(EventPayload):
    """Backend-neutral lifecycle fact for host-owned asynchronous work."""

    kind: Literal["async_operation_lifecycle"] = "async_operation_lifecycle"
    operation_kind: AsyncOperationKind
    operation_id: str = Field(min_length=1)
    state: AsyncOperationState
    revision: int | None = Field(default=None, ge=0)
    scope_id: str | None = None
    current_stage: str | None = None
    source: FrameworkSource = FrameworkSource.LOOP


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
    | FrameworkWarningData
    | AsyncOperationLifecycleData
    | WorkstreamAttemptData
    | RetryWaitData
    | GpuContentionData
    | LocalValidationSummaryData
    | TurnUsageData,
    Field(discriminator="kind"),
]


class CoreEventWriter(Protocol):
    """Product event surface independent of storage and subscription mechanics."""

    def emit(
        self,
        event_type: CoreEventType,
        text: str = "",
        *,
        data: CoreEventData | None = None,
        **fields: object,
    ) -> CoreEvent:
        """Create and publish one semantic event."""
        ...

    def record(self, event: CoreEvent) -> CoreEvent:
        """Publish one already-created semantic event."""
        ...


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
    # Stable identity of the issuing frontend connection, carried on
    # request-time run-control transitions (see
    # `vs_runtime.api.infrastructure.RunControlTransition`). ``None`` off that
    # path, mirroring `server.api.protocol.Request.client_id`'s empty-string
    # compatibility default.
    client_id: str | None = None
    data: CoreEventData | None = None


def make_core_event(
    event_type: CoreEventType,
    text: str = "",
    **fields: object,
) -> CoreEvent:
    """Create an unrecorded event using the current UTC time."""
    return CoreEvent.model_validate(
        {"timestamp": datetime.now(UTC), "type": event_type, "text": text, **fields}
    )


def json_value(value: object) -> object:
    """Return a JSON-compatible value without losing useful diagnostics."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    try:
        json.dumps(value)
    except TypeError:
        return repr(value)
    else:
        return value
